from __future__ import annotations

import os
import re
import shutil
import stat as stat_module
from pathlib import Path

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
# Reparse tags with this bit point at another named file or folder: symlinks,
# junctions and mount points. Others (Windows Server deduplication 0x80000013,
# cloud placeholders, HSM stubs) are ordinary files that read normally.
REPARSE_TAG_NAME_SURROGATE = 0x20000000
# Folders Windows file servers keep for themselves. They are unreadable to
# users and never part of a researcher's data, so uploads leave them out.
WINDOWS_SYSTEM_FOLDER_NAMES = ("DfsrPrivate", "System Volume Information", "$RECYCLE.BIN")
_WINDOWS_SYSTEM_FOLDER_KEYS = frozenset(name.casefold() for name in WINDOWS_SYSTEM_FOLDER_NAMES)


def is_link_or_junction(info: os.stat_result) -> bool:
    """True for symlinks, junctions and mount points; False for ordinary files.

    Checked live on a JCU file share: ``os.scandir`` reports deduplicated files
    with the reparse-point attribute and tag 0x80000013, while ``os.stat``
    reports the same file without it. Both must count as a regular file.
    """
    if not getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
        return stat_module.S_ISLNK(info.st_mode)
    tag = getattr(info, "st_reparse_tag", 0)
    # An unknown tag is treated as a link, the cautious choice.
    return not tag or bool(tag & REPARSE_TAG_NAME_SURROGATE)


# Hidden folders the app builds archives in (".<folder>.qris-upload-xxxx").
APP_WORK_FOLDER_MARKER = ".qris-upload-"
APP_WORK_FOLDER_PATTERN = f".*{APP_WORK_FOLDER_MARKER}*"


def is_windows_system_folder(name: str) -> bool:
    return name.casefold() in _WINDOWS_SYSTEM_FOLDER_KEYS


def is_app_work_folder(name: str) -> bool:
    return name.startswith(".") and APP_WORK_FOLDER_MARKER in name


def is_excluded_folder_name(name: str) -> bool:
    """Folders never scanned or uploaded: file-server system folders and the
    app's own temporary archive folders (which may sit inside the source)."""
    return is_windows_system_folder(name) or is_app_work_folder(name)


APP_DIR_NAME = "QRISRsyncManager"
MSYS2_RSYNC_PATH = Path(r"C:\msys64\usr\bin\rsync.exe")
MSYS2_SSH_PATH = Path(r"C:\msys64\usr\bin\ssh.exe")


def app_data_dir() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    path = base / APP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def profiles_path() -> Path:
    return app_data_dir() / "profiles.json"


def logs_dir() -> Path:
    path = app_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ssh_known_hosts_path() -> Path:
    """Return the app-owned, per-Windows-user SSH host key store."""
    return app_data_dir() / "known_hosts"


def is_executable_file(path: str | Path | None) -> bool:
    if not path:
        return False
    candidate = Path(path).expanduser()
    return candidate.is_file()


def detect_rsync() -> str:
    candidates = [
        Path.cwd() / "tools" / "rsync.exe",
        MSYS2_RSYNC_PATH,
        Path(r"C:\Program Files\Git\usr\bin\rsync.exe"),
        Path(r"C:\Program Files (x86)\Git\usr\bin\rsync.exe"),
    ]
    for candidate in candidates:
        if is_executable_file(candidate):
            return str(candidate)
    return str(MSYS2_RSYNC_PATH)


def detect_ssh() -> str:
    candidates = [
        MSYS2_SSH_PATH,
        Path(r"C:\Program Files\Git\usr\bin\ssh.exe"),
        Path(r"C:\Program Files\OpenSSH\ssh.exe"),
    ]
    for candidate in candidates:
        if is_executable_file(candidate):
            return str(candidate)
    return str(MSYS2_SSH_PATH)


def detect_tar() -> str:
    """Prefer Windows' native libarchive tar for high-throughput packaging."""
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    candidates = [
        system_root / "System32" / "tar.exe",
        Path.cwd() / "tools" / "tar.exe",
        Path(r"C:\msys64\usr\bin\tar.exe"),
    ]
    for candidate in candidates:
        if is_executable_file(candidate):
            return str(candidate)
    discovered = shutil.which("tar.exe") or shutil.which("tar")
    return discovered or str(candidates[0])


def is_msys2_executable(path: str | Path | None) -> bool:
    if not path:
        return False
    normalized = str(path).replace("/", "\\").lower()
    return "\\msys64\\usr\\bin\\" in normalized


def windows_path_to_msys(path: str | Path) -> str:
    raw = str(Path(path).expanduser())
    raw = raw.replace("\\", "/")
    drive_match = re.match(r"^([A-Za-z]):/(.*)$", raw)
    if drive_match:
        drive = drive_match.group(1).lower()
        rest = drive_match.group(2)
        return f"/{drive}/{rest}"
    if raw.startswith("//"):
        return raw
    return raw


def path_for_ssh(path: str | Path, ssh_path: str | Path | None = None) -> str:
    if is_msys2_executable(ssh_path):
        return windows_path_to_msys(path)
    return str(Path(path).expanduser())


def ssh_host_key_options(ssh_path: str | Path | None = None) -> list[str]:
    """Trust new hosts once while continuing to reject changed host keys."""
    executable = ssh_path or detect_ssh()
    known_hosts = path_for_ssh(ssh_known_hosts_path(), executable)
    return [
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
    ]


def path_for_rsync(path: str | Path, rsync_path: str | Path | None = None) -> str:
    if is_msys2_executable(rsync_path):
        return windows_path_to_msys(path)
    return str(Path(path).expanduser())


def directory_source_for_rsync(path: str | Path, rsync_path: str | Path | None = None) -> str:
    source = path_for_rsync(path, rsync_path)
    return source.rstrip("/\\") + "/"


def file_source_for_rsync(path: str | Path, rsync_path: str | Path | None = None) -> str:
    return path_for_rsync(path, rsync_path)
