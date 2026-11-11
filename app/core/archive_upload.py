"""Plan and build safe TAR archives for folders of many same-type files."""

from __future__ import annotations

import os
import shutil
import stat as stat_module
import subprocess
import sys
import tempfile
import time
from hashlib import sha256
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Protocol

from .file_scan import FolderScan
from .paths import detect_tar, is_executable_file


# A folder is archived when more than this many of its direct files share one
# file type. Size alone never qualifies a folder.
ARCHIVE_MIN_SAME_TYPE = 50
# Archives larger than this are split into numbered parts, each with its own
# inventory. Smaller parts limit retry cost and tape-recall time on QRISdata.
ARCHIVE_PART_BYTES = 50_000_000_000
ARCHIVE_MAX_PARTS = 9_999
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
) -> UploadArchivePlan:
    """Scan *root* and identify directories that need a TAR archive.

    A directory qualifies when more than *min_same_type* of its direct regular
    files share a file type. Its archive holds every file of each qualifying
    type plus same-named companions (shapefile sets, sidecars). Size alone
    never qualifies a folder: a few large rasters or videos upload faster, and
    stay directly usable, as ordinary files. Subdirectories are evaluated
    independently. Archives whose
    members exceed *max_part_bytes* are split into numbered parts.
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
        try:
            _validate_folder(folder, source)
        except (OSError, ValueError):
            scan.skipped_errors += 1
            _report(progress_callback, scan)
            continue
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
) -> UploadPackage:
    """Prepare archive exclusions and, optionally, the TAR payload.

    With ``build_archives=False`` this is a lightweight dry-run preparation:
    it creates only the exclusion file and returns the planned archive groups.
    """
    if not plan.groups:
        raise ValueError("The upload plan contains no archive groups.")
    revalidate_upload_plan(plan, cancel_event)
    tar_executable: str | None = None
    if build_archives:
        tar_executable = str(Path(tar_path).expanduser()) if tar_path else detect_tar()
        if not is_executable_file(tar_executable):
            raise FileNotFoundError(
                f"Native tar.exe was not found at {tar_executable}. "
                "Windows Server 2022 normally provides C:\\Windows\\System32\\tar.exe."
            )
    source_parent = plan.root.parent
    if source_parent == plan.root:
        source_parent = Path(tempfile.gettempdir()).resolve()
    if build_archives:
        required = plan.archived_bytes + max(1_073_741_824, plan.archived_bytes // 20)
        free = shutil.disk_usage(source_parent).free
        if free < required:
            raise OSError(
                f"Not enough temporary disk space beside the upload folder. "
                f"Required about {required:,} bytes; available {free:,} bytes."
            )

    work_dir = Path(tempfile.mkdtemp(prefix=_work_directory_prefix(plan.root), dir=source_parent))
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
                snapshots = _validated_folder_snapshots(plan, folder_plan, cancel_event)
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
                        stable_mtime = max(item.modified_ns for item in members) / 1_000_000_000
                        os.utime(_windows_io_path(archive_path), (stable_mtime, stable_mtime))
                        os.utime(_windows_io_path(inventory_path), (stable_mtime, stable_mtime))
                        archive_count += 1
                        inventory_count += 1
                    for member in members:
                        relative = _relative_posix(member.path, plan.root)
                        if include_source_directory:
                            relative = f"{plan.root.name}/{relative}"
                        exclusions.write(f"/{_escape_rsync_pattern(relative)}\n")
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
    expected = {_group_identity(group) for group in folder_plan.groups}
    actual = {_group_identity(group) for group in current_groups}
    if actual != expected:
        raise RuntimeError(
            f"{folder_plan.folder} changed after it was scanned. Start the upload again so it can be rechecked."
        )
    return snapshots


def _group_identity(group: ArchiveGroupPlan) -> tuple[str, str, int, int, int]:
    return (group.archive_name, group.category, group.media_count, group.member_count, group.member_bytes)


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
        inventory.write("# QRIS Rsync Manager archive inventory v1\n")
        inventory.write(f"# archive: {group.archive_name}\n")
        inventory.write(f"# types: {group.category}\n")
        if group.part_count > 1:
            inventory.write(f"# part: {group.part_number} of {group.part_count}\n")
        inventory.write(f"# files: {len(members)}\n")
        inventory.write("filename\tbytes\tmodified_utc\n")
        for member in members:
            modified = datetime.fromtimestamp(member.modified_ns / 1_000_000_000, timezone.utc)
            inventory.write(f"{member.path.name}\t{member.size}\t{modified.isoformat()}\n")


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
    return f".{label}.qris-upload-"


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
