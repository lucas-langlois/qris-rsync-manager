"""Plan and build safe TAR archives for folders of many same-type files."""

from __future__ import annotations

import os
import shutil
import stat as stat_module
import subprocess
import sys
import tempfile
import time
import uuid
from hashlib import sha256
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Protocol

from .file_scan import FolderScan
from .paths import (
    detect_tar,
    is_executable_file,
    is_link_or_junction,
    APP_WORK_FOLDER_MARKER,
    is_excluded_folder_name,
)
from .receipts import RECEIPT_DIR_NAME


# A folder is archived when more than this many of its direct files share one
# file type. Size alone never qualifies a folder.
ARCHIVE_MIN_SAME_TYPE = 50
# Archives larger than this are split into numbered parts, each with its own
# inventory. Smaller parts limit retry cost and tape-recall time on QRISdata.
ARCHIVE_PART_BYTES = 50_000_000_000
ARCHIVE_MAX_PARTS = 9_999
# Archives are built and uploaded in batches of up to this size (or one bigger
# archive part), each deleted before the next, so a large backup needs little
# temporary space and starts sending early.
ARCHIVE_BATCH_BYTES = 20_000_000_000
# Always left free on the drive that holds the temporary archives, so a shared
# drive never fills up for the people working on it.
ARCHIVE_TEMP_RESERVE_BYTES = 5_000_000_000
TAR_PER_FILE_BYTES = 1_536
TAR_FIXED_BYTES = 64_000_000
NATIVE_TAR_BLOCKING_FACTOR = 2048
NATIVE_TAR_POLL_SECONDS = 0.2
NATIVE_TAR_STOP_SECONDS = 5.0
NATIVE_TAR_COMMAND_LIMIT = 30_000

# Extensions that name the same file type.
TYPE_ALIASES = {".jpeg": ".jpg", ".tiff": ".tif", ".mpeg": ".mpg", ".html": ".htm"}


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
    part_number: int = 1
    part_count: int = 1


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
    max_part_bytes: int = ARCHIVE_PART_BYTES
    min_same_type: int = ARCHIVE_MIN_SAME_TYPE

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
class ArchivePartExpectation:
    """What one archive part and its inventory look like after an upload."""

    archive_name: str
    inventory_name: str
    inventory_size: int
    modified: float


@dataclass(frozen=True)
class ArchiveFolderExpectation:
    """The archive(s) an automatic upload would create for one local folder.

    ``member_paths`` are relative to the upload root, using ``/``. Uploaded
    inventories are generated deterministically from the members, so a remote
    inventory with the expected size and modification time means the archive
    on QRIScloud still matches the local files, without reading the remote
    archive (which may be on tape).
    """

    relative_folder: str
    category: str
    member_paths: tuple[str, ...]
    member_bytes: int
    parts: tuple[ArchivePartExpectation, ...]

    @property
    def member_count(self) -> int:
        return len(self.member_paths)


