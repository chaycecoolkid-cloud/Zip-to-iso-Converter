#!/usr/bin/env python3
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile

VERSION = "v1.0.1"

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox
except ImportError:  # pragma: no cover
    tk = None
    filedialog = None
    messagebox = None


def run_cmd(cmd, capture_output=False):
    result = subprocess.run(cmd, check=False, capture_output=capture_output, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip() if result.stderr else result.stdout.strip()
        raise RuntimeError(detail or f"Command failed with exit code {result.returncode}: {' '.join(cmd)}")
    return result


def resolve_tool(tool_name):
    candidates = [tool_name, f"/usr/sbin/{tool_name}", f"/usr/bin/{tool_name}"]
    for candidate in candidates:
        if shutil.which(candidate) is not None:
            return shutil.which(candidate)
        if os.path.exists(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def ensure_iso_tools():
    debootstrap_path = resolve_tool("debootstrap")
    if debootstrap_path is None:
        install = [
            "apt-get",
            "install",
            "-y",
            "--no-install-recommends",
            "debootstrap",
            "grub-common",
            "grub-pc-bin",
            "grub-efi-amd64-bin",
            "mtools",
        ]
        if os.geteuid() != 0:
            run_cmd(["sudo", "-n", *install])
        else:
            run_cmd(install)
        debootstrap_path = resolve_tool("debootstrap")
    if debootstrap_path is None:
        raise RuntimeError("debootstrap is required to build a Debian rootfs and ISO.")

    mformat = resolve_tool("mformat")
    if mformat is None:
        install = [
            "apt-get",
            "install",
            "-y",
            "--no-install-recommends",
            "mtools",
        ]
        if os.geteuid() != 0:
            run_cmd(["sudo", "-n", *install])
        else:
            run_cmd(install)
        mformat = resolve_tool("mformat")
    if mformat is None:
        raise RuntimeError("mtools/mformat is required for grub-mkrescue to create a bootable ISO.")

    grub = resolve_tool("grub-mkrescue")
    if grub is None:
        raise RuntimeError("grub-mkrescue is required to build a bootable ISO.")
    return debootstrap_path, grub


def extract_zip(zip_path: Path, temp_dir: Path) -> Path:
    payload_dir = temp_dir / "zip_payload"
    with ZipFile(zip_path) as archive:
        archive.extractall(payload_dir)
    return payload_dir


def bind_mounts(rootfs_dir: Path):
    mount_points = {
        "/dev": rootfs_dir / "dev",
        "/proc": rootfs_dir / "proc",
        "/sys": rootfs_dir / "sys",
        "/run": rootfs_dir / "run",
    }
    for src, dst in mount_points.items():
        dst.mkdir(parents=True, exist_ok=True)
        if not os.path.ismount(dst):
            cmd = ["mount", "--bind", src, str(dst)]
            if os.geteuid() != 0:
                cmd = ["sudo", "-n", *cmd]
            try:
                run_cmd(cmd)
            except Exception:
                pass


def unbind_mounts(rootfs_dir: Path):
    mount_points = {
        "/dev": rootfs_dir / "dev",
        "/proc": rootfs_dir / "proc",
        "/sys": rootfs_dir / "sys",
        "/run": rootfs_dir / "run",
    }
    for src, dst in mount_points.items():
        cmd = ["umount", str(dst)]
        if os.geteuid() != 0:
            cmd = ["sudo", "-n", *cmd]
        try:
            run_cmd(cmd)
        except Exception:
            pass


def chroot(rootfs_dir: Path, command: str) -> None:
    bind_mounts(rootfs_dir)
    try:
        cmd = ["chroot", str(rootfs_dir), "bash", "-lc", command]
        if os.geteuid() != 0:
            cmd = ["sudo", "-n", *cmd]
        run_cmd(cmd)
    finally:
        unbind_mounts(rootfs_dir)


def bootstrap_rootfs(rootfs_dir: Path, debootstrap_path: str) -> None:
    if not rootfs_dir.exists():
        rootfs_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        debootstrap_path,
        "--variant=minbase",
        "--arch",
        "amd64",
        "bookworm",
        str(rootfs_dir),
        "http://deb.debian.org/debian",
    ]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n", *cmd]
    run_cmd(cmd, capture_output=True)


def prepare_rootfs(rootfs_dir: Path, payload_dir: Path) -> None:
    rootfs_dir.mkdir(parents=True, exist_ok=True)
    target = rootfs_dir / "root" / "zip-os"
    target.mkdir(parents=True, exist_ok=True)
    for item in payload_dir.iterdir():
        dest = target / item.name
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest)

    (rootfs_dir / "etc").mkdir(parents=True, exist_ok=True)
    (rootfs_dir / "etc" / "hostname").write_text("zip-os\n", encoding="utf-8")
    (rootfs_dir / "etc" / "hosts").write_text(
        "127.0.0.1 localhost\n::1 localhost\n127.0.1.1 zip-os\n",
        encoding="utf-8",
    )
    (rootfs_dir / "etc" / "issue").write_text("ZIP OS\\n\\l\n", encoding="utf-8")
    (rootfs_dir / "root" / "README.txt").write_text(
        "This Debian-based OS was created from a ZIP archive.\nYour extracted files are in /root/zip-os\n",
        encoding="utf-8",
    )


