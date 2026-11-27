from __future__ import annotations

import shlex
import os
import stat as stat_module
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .archive_upload import (
    ArchiveCancelled,
    ArchiveFolderExpectation,
    UploadArchivePlan,
    analyze_upload_tree,
    archive_expectations,
)
from .file_scan import CancelSignal, FolderScan
from .paths import app_data_dir, is_link_or_junction, is_windows_system_folder, path_for_rsync
from .profiles import Profile
from .receipts import RECEIPT_DIR_NAME
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
                        # Transfer receipts are managed by the app, not synced.
                        if entry.name != RECEIPT_DIR_NAME and not is_windows_system_folder(entry.name):
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


@dataclass
class LocalSyncScan:
    """Local side of a Synchronise comparison, from one scan of the folder."""

    records: dict[str, FileRecord]
    archives: list[ArchiveFolderExpectation]
    plan: UploadArchivePlan | None
    cancelled: bool = False


def scan_local_for_sync(
    local_folder: str | Path,
    cancel_event: CancelSignal | None = None,
    progress_callback: Callable[[FolderScan], None] | None = None,
) -> LocalSyncScan:
    """List local files and the automatic archives an upload would create.

    One scan feeds both, so the comparison uses exactly the folders and
    archive members that a packaged upload would use.
    """
    root = Path(local_folder).expanduser().resolve()
    records: dict[str, FileRecord] = {}

    def add_file(snapshot) -> None:
        relative = snapshot.path.relative_to(root).as_posix()
        records[relative] = FileRecord(relative, snapshot.size, snapshot.modified_ns / 1_000_000_000)

    try:
        plan = analyze_upload_tree(root, cancel_event, progress_callback, file_callback=add_file)
        archives = archive_expectations(plan, cancel_event)
    except ArchiveCancelled:
        return LocalSyncScan(records, [], None, cancelled=True)
    return LocalSyncScan(records, archives, plan)


def build_remote_manifest_command(
    profile: Profile,
    remote_path: str,
    ssh_path: str | None = None,
    batch_mode: bool = True,
) -> list[str]:
    clean_path = _clean_remote_path(remote_path)
    # Skip the root-owned, unreadable QRIScloud metadata folder in each
    # collection root (it would make find fail) and the app's own receipts.
    remote_command = (
        f"cd {shlex.quote(clean_path)} && find . "
        f"\\( -path ./.afm -o -name {RECEIPT_DIR_NAME} \\) -prune -o "
        "-type f -printf '%P\\t%s\\t%T@\\n'"
    )
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

    MAX_EXAMPLES = 3

    def __init__(self) -> None:
        self.records: dict[str, FileRecord] = {}
        self.malformed_line_count = 0
        self.malformed_examples: list[str] = []

    def feed_line(self, raw_line: str) -> FileRecord | None:
        record = parse_remote_manifest_line(raw_line)
        if record is not None:
            self.records[record.relative_path] = record
        elif raw_line.strip() and not _is_ssh_notice(raw_line):
            self.malformed_line_count += 1
            if len(self.malformed_examples) < self.MAX_EXAMPLES:
                self.malformed_examples.append(raw_line.strip()[:200])
        return record


def _is_ssh_notice(raw_line: str) -> bool:
    """OpenSSH prints multi-line notices on stderr, every line starting "** ".

    OpenSSH 10 warns about post-quantum key exchange in three such lines. A
    real manifest line always has two tab-separated fields after the name, so
    these can never be mistaken for one.
    """
    return raw_line.strip().startswith("**")


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


# --- Synchronise: side-by-side comparison --------------------------------------

UPLOAD = "upload"
DOWNLOAD = "download"

MODE_BOTH = "both"
MODE_UPLOAD = "upload"
MODE_DOWNLOAD = "download"


class SyncStatus(str, Enum):
    LOCAL_ONLY = "Only on this computer"
    REMOTE_ONLY = "Only on QRIScloud"
    LOCAL_NEWER = "Newer on this computer"
    REMOTE_NEWER = "Newer on QRIScloud"
    CONFLICT = "Different size, same time"
    ARCHIVE_NEW = "Folder to archive"
    ARCHIVE_CHANGED = "Archive out of date"
    REMOTE_ARCHIVE = "Archive only on QRIScloud"


@dataclass
class SyncItem:
    """One row of the Synchronise window.

    A normal row is one file. An archive row stands for a local folder that an
    upload packs into ``folder.tar`` (see ``archive_upload``); its member
    files are never listed separately.
    """

    path: str
    status: SyncStatus
    local: FileRecord | None = None
    remote: FileRecord | None = None
    archive: ArchiveFolderExpectation | None = None
    direction: str = UPLOAD
    checked: bool = False

    @property
    def is_archive(self) -> bool:
        return self.archive is not None

    @property
    def allowed_directions(self) -> tuple[str, ...]:
        if self.status in (SyncStatus.LOCAL_ONLY, SyncStatus.ARCHIVE_NEW, SyncStatus.ARCHIVE_CHANGED):
            return (UPLOAD,)
        if self.status in (SyncStatus.REMOTE_ONLY, SyncStatus.REMOTE_ARCHIVE):
            return (DOWNLOAD,)
        return (UPLOAD, DOWNLOAD)

    @property
    def replaces_existing(self) -> bool:
        """True when the transfer overwrites a file that exists on the other side."""
        return self.local is not None and self.remote is not None

    @property
    def replaces_newer(self) -> bool:
        """True when the chosen direction overwrites the newer copy."""
        return (self.direction == UPLOAD and self.status == SyncStatus.REMOTE_NEWER) or (
            self.direction == DOWNLOAD and self.status == SyncStatus.LOCAL_NEWER
        )

    def transfer_bytes(self) -> int:
        if self.archive is not None:
            return self.archive.member_bytes
        record = self.local if self.direction == UPLOAD else self.remote
        return record.size if record is not None else 0

    def set_direction(self, direction: str) -> None:
        if direction not in self.allowed_directions:
            raise ValueError(f"{self.path} cannot be transferred in direction {direction}.")
        self.direction = direction