@dataclass(frozen=True)
class UploadPackage:
    work_dir: Path
    payload_dir: Path
    exclude_file: Path
    archive_count: int
    inventory_count: int
    planned_groups: tuple[ArchiveGroupPlan, ...] = ()

    def cleanup(self) -> str | None:
        try:
            shutil.rmtree(_windows_io_path(self.work_dir))
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
    min_same_type: int = ARCHIVE_MIN_SAME_TYPE,
    max_part_bytes: int = ARCHIVE_PART_BYTES,
    file_callback: Callable[[FileSnapshot], None] | None = None,
) -> UploadArchivePlan:
    """Scan *root* and identify directories that need a TAR archive.

    A directory qualifies when more than *min_same_type* of its direct regular
    files share a file type. Its archive holds every file of each qualifying
    type plus same-named companions (shapefile sets, sidecars). Size alone
    never qualifies a folder: a few large rasters or videos upload faster, and
    stay directly usable, as ordinary files. Subdirectories are evaluated
    independently. Archives whose
    members exceed *max_part_bytes* are split into numbered parts.

    Transfer-receipt folders are app records, never data, and are skipped.
    *file_callback* receives every regular file found, so a caller can build
    a file list from the same scan.
    """
    if max_part_bytes <= 0:
        raise ValueError("max_part_bytes must be positive.")
    source = Path(root).expanduser().resolve()
    _validate_root(source)
    scan = FolderScan()
    plan = UploadArchivePlan(
        root=source, scan=scan, max_part_bytes=max_part_bytes, min_same_type=min_same_type
    )
    pending = [source]

    while pending:
        _raise_if_cancelled(cancel_event)
        folder = pending.pop()
        # Subfolders were queued only after their scandir entry showed a normal
        # directory, so only the root needs a stat here. Re-checking every
        # ancestor cost one network round trip per level per folder on a share.
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
                        # Links and junctions are left to rsync, as in scan_folder.
                        pass
                    elif stat_module.S_ISDIR(info.st_mode):
                        if entry.name != RECEIPT_DIR_NAME and not is_excluded_folder_name(entry.name):
                            pending.append(Path(entry.path))
                    elif stat_module.S_ISREG(info.st_mode):
                        snapshot = FileSnapshot(Path(entry.path), info.st_size, info.st_mtime_ns)
                        snapshots.append(snapshot)
                        if file_callback is not None:
                            file_callback(snapshot)
                        scan.file_count += 1
                        scan.total_bytes += info.st_size
                        if info.st_size < 1_048_576:
                            scan.tiny_file_count += 1
                except OSError:
                    scan.skipped_errors += 1
                _report(progress_callback, scan)

        flat_bytes = sum(item.size for item in snapshots)
        groups = _group_snapshots(source, folder, snapshots, max_part_bytes, min_same_type)
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
    include_source_directory: bool = False,
) -> UploadPackage:
    """Build TAR/inventory payload and an rsync exclusion list.

    Source files are never modified. The temporary work directory is created
    beside the upload root so its free-space characteristics are predictable.
    """
    return prepare_upload_package(
        plan,
        cancel_event,
        progress_callback,
        tar_path=tar_path,
        build_archives=True,
        include_source_directory=include_source_directory,
    )


