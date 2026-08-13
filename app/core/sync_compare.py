from __future__ import annotations

import shlex
import os
import stat as stat_module
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .file_scan import CancelSignal
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
class ManifestScanProgress:
    """Live progress from :func:`scan_local_manifest`.

    The manifest returned by the scanner remains a plain dictionary for
    backwards compatibility.  This separate object lets callers update a UI
    or stop a scan without retaining directory entries or partial manifests.
    """

    files_scanned: int = 0
    directories_scanned: int = 0
    skipped_errors: int = 0
    cancelled: bool = False


ManifestProgressCallback = Callable[[ManifestScanProgress], None]


@dataclass
class SyncSelection:
    missing: list[FileRecord]
    changed: list[FileRecord]

    @property
    def selected(self) -> list[FileRecord]:
        return [*self.missing, *self.changed]


def scan_local_manifest(
    local_folder: str | Path,
    cancel_event: CancelSignal | None = None,
    progress_callback: ManifestProgressCallback | None = None,
) -> dict[str, FileRecord]:
    """Return regular files below *local_folder*, without following links.

    Cancellation returns the manifest collected so far. ``progress_callback``
    receives a live :class:`ManifestScanProgress` after each scanned entry.
    Directory traversal uses ``scandir`` and a small pending stack, rather
    than materialising the directory tree before scanning it.
    """
    root = Path(local_folder).expanduser()
    records: dict[str, FileRecord] = {}
    progress = ManifestScanProgress()
    if _manifest_cancelled(cancel_event):
        progress.cancelled = True
        _report_manifest_progress(progress_callback, progress)
        return records

    try:
        root_stat = root.stat(follow_symlinks=False)
    except (OSError, TypeError):
        progress.skipped_errors += 1
        _report_manifest_progress(progress_callback, progress)
        return records

    if _is_reparse_point(root_stat) or not stat_module.S_ISDIR(root_stat.st_mode):
        return records

    pending = [root]
    while pending:
        if _manifest_cancelled(cancel_event):
            progress.cancelled = True
            _report_manifest_progress(progress_callback, progress)
            break
        directory = pending.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            progress.skipped_errors += 1
            _report_manifest_progress(progress_callback, progress)
            continue
        progress.directories_scanned += 1
        with entries:
            for entry in entries:
                if _manifest_cancelled(cancel_event):
                    progress.cancelled = True
                    _report_manifest_progress(progress_callback, progress)
                    break
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                    if _is_reparse_point(entry_stat):
                        pass
                    elif stat_module.S_ISDIR(entry_stat.st_mode):
                        pending.append(Path(entry.path))
                    elif stat_module.S_ISREG(entry_stat.st_mode):
                        relative_path = Path(entry.path).relative_to(root).as_posix()
                        records[relative_path] = FileRecord(
                            relative_path, entry_stat.st_size, entry_stat.st_mtime
                        )
                        progress.files_scanned += 1
                except OSError:
                    progress.skipped_errors += 1
                _report_manifest_progress(progress_callback, progress)
        if progress.cancelled:
            break
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
    collector = RemoteManifestCollector()
    for raw_line in output.splitlines():
        collector.feed_line(raw_line)
    return collector.records


def parse_remote_manifest_line(raw_line: str) -> FileRecord | None:
    """Parse one ``find -printf`` output line, ignoring non-manifest output."""
    line = raw_line.strip()
    if not line or line.startswith("** WARNING:"):
        return None
    parts = line.split("\t", 2)
    if len(parts) != 3:
        return None
    relative_path, size_text, modified_text = parts
    try:
        return FileRecord(relative_path, int(size_text), float(modified_text))
    except ValueError:
        return None


class RemoteManifestCollector:
    """Incrementally collect remote-manifest records from process stdout."""

    def __init__(self) -> None:
        self.records: dict[str, FileRecord] = {}
        self.malformed_line_count = 0

    def feed_line(self, raw_line: str) -> FileRecord | None:
        record = parse_remote_manifest_line(raw_line)
        if record is not None:
            self.records[record.relative_path] = record
        elif raw_line.strip() and not raw_line.strip().startswith("** WARNING:"):
            self.malformed_line_count += 1
        return record


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
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            temporary_path = Path(handle.name)
            for record in records:
                handle.write(record.relative_path)
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()
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


def _is_reparse_point(stat: os.stat_result) -> bool:
    return bool(getattr(stat, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _manifest_cancelled(cancel_event: CancelSignal | None) -> bool:
    return bool(cancel_event and cancel_event.is_set())


def _report_manifest_progress(callback: ManifestProgressCallback | None, progress: ManifestScanProgress) -> None:
    if callback is not None:
        callback(progress)
