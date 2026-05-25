from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from .paths import app_data_dir, path_for_rsync
from .profiles import Profile
from .remote_dirs import build_remote_ssh_base


MTIME_TOLERANCE_SECONDS = 2.0


@dataclass
class FileRecord:
    relative_path: str
    size: int
    modified_timestamp: float


@dataclass
class SyncSelection:
    missing: list[FileRecord]
    changed: list[FileRecord]

    @property
    def selected(self) -> list[FileRecord]:
        return [*self.missing, *self.changed]


def scan_local_manifest(local_folder: str | Path) -> dict[str, FileRecord]:
    root = Path(local_folder).expanduser()
    records: dict[str, FileRecord] = {}
    for path in root.rglob("*"):
        try:
            if not path.is_file():
                continue
            stat = path.stat()
        except OSError:
            continue
        relative_path = path.relative_to(root).as_posix()
        records[relative_path] = FileRecord(relative_path, stat.st_size, stat.st_mtime)
    return records


def build_remote_manifest_command(
    profile: Profile,
    remote_path: str,
    ssh_path: str | None = None,
    batch_mode: bool = True,
) -> list[str]:
    clean_path = _clean_remote_path(remote_path)
    remote_command = f"cd {shlex.quote(clean_path)} && find . -type f -printf '%P\\t%s\\t%T@\\n'"
    return [*build_remote_ssh_base(profile, ssh_path=ssh_path, batch_mode=batch_mode), remote_command]


def parse_remote_manifest(output: str) -> dict[str, FileRecord]:
    records: dict[str, FileRecord] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("** WARNING:"):
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        relative_path, size_text, modified_text = parts
        try:
            size = int(size_text)
            modified = float(modified_text)
        except ValueError:
            continue
        records[relative_path] = FileRecord(relative_path, size, modified)
    return records


def compare_manifests(
    local: dict[str, FileRecord],
    remote: dict[str, FileRecord],
    mtime_tolerance_seconds: float = MTIME_TOLERANCE_SECONDS,
) -> SyncSelection:
    missing: list[FileRecord] = []
    changed: list[FileRecord] = []
    for relative_path in sorted(local):
        local_record = local[relative_path]
        remote_record = remote.get(relative_path)
        if remote_record is None:
            missing.append(local_record)
            continue
        if local_record.size != remote_record.size:
            changed.append(local_record)
            continue
        if abs(local_record.modified_timestamp - remote_record.modified_timestamp) > mtime_tolerance_seconds:
            changed.append(local_record)
    return SyncSelection(missing=missing, changed=changed)


def write_files_from(records: list[FileRecord], name: str = "sync_selection") -> Path:
    path = app_data_dir() / "filelists" / f"{name}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [record.relative_path for record in records]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8", newline="\n")
    return path


def files_from_argument(path: str | Path, rsync_path: str) -> str:
    return f"--files-from={path_for_rsync(path, rsync_path)}"


def _clean_remote_path(remote_path: str) -> str:
    clean = remote_path.strip()
    if not clean:
        raise ValueError("Remote path is required.")
    if not clean.startswith("/"):
        raise ValueError("Remote path must start with '/'.")
    return clean.rstrip("/") or "/"