def prepare_upload_package(
    plan: UploadArchivePlan,
    cancel_event: CancelSignal | None = None,
    progress_callback: BuildProgressCallback | None = None,
    *,
    tar_path: str | Path | None = None,
    build_archives: bool = True,
    include_source_directory: bool = False,
    work_parent: Path | None = None,
    skip_changed: list[Path] | None = None,
) -> UploadPackage:
    """Prepare archive exclusions and, optionally, the TAR payload.

    *work_parent* is where the temporary folder is made; by default beside the
    upload folder (see ``plan_archive_batches``).

    Without *skip_changed*, any folder that changed since the scan raises.
    With a list, changed folders are appended to it instead and none of their
    direct files is sent loose or archived: a share in use keeps going, and
    the caller reports them for a later Synchronise.

    With ``build_archives=False`` this is a lightweight dry-run preparation:
    it creates only the exclusion file and returns the planned archive groups.
    """
    if not plan.groups:
        raise ValueError("The upload plan contains no archive groups.")
    if skip_changed is None:
        revalidate_upload_plan(plan, cancel_event)
    else:
        revalidate_upload_plan(replace(plan, folders=[]), cancel_event)  # the root only
    tar_executable: str | None = None
    if build_archives:
        tar_executable = str(Path(tar_path).expanduser()) if tar_path else detect_tar()
        if not is_executable_file(tar_executable):
            raise FileNotFoundError(
                f"Native tar.exe was not found at {tar_executable}. "
                "Windows Server 2022 normally provides C:\\Windows\\System32\\tar.exe."
            )
    source_parent = work_parent or default_work_parent(plan.root)
    if build_archives:
        required = required_temp_bytes(plan.groups)
        free = shutil.disk_usage(source_parent).free
        if free < required:
            raise NotEnoughTempSpace(
                f"Not enough temporary disk space in {source_parent}. "
                f"Required about {required:,} bytes; available {free:,} bytes."
            )

    work_dir = _make_work_dir(source_parent, _work_directory_prefix(plan.root))
    payload_dir = work_dir / "payload"
    exclude_file = work_dir / "archived-files.exclude"
    payload_dir.mkdir()
    package = UploadPackage(work_dir, payload_dir, exclude_file, 0, 0, tuple(plan.groups))
    try:
        with exclude_file.open("w", encoding="utf-8", newline="\n") as exclusions:
            archive_count = 0
            inventory_count = 0
            for folder_plan in plan.folders:
                _raise_if_cancelled(cancel_event)
                try:
                    snapshots = _validated_folder_snapshots(plan, folder_plan, cancel_event)
                except RuntimeError:
                    if skip_changed is None:
                        raise
                    skip_changed.append(folder_plan.folder)
                    exclusions.writelines(_folder_filter_rules(plan, folder_plan, [], include_source_directory))
                    continue
                member_map = dict(_archive_parts(snapshots, plan.max_part_bytes, plan.min_same_type))
                for group in folder_plan.groups:
                    members = member_map[group.part_number]
                    if build_archives:
                        destination = payload_dir / Path(group.relative_folder) if group.relative_folder else payload_dir
                        os.makedirs(_windows_io_path(destination), exist_ok=True)
                        archive_path = destination / group.archive_name
                        inventory_path = destination / group.inventory_name
                        staged_archive_path = work_dir / f"archive-{archive_count:06d}.tar"
                        _emit(progress_callback, f"Creating archive: {archive_path.name}")
                        _write_tar(staged_archive_path, folder_plan.folder, members, cancel_event,
                                   progress_callback, tar_executable or "", display_name=archive_path.name)
                        os.replace(_windows_io_path(staged_archive_path), _windows_io_path(archive_path))
                        _write_inventory(inventory_path, group, members)
                        stable_mtime = _stable_mtime(members)
                        os.utime(_windows_io_path(archive_path), (stable_mtime, stable_mtime))
                        os.utime(_windows_io_path(inventory_path), (stable_mtime, stable_mtime))
                        archive_count += 1
                        inventory_count += 1
                archived = {member.path for members in member_map.values() for member in members}
                exclusions.writelines(
                    _folder_filter_rules(
                        plan, folder_plan, [item for item in snapshots if item.path not in archived],
                        include_source_directory,
                    )
                )
        return UploadPackage(work_dir, payload_dir, exclude_file, archive_count, inventory_count, tuple(plan.groups))
    except Exception as exc:
        cleanup_error = package.cleanup()
        if cleanup_error:
            raise RuntimeError(f"{exc}\n{cleanup_error}") from exc
        raise


def _group_snapshots(
    root: Path,
    folder: Path,
    snapshots: list[FileSnapshot],
    max_part_bytes: int = ARCHIVE_PART_BYTES,
    min_same_type: int = 0,
) -> list[ArchiveGroupPlan]:
    """Plan the archive (or numbered parts) for one folder's direct files."""
    types = _qualifying_types(snapshots, min_same_type)
    if not types:
        return []
    parts = _archive_parts(snapshots, max_part_bytes, min_same_type)
    part_count = len(parts)
    if part_count > ARCHIVE_MAX_PARTS:
        raise ValueError(
            f"Cannot archive {folder}: it would need {part_count:,} parts "
            f"(maximum {ARCHIVE_MAX_PARTS:,}). Split the folder into smaller folders first."
        )
    relative_folder = _relative_posix(folder, root)
    safe_base = _archive_basename(folder.name or root.name or "folder")
    existing_names = {item.path.name.casefold() for item in snapshots}
    type_label = ", ".join(_type_label(item) for item in sorted(types))
    groups: list[ArchiveGroupPlan] = []
    for part_number, members in parts:
        if part_count == 1:
            archive_name = f"{safe_base}.tar"
        else:
            width = max(2, len(str(part_count)))
            archive_name = f"{safe_base}.part{part_number:0{width}d}.tar"
        inventory_name = f"{archive_name}.inventory.txt"
        if archive_name.casefold() in existing_names or inventory_name.casefold() in existing_names:
            raise ValueError(
                f"Cannot create the automatic archive in {folder}: {archive_name} or its inventory already exists."
            )
        groups.append(
            ArchiveGroupPlan(
                folder=folder,
                relative_folder=relative_folder,
                category=type_label,
                media_count=sum(1 for item in members if _file_type(item.path) in types),
                member_count=len(members),
                member_bytes=sum(item.size for item in members),
                archive_name=archive_name,
                inventory_name=inventory_name,
                part_number=part_number,
                part_count=part_count,
            )
        )
    return groups