def install_os_packages(rootfs_dir: Path) -> None:
    chroot(rootfs_dir, "apt-get update")
    chroot(
        rootfs_dir,
        "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends bash ca-certificates linux-image-amd64 initramfs-tools grub-pc-bin grub-efi-amd64-bin net-tools",
    )
    chroot(rootfs_dir, "update-initramfs -u -k all || true")


def configure_grub(rootfs_dir: Path) -> None:
    kernel_candidates = sorted((rootfs_dir / "boot").glob("vmlinuz-*"), key=lambda p: p.stat().st_mtime)
    initrd_candidates = sorted((rootfs_dir / "boot").glob("initrd.img-*"), key=lambda p: p.stat().st_mtime)
    if not kernel_candidates or not initrd_candidates:
        raise RuntimeError("Kernel and initrd were not created inside the Debian rootfs.")

    grub_dir = rootfs_dir / "boot" / "grub"
    grub_dir.mkdir(parents=True, exist_ok=True)
    grub_cfg = grub_dir / "grub.cfg"
    grub_cfg.write_text(
        "set timeout=0\n"
        "set default=0\n"
        "menuentry 'ZIP OS' {\n"
        f"    linux /boot/{kernel_candidates[-1].name} root=/dev/sda1 ro console=ttyS0\n"
        f"    initrd /boot/{initrd_candidates[-1].name}\n"
        "}\n",
        encoding="utf-8",
    )


