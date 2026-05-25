from __future__ import annotations

import os
import re
from pathlib import Path


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


def path_for_rsync(path: str | Path, rsync_path: str | Path | None = None) -> str:
    if is_msys2_executable(rsync_path):
        return windows_path_to_msys(path)
    return str(Path(path).expanduser())


def directory_source_for_rsync(path: str | Path, rsync_path: str | Path | None = None) -> str:
    source = path_for_rsync(path, rsync_path)
    return source.rstrip("/\\") + "/"