def _file_type(path: Path) -> str:
    suffix = path.suffix.casefold()
    return TYPE_ALIASES.get(suffix, suffix)


def _type_label(file_type: str) -> str:
    return file_type.lstrip(".") or "no extension"


def _qualifying_types(snapshots: list[FileSnapshot], min_same_type: int) -> set[str]:
    counts: dict[str, int] = {}
    for item in snapshots:
        file_type = _file_type(item.path)
        counts[file_type] = counts.get(file_type, 0) + 1
    return {file_type for file_type, count in counts.items() if count > min_same_type}


def _archive_units(snapshots: list[FileSnapshot], min_same_type: int) -> dict[str, list[FileSnapshot]]:
    """Group archive members by base name so related files stay together.

    Every file of a qualifying type is a member. A file of another type joins
    when it shares a member's base name (``site01.shx`` with ``site01.shp``,
    ``IMG_1.xmp`` with ``IMG_1.jpg``) or extends a member's full name
    (``clip.mp4.json``, ``ortho.tif.aux.xml``).
    """
    types = _qualifying_types(snapshots, min_same_type)
    primary = [item for item in snapshots if _file_type(item.path) in types]
    stems = {item.path.stem.casefold() for item in primary}
    stem_by_full_name = {item.path.name.casefold(): item.path.stem.casefold() for item in primary}
    units: dict[str, list[FileSnapshot]] = {}
    for item in snapshots:
        key = _unit_key(item, stems, stem_by_full_name)
        if key is not None:
            units.setdefault(key, []).append(item)
    return units


def _unit_key(item: FileSnapshot, stems: set[str], stem_by_full_name: dict[str, str]) -> str | None:
    """Return the base name *item* is archived under, or None to leave it loose."""
    name = item.path.name.casefold()
    if name in stem_by_full_name:
        return stem_by_full_name[name]
    stem = item.path.stem.casefold()
    if stem in stems:
        return stem
    # Companions that extend a member's full name, such as ``clip.mp4.json``.
    prefix = name
    while "." in prefix:
        prefix = prefix.rsplit(".", 1)[0]
        if prefix in stem_by_full_name:
            return stem_by_full_name[prefix]
    return None


def _archive_parts(
    snapshots: list[FileSnapshot],
    max_part_bytes: int = ARCHIVE_PART_BYTES,
    min_same_type: int = 0,
) -> list[tuple[int, list[FileSnapshot]]]:
    """Split a folder's archive members into parts of at most *max_part_bytes*.

    Related files always stay in the same part. A single unit larger than the
    limit gets a part of its own. Assignment is deterministic so analysis and
    packaging produce identical parts.
    """
    units = _archive_units(snapshots, min_same_type)
    parts: list[list[FileSnapshot]] = []
    current: list[FileSnapshot] = []
    current_bytes = 0
    for key in sorted(units):
        unit = units[key]
        unit_bytes = sum(item.size for item in unit)
        if current and current_bytes + unit_bytes > max_part_bytes:
            parts.append(current)
            current, current_bytes = [], 0
        current.extend(unit)
        current_bytes += unit_bytes
    if current:
        parts.append(current)
    for part in parts:
        part.sort(key=lambda item: item.path.name.casefold())
    return list(enumerate(parts, start=1))


