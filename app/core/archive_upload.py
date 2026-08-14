"""Plan and build safe media archives for large QRIScloud uploads."""

from __future__ import annotations

import os
import shutil
import stat as stat_module
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Protocol

from .file_scan import FolderScan
from .paths import detect_tar, is_executable_file


ARCHIVE_FOLDER_BYTES = 10_000_000_000
ARCHIVE_FLAT_FILE_COUNT = 200
NATIVE_TAR_BLOCKING_FACTOR = 2048
NATIVE_TAR_POLL_SECONDS = 0.2
NATIVE_TAR_STOP_SECONDS = 5.0
NATIVE_TAR_COMMAND_LIMIT = 30_000

PHOTO_EXTENSIONS = frozenset(
    {
        ".arw", ".bmp", ".cr2", ".cr3", ".dng", ".gif", ".heic", ".heif",
        ".jpeg", ".jpg", ".nef", ".orf", ".png", ".raw", ".rw2", ".tif", ".tiff",
    }
)
VIDEO_EXTENSIONS = frozenset(
    {
        ".avi", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg",
        ".mts", ".webm", ".wmv",
    }
)


class CancelSignal(Protocol):
    def is_set(self) -> bool: ...


class ArchiveCancelled(RuntimeError):
    """Raised when archive analysis or construction is cancelled."""


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    size: int
    modified_ns: int


@dataclass(frozen=True)
class ArchiveGroupPlan:
    folder: Path
    relative_folder: str
    category: str
    media_count: int
    member_count: int
    member_bytes: int
    archive_name: str
    inventory_name: str


@dataclass(frozen=True)
class EligibleFolderPlan:
    folder: Path
    relative_folder: str
    flat_file_count: int
    flat_file_bytes: int
    snapshot_signature: tuple[tuple[str, int, int], ...]
    groups: tuple[ArchiveGroupPlan, ...]


@dataclass
class UploadArchivePlan:
    root: Path
    scan: FolderScan
    folders: list[EligibleFolderPlan] = field(default_factory=list)

    @property
    def groups(self) -> list[ArchiveGroupPlan]:
        return [group for folder in self.folders for group in folder.groups]

    @property
    def archived_file_count(self) -> int:
        return sum(group.member_count for group in self.groups)

    @property
    def archived_bytes(self) -> int:
        return sum(group.member_bytes for group in self.groups)


@dataclass(frozen=True)
class UploadPackage:
    work_dir: Path
    payload_dir: Path
    exclude_file: Path
    archive_count: int
    inventory_count: int

    def cleanup(self) -> str | None:
        try:
            shutil.rmtree(self.work_dir)
        except FileNotFoundError:
            return None
        except OSError as exc:
            return f"Could not remove temporary archive folder {self.work_dir}: {exc}"
        return None


ProgressCallback = Callable[[FolderScan], None]
BuildProgressCallback = Callable[[str], None]


def analyze_upload_tree(
    root: str | Path,
    cancel_event: CancelSignal | None = None,
    progress_callback: ProgressCallback | None = None,
    *,
    min_folder_bytes: int = ARCHIVE_FOLDER_BYTES,
    min_flat_files: int = ARCHIVE_FLAT_FILE_COUNT,
) -> UploadArchivePlan:
    """Scan *root* and identify directories requiring media archives.

    A directory qualifies when its direct regular files exceed either
    threshold. Subdirectories are evaluated independently.
    """
    source = Path(root).expanduser().resolve()
    _validate_root(source)
    scan = FolderScan()
    plan = UploadArchivePlan(root=source, scan=scan)
    pending = [source]

    while pending:
        _raise_if_cancelled(cancel_event)
        folder = pending.pop()
        snapshots: list[FileSnapshot] = []
        scan.directory_count += 1
        try:
            iterator = os.scandir(folder)
        except OSError:
            scan.skipped_errors += 1
            _report(progress_callback, scan)
            continue
        with iterator:
            for entry in iterator:
                _raise_if_cancelled(cancel_event)
                try:
                    info = entry.stat(follow_symlinks=False)
                    if _is_reparse(info):
                        scan.skipped_errors += 1
                    elif stat_module.S_ISDIR(info.st_mode):
                        pending.append(Path(entry.path))
                    elif stat_module.S_ISREG(info.st_mode):
                        snapshot = FileSnapshot(Path(entry.path), info.st_size, info.st_mtime_ns)
                        snapshots.append(snapshot)
                        scan.file_count += 1
                        scan.total_bytes += info.st_size
                        if info.st_size < 1_048_576:
                            scan.tiny_file_count += 1
                except OSError:
                    scan.skipped_errors += 1
                _report(progress_callback, scan)

        flat_bytes = sum(item.size for item in snapshots)
        if len(snapshots) > min_flat_files or flat_bytes > min_folder_bytes:
            groups = _group_snapshots(source, folder, snapshots)
            if groups:
                plan.folders.append(
                    EligibleFolderPlan(
                        folder=folder,
                        relative_folder=_relative_posix(folder, source),
                        flat_file_count=len(snapshots),
                        flat_file_bytes=flat_bytes,
                        snapshot_signature=_snapshot_signature(snapshots),
                        groups=tuple(groups),
                    )
                )
    plan.folders.sort(key=lambda item: item.relative_folder.casefold())
    return plan