def preflight_environment() -> tuple[bool, str]:
    if os.geteuid() != 0:
        return False, "This script must run as root because it uses debootstrap, chroot, and bind mounts."

    for required in ("mount", "chroot", "debootstrap", "grub-mkrescue"):
        if resolve_tool(required) is None:
            return False, f"Required tool '{required}' is missing from PATH."

    for stage in (Path("/var/tmp"), Path("/tmp"), Path.cwd()):
        if stage.exists() and os.access(stage, os.W_OK | os.X_OK):
            target_dir = stage
            break
    else:
        target_dir = None

    if target_dir is None:
        return False, "No writable, executable staging directory was found."

    finder = shutil.which("findmnt")
    if finder is not None:
        result = subprocess.run(
            [finder, "-T", str(target_dir), "-o", "OPTIONS", "--raw"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            options = result.stdout.strip().lower()
            if "noexec" in options or "nodev" in options:
                return False, (
                    "The host filesystem is mounted with noexec/nodev restrictions, which blocks debootstrap, "
                    "chroot, and bootloader setup."
                )

    return True, ""


def build_iso_from_zip(zip_path: Path, output_iso: Path) -> None:
    if not zip_path.exists() or zip_path.is_dir():
        raise FileNotFoundError(f"Input archive not found or is not a file: {zip_path}")

    output_iso.parent.mkdir(parents=True, exist_ok=True)
    can_build, reason = preflight_environment()
    if not can_build:
        raise RuntimeError(
            "The host filesystem blocks full OS ISO creation. "
            f"{reason} "
            "Use a normal Linux VM or host with root access, a writable exec-capable temp directory, "
            "and a system that allows debootstrap/chroot/mount."
        )

    debootstrap_path, grub_mkrescue = ensure_iso_tools()

    try:
        with tempfile.TemporaryDirectory(prefix="zip_to_os_", dir=str(stage_root)) as tmp:
            tmp_dir = Path(tmp)
            payload_dir = extract_zip(zip_path, tmp_dir)
            rootfs_dir = tmp_dir / "rootfs"
            bootstrap_rootfs(rootfs_dir, debootstrap_path)
            prepare_rootfs(rootfs_dir, payload_dir)
            install_os_packages(rootfs_dir)
            configure_grub(rootfs_dir)
            run_cmd([grub_mkrescue, "-o", str(output_iso), str(rootfs_dir)])
        print(f"[{VERSION}] Created full bootable Debian-style OS ISO: {output_iso}")
    except Exception as exc:
        message = str(exc)
        if "noexec" in message or "nodev" in message or "Operation not permitted" in message:
            raise RuntimeError(
                "The host filesystem blocks full OS ISO creation. "
                "This usually means the environment is running in a restricted container or VM with no root/mount access. "
                "Use a normal Linux host or VM with root privileges and a writable exec-capable temp directory."
            ) from exc
        raise


def convert_zip_to_iso(zip_path: Path, output_iso: Path) -> None:
    if zip_path.suffix.lower() != ".zip":
        raise ValueError("Input file must be a .zip archive.")
    if output_iso.suffix.lower() != ".iso":
        raise ValueError("Output file must end with .iso")
    build_iso_from_zip(zip_path, output_iso)


def run_gui() -> int:
    if tk is None or filedialog is None or messagebox is None:
        print("Tkinter is not available in this environment.")
        return 1

    root = tk.Tk()
    root.withdraw()

    zip_path = filedialog.askopenfilename(
        title="Select ZIP file",
        filetypes=[("ZIP Archives", "*.zip"), ("All Files", "*.*")],
    )
    if not zip_path:
        root.destroy()
        return 0

    output_iso = filedialog.asksaveasfilename(
        title="Save ISO file",
        defaultextension=".iso",
        filetypes=[("ISO Files", "*.iso"), ("All Files", "*.*")],
    )
    if not output_iso:
        root.destroy()
        return 0

    if not output_iso.lower().endswith(".iso"):
        output_iso = f"{output_iso}.iso"

    try:
        convert_zip_to_iso(Path(zip_path), Path(output_iso))
        messagebox.showinfo(
            "Success",
            f"{VERSION}: a Debian-based bootable OS ISO was created from the ZIP contents.\n\n"
            "The extracted content is placed in /root/zip-os inside the generated system.",
        )
        root.destroy()
        return 0
    except Exception as exc:
        messagebox.showerror("Conversion failed", str(exc))
        root.destroy()
        return 1


def main() -> int:
    if len(sys.argv) == 1:
        return run_gui()
    if len(sys.argv) != 3:
        print("Usage: python3 zip_to_iso.py input.zip output.iso")
        return 1

    zip_path = Path(sys.argv[1]).expanduser().resolve()
    output_iso = Path(sys.argv[2]).expanduser().resolve()

    try:
        convert_zip_to_iso(zip_path, output_iso)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}")
        return 1
    except Exception as exc:
        print(f"Conversion failed: {exc}")
        return 1

    print(f"[{VERSION}] Created ISO: {output_iso}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