def _snapshot_flat_files(
    folder: Path,
    cancel_event: CancelSignal | None,
    root: Path | None = None,
) -> list[FileSnapshot]:
    _validate_folder(folder, root)
    snapshots: list[FileSnapshot] = []
    with os.scandir(folder) as entries:
        for entry in entries:
            _raise_if_cancelled(cancel_event)
            info = entry.stat(follow_symlinks=False)
            if not _is_reparse(info) and stat_module.S_ISREG(info.st_mode):
                snapshots.append(FileSnapshot(Path(entry.path), info.st_size, info.st_mtime_ns))
    return snapshots


def archive_expectations(
    plan: UploadArchivePlan,
    cancel_event: CancelSignal | None = None,
) -> list[ArchiveFolderExpectation]:
    """Describe the archives an upload of *plan* would put on QRIScloud."""
    expectations: list[ArchiveFolderExpectation] = []
    for folder_plan in plan.folders:
        _raise_if_cancelled(cancel_event)
        snapshots = _validated_folder_snapshots(plan, folder_plan, cancel_event)
        member_map = dict(_archive_parts(snapshots, plan.max_part_bytes, plan.min_same_type))
        member_paths: list[str] = []
        member_bytes = 0
        parts: list[ArchivePartExpectation] = []
        for group in folder_plan.groups:
            members = member_map[group.part_number]
            member_paths.extend(_relative_posix(member.path, plan.root) for member in members)
            member_bytes += sum(member.size for member in members)
            parts.append(
                ArchivePartExpectation(
                    archive_name=group.archive_name,
                    inventory_name=group.inventory_name,
                    inventory_size=len(_inventory_text(group, members).encode("utf-8")),
                    modified=_stable_mtime(members),
                )
            )
        expectations.append(
            ArchiveFolderExpectation(
                relative_folder=folder_plan.relative_folder,
                category=", ".join(dict.fromkeys(group.category for group in folder_plan.groups)),
                member_paths=tuple(member_paths),
                member_bytes=member_bytes,
                parts=tuple(parts),
            )
        )
    return expectations


def subset_upload_plan(plan: UploadArchivePlan, relative_folders: set[str]) -> UploadArchivePlan:
    """Return a copy of *plan* that only archives the given folders."""
    return UploadArchivePlan(
        root=plan.root,
        scan=plan.scan,
        folders=[folder for folder in plan.folders if folder.relative_folder in relative_folders],
        max_part_bytes=plan.max_part_bytes,
        min_same_type=plan.min_same_type,
    )


def required_temp_bytes(groups) -> int:
    """Temporary space to build these archive parts: the tar size plus a margin.

    A tar adds one 512-byte header per file (two for long or non-ASCII names)
    and pads each file to 512 bytes.
    """
    return sum(group.member_bytes + group.member_count * TAR_PER_FILE_BYTES for group in groups) + TAR_FIXED_BYTES


class NotEnoughTempSpace(OSError):
    """The temporary folder no longer has room for a batch."""


@dataclass(frozen=True)
class ArchiveBatchPlan:
    """Where to build archives, the batches to build and upload in turn, and
    the archive parts too big for the free space (they are skipped)."""

    work_parent: Path
    batches: tuple[UploadArchivePlan, ...]
    too_big: tuple[ArchiveGroupPlan, ...] = ()
    room: int = 0