@dataclass
class SyncPlan:
    items: list[SyncItem]
    identical_files: int = 0
    identical_archives: int = 0

    def apply_mode(self, mode: str) -> None:
        """Reset every row to the default tick and direction for *mode*."""
        for item in self.items:
            item.direction, item.checked = default_action(item.status, mode)

    def chosen(self, direction: str) -> list[SyncItem]:
        return [item for item in self.items if item.checked and item.direction == direction]


def default_action(status: SyncStatus, mode: str) -> tuple[str, bool]:
    """Return (direction, ticked) for a row, like WinSCP's synchronise defaults.

    The newer copy wins. Rows that would replace a newer file, conflicts, and
    archives that exist only on QRIScloud start unticked.
    """
    uploads = mode in (MODE_BOTH, MODE_UPLOAD)
    downloads = mode in (MODE_BOTH, MODE_DOWNLOAD)
    if status in (SyncStatus.LOCAL_ONLY, SyncStatus.ARCHIVE_NEW, SyncStatus.ARCHIVE_CHANGED):
        return UPLOAD, uploads
    if status == SyncStatus.REMOTE_ONLY:
        return DOWNLOAD, downloads
    if status == SyncStatus.REMOTE_ARCHIVE:
        return DOWNLOAD, False
    if status == SyncStatus.LOCAL_NEWER:
        return (UPLOAD, True) if uploads else (DOWNLOAD, False)
    if status == SyncStatus.REMOTE_NEWER:
        return (DOWNLOAD, True) if downloads else (UPLOAD, False)
    # Same time, different size: no safe default, so let the user decide.
    return (DOWNLOAD if mode == MODE_DOWNLOAD else UPLOAD), False


def build_sync_plan(
    local: dict[str, FileRecord],
    remote: dict[str, FileRecord],
    archives: Iterable[ArchiveFolderExpectation] = (),
    mode: str = MODE_BOTH,
    mtime_tolerance_seconds: float = MTIME_TOLERANCE_SECONDS,
) -> SyncPlan:
    """Compare two file lists, treating automatic-archive folders as one row each."""
    local = {path: record for path, record in local.items() if not _is_receipt_path(path)}
    remote = {path: record for path, record in remote.items() if not _is_receipt_path(path)}
    items: list[SyncItem] = []
    identical_files = 0
    identical_archives = 0
    archived_members: set[str] = set()
    archive_files: set[str] = set()

    for archive in archives:
        prefix = f"{archive.relative_folder}/" if archive.relative_folder else ""
        archived_members.update(archive.member_paths)
        present = 0
        matching = 0
        for part in archive.parts:
            tar_path = prefix + part.archive_name
            inventory_path = prefix + part.inventory_name
            archive_files.update((tar_path, inventory_path))
            tar = remote.get(tar_path)
            inventory = remote.get(inventory_path)
            if tar is not None:
                present += 1
            if (
                tar is not None
                and inventory is not None
                and inventory.size == part.inventory_size
                and abs(inventory.modified_timestamp - part.modified) <= mtime_tolerance_seconds
            ):
                matching += 1
        if matching == len(archive.parts):
            identical_archives += 1
            continue
        status = SyncStatus.ARCHIVE_CHANGED if present else SyncStatus.ARCHIVE_NEW
        items.append(SyncItem(path=archive.relative_folder, status=status, archive=archive))

    for path in sorted(set(local) | set(remote), key=str.casefold):
        if path in archived_members or path in archive_files:
            continue
        local_record = local.get(path)
        remote_record = remote.get(path)
        if local_record is not None and remote_record is not None:
            same_time = abs(local_record.modified_timestamp - remote_record.modified_timestamp) <= mtime_tolerance_seconds
            if same_time and local_record.size == remote_record.size:
                identical_files += 1
                continue
            if same_time:
                status = SyncStatus.CONFLICT
            elif local_record.modified_timestamp > remote_record.modified_timestamp:
                status = SyncStatus.LOCAL_NEWER
            else:
                status = SyncStatus.REMOTE_NEWER
        elif local_record is not None:
            status = SyncStatus.LOCAL_ONLY
        else:
            status = SyncStatus.REMOTE_ARCHIVE if _is_archive_file(path) else SyncStatus.REMOTE_ONLY
        items.append(SyncItem(path=path, status=status, local=local_record, remote=remote_record))

    plan = SyncPlan(items, identical_files, identical_archives)
    plan.apply_mode(mode)
    return plan


def _is_receipt_path(path: str) -> bool:
    return RECEIPT_DIR_NAME in path.split("/")


def _is_archive_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].casefold()
    return name.endswith(".tar") or name.endswith(".tar.inventory.txt")


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
    return is_link_or_junction(stat)


def _manifest_cancelled(cancel_event: CancelSignal | None) -> bool:
    return bool(cancel_event and cancel_event.is_set())


def _report_manifest_progress(callback: ManifestProgressCallback | None, progress: ManifestScanProgress) -> None:
    if callback is not None:
        callback(progress)
