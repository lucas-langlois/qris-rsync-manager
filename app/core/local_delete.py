"""Move explicitly selected local paths to the operating-system trash."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QFile

from .file_scan import CancelSignal


@dataclass
class LocalDeleteResult:
    recycled_items: int = 0
    skipped_errors: int = 0
    cancelled: bool = False
    failures: list[str] = field(default_factory=list)

    @property
    def deleted_count(self) -> int:
        return self.recycled_items

    @property
    def deleted_files(self) -> int:
        return self.recycled_items

    @property
    def deleted_directories(self) -> int:
        return 0


DeleteProgressCallback = Callable[[LocalDeleteResult], None]


def delete_local_paths(
    paths: Iterable[str | Path],
    cancel_event: CancelSignal | None = None,
    progress_callback: DeleteProgressCallback | None = None,
) -> LocalDeleteResult:
    """Move only the caller-supplied top-level paths to the Recycle Bin.

    Delegating folder handling to the OS avoids recursively following a path
    that is swapped for a junction while deletion is in progress.
    """
    result = LocalDeleteResult()
    for raw_path in paths:
        if cancel_event and cancel_event.is_set():
            result.cancelled = True
            break
        path = Path(raw_path).expanduser().absolute()
        if not path.exists() and not path.is_symlink():
            result.skipped_errors += 1
            result.failures.append(f"{path}: path does not exist")
        elif QFile.moveToTrash(str(path)):
            result.recycled_items += 1
        else:
            result.skipped_errors += 1
            result.failures.append(f"{path}: could not move item to the Recycle Bin")
        if progress_callback:
            progress_callback(result)
    return result


def delete_local_path(
    path: str | Path,
    cancel_event: CancelSignal | None = None,
    progress_callback: DeleteProgressCallback | None = None,
) -> LocalDeleteResult:
    return delete_local_paths([path], cancel_event=cancel_event, progress_callback=progress_callback)