def plan_archive_batches(
    plan: UploadArchivePlan,
    *,
    batch_bytes: int = ARCHIVE_BATCH_BYTES,
) -> ArchiveBatchPlan:
    """Split *plan* into upload batches built one at a time on the source drive.

    Archives are built beside the source folder, or inside it when the parent
    is read-only (such as the root of a mapped share). Each batch is uploaded
    and deleted before the next is built, so at most one batch (normally
    about 20 GB, or one bigger archive part) uses space at a time, and
    ``ARCHIVE_TEMP_RESERVE_BYTES`` always stays free for other people using
    the drive. A part that cannot fit at all is listed in ``too_big``.
    """
    work_parent = choose_work_parent(plan.root)
    room = _free_bytes(work_parent) - ARCHIVE_TEMP_RESERVE_BYTES
    limit = max(0, min(batch_bytes, room))
    batches: list[list[tuple[EligibleFolderPlan, ArchiveGroupPlan]]] = [[]]
    too_big: list[ArchiveGroupPlan] = []
    for folder in plan.folders:
        for group in folder.groups:
            if required_temp_bytes([group]) > room:
                too_big.append(group)
                continue
            current = [item for _folder, item in batches[-1]]
            if current and required_temp_bytes(current + [group]) > limit:
                batches.append([])
            batches[-1].append((folder, group))
    return ArchiveBatchPlan(
        work_parent,
        tuple(_plan_from_groups(plan, batch) for batch in batches if batch),
        tuple(too_big),
        room,
    )


def split_batch_by_folder(batch: UploadArchivePlan) -> list[UploadArchivePlan]:
    """One plan per folder, so a folder that changed can be skipped alone."""
    return [replace(batch, folders=[folder]) for folder in batch.folders]


def choose_work_parent(root: Path) -> Path:
    """The folder beside *root* when writable, else *root* itself.

    The parent of a top-level folder on a share is often the read-only share
    root. A work folder inside *root* is hidden and never scanned or uploaded
    (``is_app_work_folder``).
    """
    parent = root.parent
    if parent != root and _is_writable_folder(parent):
        return parent
    return root


def default_work_parent(root: Path) -> Path:
    parent = root.parent
    return Path(tempfile.gettempdir()).resolve() if parent == root else parent


def _is_writable_folder(folder: Path) -> bool:
    probe = folder / f".qris-write-check-{uuid.uuid4().hex}"
    try:
        os.mkdir(_windows_io_path(probe))
    except OSError:
        return False
    try:
        os.rmdir(_windows_io_path(probe))
    except OSError:
        pass
    return True


def _make_work_dir(parent: Path, prefix: str) -> Path:
    """Create a unique work folder.

    Not ``tempfile.mkdtemp``: on Windows it treats "access denied" as a name
    clash and retries 10,000 times, which looks like a hang on a read-only
    network folder.
    """
    for _attempt in range(5):
        candidate = parent / f"{prefix}{uuid.uuid4().hex[:8]}"
        try:
            os.mkdir(_windows_io_path(candidate))
            return candidate
        except FileExistsError:
            continue
        except PermissionError as exc:
            raise PermissionError(f"Cannot create a temporary folder in {parent}: {exc}") from exc
    raise FileExistsError(f"Could not create a unique temporary folder in {parent}.")


def _plan_from_groups(
    plan: UploadArchivePlan, pairs: list[tuple[EligibleFolderPlan, ArchiveGroupPlan]]
) -> UploadArchivePlan:
    folders: list[EligibleFolderPlan] = []
    for folder, group in pairs:
        if folders and folders[-1].folder == folder.folder:
            folders[-1] = replace(folders[-1], groups=folders[-1].groups + (group,))
        else:
            folders.append(replace(folder, groups=(group,)))
    return replace(plan, folders=folders)


def _free_bytes(path: Path) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def revalidate_upload_plan(
    plan: UploadArchivePlan,
    cancel_event: CancelSignal | None = None,
) -> None:
    """Confirm every selected source folder still exactly matches *plan*.

    Call immediately before an upload phase.  This is intentionally separate
    from TAR construction so callers can reject source changes before any
    transfer command is started.
    """
    try:
        _validate_root(plan.root)
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"{plan.root} changed after it was scanned. Start the upload again so it can be rechecked."
        ) from exc
    for folder_plan in plan.folders:
        _raise_if_cancelled(cancel_event)
        _validated_folder_snapshots(plan, folder_plan, cancel_event)


