"""Safe, cancellable local-folder scanning helpers."""

from __future__ import annotations

import os
import stat as stat_module
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


HIGH_FILE_COUNT = 100_000
MANY_TINY_FILES = 50_000
TINY_FILE_BYTES = 1_048_576


class CancelSignal(Protocol):
    def is_set(self) -> bool: ...


ProgressCallback = Callable[["FolderScan"], None]


@dataclass
class FolderScan:
    file_count: int = 0
    total_bytes: int = 0
    tiny_file_count: int = 0
    skipped_errors: int = 0
    directory_count: int = 0
    cancelled: bool = False

    def warnings(self) -> list[str]:
        messages: list[str] = []
        if self.file_count >= HIGH_FILE_COUNT:
            messages.append(
                f"This folder contains {self.file_count:,} files. Very high file counts can be slow on QRIScloud."
            )
        if self.tiny_file_count >= MANY_TINY_FILES:
            messages.append(
                f"This folder contains {self.tiny_file_count:,} files under 1 MB. Consider zipping or archiving small files first."
            )
        if self.skipped_errors:
            messages.append(f"{self.skipped_errors:,} files or folders could not be scanned and will still be passed to rsync.")
        if self.cancelled:
            messages.append("Local folder scan was cancelled; file counts are incomplete.")
        return messages


def scan_folder(
    path: str | Path,
    cancel_event: CancelSignal | None = None,
    progress_callback: ProgressCallback | None = None,
) -> FolderScan:
    """Scan regular files below *path* without following symlinks or junctions.

    ``progress_callback`` is invoked after each entry with the live result object.
    Callers should treat it as a snapshot and copy values they need to retain.
    """
    root = Path(path).expanduser()
    result = FolderScan()
    if _cancelled(cancel_event):
        result.cancelled = True
        _report(progress_callback, result)
        return result

    try:
        root_stat = root.stat(follow_symlinks=False)
    except (OSError, TypeError):
        result.skipped_errors = 1
        _report(progress_callback, result)
        return result

    if not _is_directory(root, root_stat):
        if _is_reparse_point(root_stat):
            raise ValueError("The upload folder cannot be a symbolic link or Windows junction.")
        _scan_file(root, result)
        _report(progress_callback, result)
        return result

    # scandir does not follow directory symlinks when follow_symlinks=False.  We
    # additionally identify Windows reparse points so junctions remain leaves.
    pending = [root]
    while pending:
        if _cancelled(cancel_event):
            result.cancelled = True
            break
        directory = pending.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            result.skipped_errors += 1
            _report(progress_callback, result)
            continue

        result.directory_count += 1
        with entries:
            for entry in entries:
                if _cancelled(cancel_event):
                    result.cancelled = True
                    break
                try:
                    stat = entry.stat(follow_symlinks=False)
                    if _is_reparse_point(stat):
                        pass
                    elif _is_directory(entry.path, stat):
                        pending.append(Path(entry.path))
                    elif _is_regular_file(stat):
                        result.file_count += 1
                        result.total_bytes += stat.st_size
                        if stat.st_size < TINY_FILE_BYTES:
                            result.tiny_file_count += 1
                except OSError:
                    result.skipped_errors += 1
                _report(progress_callback, result)
        if result.cancelled:
            break
    return result


def _scan_file(path: Path, result: FolderScan) -> None:
    try:
        stat = path.stat(follow_symlinks=False)
    except (OSError, TypeError):
        result.skipped_errors += 1
        return
    if _is_regular_file(stat) and not _is_reparse_point(stat):
        result.file_count = 1
        result.total_bytes = stat.st_size
        result.tiny_file_count = int(stat.st_size < TINY_FILE_BYTES)


def _is_directory(path: str | Path, stat: os.stat_result) -> bool:
    return not _is_reparse_point(stat) and stat_module.S_ISDIR(stat.st_mode)


def _is_regular_file(stat: os.stat_result) -> bool:
    return not _is_reparse_point(stat) and stat_module.S_ISREG(stat.st_mode)


def _is_reparse_point(stat: os.stat_result) -> bool:
    return bool(getattr(stat, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _cancelled(cancel_event: CancelSignal | None) -> bool:
    return bool(cancel_event and cancel_event.is_set())


def _report(callback: ProgressCallback | None, result: FolderScan) -> None:
    if callback is not None:
        callback(result)