def build_upload_package(
    plan: UploadArchivePlan,
    cancel_event: CancelSignal | None = None,
    progress_callback: BuildProgressCallback | None = None,
    *,
    tar_path: str | Path | None = None,
) -> UploadPackage:
    """Build TAR/inventory payload and an rsync exclusion list.

    Source files are never modified. The temporary work directory is created
    beside the upload root so its free-space characteristics are predictable.
    """
    if not plan.groups:
        raise ValueError("The upload plan contains no archive groups.")
    tar_executable = str(Path(tar_path).expanduser()) if tar_path else detect_tar()
    if not is_executable_file(tar_executable):
        raise FileNotFoundError(
            f"Native tar.exe was not found at {tar_executable}. "
            "Windows Server 2022 normally provides C:\\Windows\\System32\\tar.exe."
        )
    source_parent = plan.root.parent
    if source_parent == plan.root:
        source_parent = Path(tempfile.gettempdir()).resolve()
    required = plan.archived_bytes + max(1_073_741_824, plan.archived_bytes // 20)
    free = shutil.disk_usage(source_parent).free
    if free < required:
        raise OSError(
            f"Not enough temporary disk space beside the upload folder. "
            f"Required about {required:,} bytes; available {free:,} bytes."
        )

    work_dir = Path(tempfile.mkdtemp(prefix=f".{plan.root.name}.qris-upload-", dir=source_parent))
    payload_dir = work_dir / "payload"
    exclude_file = work_dir / "archived-files.exclude"
    payload_dir.mkdir()
    package = UploadPackage(work_dir, payload_dir, exclude_file, 0, 0)
    try:
        with exclude_file.open("w", encoding="utf-8", newline="\n") as exclusions:
            archive_count = 0
            inventory_count = 0
            for folder_plan in plan.folders:
                _raise_if_cancelled(cancel_event)
                snapshots = _snapshot_flat_files(folder_plan.folder, cancel_event)
                if _snapshot_signature(snapshots) != folder_plan.snapshot_signature:
                    raise RuntimeError(
                        f"{folder_plan.folder} changed after it was scanned. Start the upload again so it can be rechecked."
                    )
                current_groups = _group_snapshots(plan.root, folder_plan.folder, snapshots)
                expected = {(group.category, group.media_count, group.member_count, group.member_bytes) for group in folder_plan.groups}
                actual = {(group.category, group.media_count, group.member_count, group.member_bytes) for group in current_groups}
                if actual != expected:
                    raise RuntimeError(
                        f"{folder_plan.folder} changed after it was scanned. Start the upload again so it can be rechecked."
                    )
                member_map = _members_by_category(snapshots)
                for group in folder_plan.groups:
                    members = member_map[group.category]
                    destination = payload_dir / Path(group.relative_folder) if group.relative_folder else payload_dir
                    destination.mkdir(parents=True, exist_ok=True)
                    archive_path = destination / group.archive_name
                    inventory_path = destination / group.inventory_name
                    _emit(progress_callback, f"Creating {group.category} archive: {archive_path.name}")
                    _write_tar(
                        archive_path,
                        folder_plan.folder,
                        members,
                        cancel_event,
                        progress_callback,
                        tar_executable,
                    )
                    _write_inventory(inventory_path, group, members)
                    stable_mtime = max(item.modified_ns for item in members) / 1_000_000_000
                    os.utime(archive_path, (stable_mtime, stable_mtime))
                    os.utime(inventory_path, (stable_mtime, stable_mtime))
                    archive_count += 1
                    inventory_count += 1
                    for member in members:
                        relative = _relative_posix(member.path, plan.root)
                        exclusions.write(f"/{_escape_rsync_pattern(relative)}\n")
        return UploadPackage(work_dir, payload_dir, exclude_file, archive_count, inventory_count)
    except Exception as exc:
        cleanup_error = package.cleanup()
        if cleanup_error:
            raise RuntimeError(f"{exc}\n{cleanup_error}") from exc
        raise


def _group_snapshots(root: Path, folder: Path, snapshots: list[FileSnapshot]) -> list[ArchiveGroupPlan]:
    members = _members_by_category(snapshots)
    groups: list[ArchiveGroupPlan] = []
    relative_folder = _relative_posix(folder, root)
    safe_base = folder.name or root.name or "folder"
    existing_names = {item.path.name.casefold() for item in snapshots}
    media_by_category = _media_by_category(snapshots)
    for category in ("photos", "videos"):
        category_members = members[category]
        media = media_by_category[category]
        if not media:
            continue
        archive_name = f"{safe_base}__{category}.tar"
        inventory_name = f"{archive_name}.inventory.txt"
        if archive_name.casefold() in existing_names or inventory_name.casefold() in existing_names:
            raise ValueError(
                f"Cannot create the automatic archive in {folder}: {archive_name} or its inventory already exists."
            )
        groups.append(
            ArchiveGroupPlan(
                folder=folder,
                relative_folder=relative_folder,
                category=category,
                media_count=len(media),
                member_count=len(category_members),
                member_bytes=sum(item.size for item in category_members),
                archive_name=archive_name,
                inventory_name=inventory_name,
            )
        )
    return groups


def _media_by_category(snapshots: list[FileSnapshot]) -> dict[str, list[FileSnapshot]]:
    result = {"photos": [], "videos": []}
    for item in snapshots:
        suffix = item.path.suffix.casefold()
        if suffix in PHOTO_EXTENSIONS:
            result["photos"].append(item)
        elif suffix in VIDEO_EXTENSIONS:
            result["videos"].append(item)
    return result


def _members_by_category(snapshots: list[FileSnapshot]) -> dict[str, list[FileSnapshot]]:
    media = _media_by_category(snapshots)
    assigned: dict[str, str] = {}
    ambiguous: set[str] = set()
    lookup: dict[str, FileSnapshot] = {item.path.name.casefold(): item for item in snapshots}
    for category, items in media.items():
        for item in items:
            assigned[item.path.name.casefold()] = category
    media_names = [(item.path.name.casefold(), item.path.stem.casefold(), category) for category, items in media.items() for item in items]
    for candidate in snapshots:
        key = candidate.path.name.casefold()
        if key in assigned:
            continue
        matches = {
            category
            for full_name, stem, category in media_names
            if candidate.path.stem.casefold() == stem or key.startswith(full_name + ".")
        }
        if len(matches) == 1:
            assigned[key] = next(iter(matches))
        elif len(matches) > 1:
            ambiguous.add(key)
    result = {"photos": [], "videos": []}
    for key, category in assigned.items():
        if key not in ambiguous:
            result[category].append(lookup[key])
    for items in result.values():
        items.sort(key=lambda item: item.path.name.casefold())
    return result


def _snapshot_flat_files(folder: Path, cancel_event: CancelSignal | None) -> list[FileSnapshot]:
    snapshots: list[FileSnapshot] = []
    with os.scandir(folder) as entries:
        for entry in entries:
            _raise_if_cancelled(cancel_event)
            info = entry.stat(follow_symlinks=False)
            if not _is_reparse(info) and stat_module.S_ISREG(info.st_mode):
                snapshots.append(FileSnapshot(Path(entry.path), info.st_size, info.st_mtime_ns))
    return snapshots


def _write_tar(
    destination: Path,
    source_folder: Path,
    members: list[FileSnapshot],
    cancel_event: CancelSignal | None,
    progress_callback: BuildProgressCallback | None,
    tar_path: str,
) -> None:
    for member in members:
        _raise_if_cancelled(cancel_event)
        _validate_archive_member(member)
    commands = _native_tar_commands(tar_path, destination, source_folder, members)
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
    process: subprocess.Popen[str] | None = None
    try:
        last_report = 0.0
        last_percent = -5
        total_bytes = max(1, sum(member.size for member in members))
        for batch_number, command in enumerate(commands, start=1):
            _raise_if_cancelled(cancel_event)
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                creationflags=creationflags,
            )
            if batch_number == 1:
                _emit(
                    progress_callback,
                    f"Native tar.exe started with a "
                    f"{NATIVE_TAR_BLOCKING_FACTOR * 512 // 1_048_576} MiB I/O buffer "
                    f"for {len(members):,} files",
                )
            while True:
                try:
                    output, _ = process.communicate(timeout=NATIVE_TAR_POLL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    if cancel_event and cancel_event.is_set():
                        _stop_native_tar(process)
                        raise ArchiveCancelled("Archive preparation was cancelled.")
                    now = time.monotonic()
                    if now - last_report >= 2.0:
                        last_report = now
                        written = destination.stat().st_size if destination.exists() else 0
                        percent = min(99, int(written * 100 / total_bytes))
                        if percent >= last_percent + 5:
                            last_percent = percent
                            _emit(progress_callback, f"Creating {destination.name}: approximately {percent}% written")
            if process.returncode != 0:
                details = (output or "").strip()
                raise RuntimeError(
                    f"Native tar.exe failed in batch {batch_number} of {len(commands)} "
                    f"with exit code {process.returncode}"
                    + (f": {details}" if details else ".")
                )
            process = None
        _raise_if_cancelled(cancel_event)
        for member in members:
            _validate_archive_member(member)
        _emit(progress_callback, f"Archived {len(members):,} files into {destination.name}")
    except Exception:
        if process is not None and process.poll() is None:
            _stop_native_tar(process)
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        raise


def _native_tar_commands(
    tar_path: str,
    destination: Path,
    source_folder: Path,
    members: list[FileSnapshot],
    *,
    command_limit: int = NATIVE_TAR_COMMAND_LIMIT,
) -> list[list[str]]:
    names = [member.path.relative_to(source_folder).as_posix() for member in members]
    batches: list[list[str]] = []
    current: list[str] = []
    for name in names:
        candidate = [*current, name]
        probe = _native_tar_command(tar_path, destination, source_folder, candidate, append=bool(batches))
        if current and len(subprocess.list2cmdline(probe)) > command_limit:
            batches.append(current)
            current = [name]
        else:
            current = candidate
    if current:
        batches.append(current)
    return [
        _native_tar_command(tar_path, destination, source_folder, batch, append=index > 0)
        for index, batch in enumerate(batches)
    ]


def _native_tar_command(
    tar_path: str,
    destination: Path,
    source_folder: Path,
    names: list[str],
    *,
    append: bool,
) -> list[str]:
    return [
        tar_path,
        "-r" if append else "-c",
        "-b",
        str(NATIVE_TAR_BLOCKING_FACTOR),
        "--format",
        "pax",
        "-f",
        str(destination),
        "-C",
        str(source_folder),
        "--",
        *names,
    ]


def _validate_archive_member(member: FileSnapshot) -> None:
    current = member.path.stat(follow_symlinks=False)
    if _is_reparse(current) or not stat_module.S_ISREG(current.st_mode):
        raise RuntimeError(f"Archive member is no longer a regular file: {member.path}")
    if current.st_size != member.size or current.st_mtime_ns != member.modified_ns:
        raise RuntimeError(f"Archive member changed while packaging: {member.path}")


def _stop_native_tar(process: subprocess.Popen[str]) -> None:
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.communicate(timeout=NATIVE_TAR_STOP_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.communicate(timeout=NATIVE_TAR_STOP_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Native tar.exe did not stop after cancellation.") from exc


def _write_inventory(destination: Path, group: ArchiveGroupPlan, members: list[FileSnapshot]) -> None:
    with destination.open("w", encoding="utf-8", newline="\n") as inventory:
        inventory.write("# QRIS Rsync Manager archive inventory v1\n")
        inventory.write(f"# archive: {group.archive_name}\n")
        inventory.write(f"# category: {group.category}\n")
        inventory.write(f"# files: {len(members)}\n")
        inventory.write("filename\tbytes\tmodified_utc\n")
        for member in members:
            modified = datetime.fromtimestamp(member.modified_ns / 1_000_000_000, timezone.utc)
            inventory.write(f"{member.path.name}\t{member.size}\t{modified.isoformat()}\n")


def _validate_root(root: Path) -> None:
    info = root.stat(follow_symlinks=False)
    if _is_reparse(info) or not stat_module.S_ISDIR(info.st_mode):
        raise ValueError("The upload folder must be a normal directory, not a symbolic link or junction.")


def _relative_posix(path: Path, root: Path) -> str:
    relative = path.relative_to(root)
    return "" if relative == Path(".") else PurePosixPath(relative).as_posix()


def _escape_rsync_pattern(path: str) -> str:
    escaped = path.replace("\\", "\\\\")
    for character in "*?[]":
        escaped = escaped.replace(character, "\\" + character)
    return escaped


def _snapshot_signature(snapshots: list[FileSnapshot]) -> tuple[tuple[str, int, int], ...]:
    return tuple(
        sorted(
            ((item.path.name.casefold(), item.size, item.modified_ns) for item in snapshots),
            key=lambda item: item[0],
        )
    )


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & getattr(info, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _raise_if_cancelled(cancel_event: CancelSignal | None) -> None:
    if cancel_event and cancel_event.is_set():
        raise ArchiveCancelled("Archive preparation was cancelled.")


def _report(callback: ProgressCallback | None, scan: FolderScan) -> None:
    if callback:
        callback(scan)


def _emit(callback: BuildProgressCallback | None, message: str) -> None:
    if callback:
        callback(message)