def _validated_folder_snapshots(
    plan: UploadArchivePlan,
    folder_plan: EligibleFolderPlan,
    cancel_event: CancelSignal | None,
) -> list[FileSnapshot]:
    try:
        snapshots = _snapshot_flat_files(folder_plan.folder, cancel_event, plan.root)
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"{folder_plan.folder} changed after it was scanned. Start the upload again so it can be rechecked."
        ) from exc
    if _snapshot_signature(snapshots) != folder_plan.snapshot_signature:
        raise RuntimeError(
            f"{folder_plan.folder} changed after it was scanned. Start the upload again so it can be rechecked."
        )
    current_groups = _group_snapshots(
        plan.root, folder_plan.folder, snapshots, plan.max_part_bytes, plan.min_same_type
    )
    # A batch may hold only some of a folder's archive parts.
    expected = {_group_identity(group) for group in folder_plan.groups}
    actual = {_group_identity(group) for group in current_groups}
    if not expected <= actual:
        raise RuntimeError(
            f"{folder_plan.folder} changed after it was scanned. Start the upload again so it can be rechecked."
        )
    return snapshots


def _group_identity(group: ArchiveGroupPlan) -> tuple[str, str, int, int, int, int]:
    return (
        group.archive_name, group.category, group.media_count, group.member_count, group.member_bytes,
        group.part_number,
    )


def _write_tar(
    destination: Path,
    source_folder: Path,
    members: list[FileSnapshot],
    cancel_event: CancelSignal | None,
    progress_callback: BuildProgressCallback | None,
    tar_path: str,
    display_name: str | None = None,
) -> None:
    for member in members:
        _raise_if_cancelled(cancel_event)
        _validate_archive_member(member)
    commands = _native_tar_commands(tar_path, destination, source_folder, members)
    progress_name = display_name or destination.name
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
                        try:
                            written = os.stat(_windows_io_path(destination)).st_size
                        except FileNotFoundError:
                            written = 0
                        percent = min(99, int(written * 100 / total_bytes))
                        if percent >= last_percent + 5:
                            last_percent = percent
                            _emit(progress_callback, f"Creating {progress_name}: approximately {percent}% written")
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
        _emit(progress_callback, f"Archived {len(members):,} files into {progress_name}")
    except Exception:
        if process is not None and process.poll() is None:
            _stop_native_tar(process)
        try:
            os.unlink(_windows_io_path(destination))
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
        # CreateProcess counts UTF-16 code units, not Python characters.
        if _utf16_units(subprocess.list2cmdline(probe)) > command_limit:
            single = _native_tar_command(tar_path, destination, source_folder, [name], append=bool(batches or current))
            if _utf16_units(subprocess.list2cmdline(single)) > command_limit:
                raise ValueError(f"Archive member exceeds the Windows command-line limit: {name}")
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
    with open(_windows_io_path(destination), "w", encoding="utf-8", newline="\n") as inventory:
        inventory.write(_inventory_text(group, members))


def _inventory_text(group: ArchiveGroupPlan, members: list[FileSnapshot]) -> str:
    lines = [
        "# QRIS Rsync Manager archive inventory v1",
        f"# archive: {group.archive_name}",
        f"# types: {group.category}",
    ]
    if group.part_count > 1:
        lines.append(f"# part: {group.part_number} of {group.part_count}")
    lines.append(f"# files: {len(members)}")
    lines.append("filename\tbytes\tmodified_utc")
    for member in members:
        modified = datetime.fromtimestamp(member.modified_ns / 1_000_000_000, timezone.utc)
        lines.append(f"{member.path.name}\t{member.size}\t{modified.isoformat()}")
    return "".join(f"{line}\n" for line in lines)


def _stable_mtime(members: list[FileSnapshot]) -> float:
    """Archive and inventory mtime: the newest member's, so it is reproducible."""
    return max(item.modified_ns for item in members) / 1_000_000_000


def _windows_io_path(path: str | Path) -> str:
    """Return an extended Windows path so packaged builds can exceed MAX_PATH."""
    raw = os.path.abspath(os.fspath(path))
    if not sys.platform.startswith("win") or raw.startswith("\\\\?\\"):
        return raw
    if raw.startswith("\\\\"):
        return "\\\\?\\UNC\\" + raw[2:]
    return "\\\\?\\" + raw


