"""Standalone first-boot helper; also copied into cloud-init's write_files."""

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path


SWAP_FILE = Path("/var/lib/devbox/swapfile")
FSTAB = Path("/etc/fstab")
MIB = 1024 * 1024
DISK_RESERVE = 1024 * MIB


def command(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def validate_file(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1:
        raise RuntimeError(f"refusing unsafe swap file: {path}")


def persist(path: Path, fstab: Path) -> None:
    """Preserve existing entries and never add a duplicate or conflicting mount."""
    content = fstab.read_text()
    for line in content.splitlines():
        fields = line.split()
        if fields and fields[0] == str(path):
            if len(fields) < 4 or fields[2] != "swap" or "noauto" in fields[3].split(","):
                raise RuntimeError(f"conflicting fstab entry for {path}")
            return
    with fstab.open("a") as stream:
        if content and not content.endswith("\n"):
            stream.write("\n")
        stream.write(f"{path} none swap sw 0 0\n")
        stream.flush()
        os.fsync(stream.fileno())


def configure_swap(size_mib: int, path: Path = SWAP_FILE, fstab: Path = FSTAB) -> str:
    if type(size_mib) is not int or size_mib < 0:
        raise ValueError("swap_size_mib must be a non-negative whole number")
    if size_mib == 0:
        return "Swap setup disabled; existing swap is unchanged"

    active = command("swapon", "--show=NAME", "--noheadings", "--raw").splitlines()
    if active:
        # Repair persistence if a previous invocation enabled our file before finishing.
        if str(path) in active:
            validate_file(path)
            persist(path, fstab)
        return "Reusing active swap; no new swap file created"

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() or path.is_symlink():
        validate_file(path)
        if command("blkid", "-p", "-s", "TYPE", "-o", "value", str(path)) != "swap":
            raise RuntimeError(f"refusing to overwrite non-swap file: {path}")
        # Reuse an inactive swap file, including one restored from a snapshot. Never resize it.
        os.chmod(path, 0o600)
    else:
        size = size_mib * MIB
        if shutil.disk_usage(path.parent).free < size + DISK_RESERVE:
            raise RuntimeError("not enough disk space for swap plus 1 GiB reserve")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                # Fully allocate on disk; a sparse truncate-only file cannot be used as swap.
                os.posix_fallocate(stream.fileno(), 0, size)
                stream.flush()
                os.fsync(stream.fileno())
            command("mkswap", str(path))
        except BaseException:
            # Only this invocation's newly-created file may be removed, never an existing one.
            path.unlink()
            raise

    # Fail visibly during bootstrap if activation is unsupported; don't claim success.
    command("swapon", str(path))
    persist(path, fstab)
    names = command("swapon", "--show=NAME", "--noheadings", "--raw").splitlines()
    if str(path) not in names:
        raise RuntimeError(f"swap activation could not be verified: {path}")
    return f"Swap active and persistent: {path} ({path.stat().st_size // MIB} MiB)"


if __name__ == "__main__":
    print(configure_swap(int(sys.argv[1])))