def _work_directory_prefix(root: Path) -> str:
    """Keep temporary-directory components safely below Windows' name limit."""
    label = root.name or "upload"
    if len(label) > 48:
        label = f"{label[:31]}-{sha256(label.encode('utf-8')).hexdigest()[:16]}"
    return f".{label}{APP_WORK_FOLDER_MARKER}"


def _archive_basename(name: str) -> str:
    """Return a deterministic component that leaves room for archive suffixes."""
    # The longest generated component is ``.part0000.tar.inventory.txt``.
    max_units = 255 - _utf16_units(".part0000.tar.inventory.txt")
    if _utf16_units(name) <= max_units:
        return name
    digest = sha256(name.encode("utf-8")).hexdigest()[:16]
    suffix = f"-{digest}"
    return f"{_truncate_utf16(name, max_units - _utf16_units(suffix))}{suffix}"


def _utf16_units(value: str) -> int:
    """Return the Windows filename-unit length without writing a BOM."""
    return len(value.encode("utf-16-le")) // 2


def _truncate_utf16(value: str, max_units: int) -> str:
    """Truncate at a Unicode boundary while respecting an NTFS component cap."""
    result: list[str] = []
    used = 0
    for character in value:
        units = _utf16_units(character)
        if used + units > max_units:
            break
        result.append(character)
        used += units
    return "".join(result)


def _validate_root(root: Path) -> None:
    info = root.stat(follow_symlinks=False)
    if _is_reparse(info) or not stat_module.S_ISDIR(info.st_mode):
        raise ValueError("The upload folder must be a normal directory, not a symbolic link or junction.")


def _validate_folder(folder: Path, root: Path | None = None) -> None:
    """Require selected folders (and their source ancestry) to stay normal dirs."""
    components = [folder]
    if root is not None:
        relative = folder.relative_to(root)
        components = [root]
        current = root
        for part in relative.parts:
            current = current / part
            components.append(current)
    for component in components:
        info = component.stat(follow_symlinks=False)
        if _is_reparse(info) or not stat_module.S_ISDIR(info.st_mode):
            raise ValueError(f"The selected upload folder is no longer a normal directory: {component}")


def _relative_posix(path: Path, root: Path) -> str:
    relative = path.relative_to(root)
    return "" if relative == Path(".") else PurePosixPath(relative).as_posix()


def _folder_filter_rules(
    plan: UploadArchivePlan,
    folder_plan: EligibleFolderPlan,
    loose: list[FileSnapshot],
    include_source_directory: bool,
) -> list[str]:
    """rsync rules that leave out one archived folder's archived files.

    A few rules per folder instead of one per archived file: rsync checks every
    path against every rule, and 775,000 per-file rules (a real 4.6 TB share)
    cost about 8 ms per path, hours per upload. The rules keep subfolders and
    the folder's files that are not archived, and leave out the rest of the
    files directly inside it. First match wins.
    """
    prefix = folder_plan.relative_folder
    if include_source_directory:
        prefix = f"{plan.root.name}/{prefix}" if prefix else plan.root.name
    base = f"/{_escape_rsync_pattern(prefix)}/" if prefix else "/"
    rules = [f"+ {base}*/\n"]
    for item in loose:
        rules.append(f"+ {base}{_escape_rsync_pattern(item.path.name)}\n")
    rules.append(f"- {base}*\n")
    return rules


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
    return is_link_or_junction(info)


def _raise_if_cancelled(cancel_event: CancelSignal | None) -> None:
    if cancel_event and cancel_event.is_set():
        raise ArchiveCancelled("Archive preparation was cancelled.")


def _report(callback: ProgressCallback | None, scan: FolderScan) -> None:
    if callback:
        callback(scan)


def _emit(callback: BuildProgressCallback | None, message: str) -> None:
    if callback:
        callback(message)
