from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


HIGH_FILE_COUNT = 100_000
MANY_TINY_FILES = 50_000
TINY_FILE_BYTES = 1_048_576


@dataclass
class FolderScan:
    file_count: int = 0
    total_bytes: int = 0
    tiny_file_count: int = 0
    skipped_errors: int = 0

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
        return messages


def scan_folder(path: str | Path) -> FolderScan:
    root = Path(path).expanduser()
    result = FolderScan()
    for item in root.rglob("*"):
        try:
            if not item.is_file():
                continue
            size = item.stat().st_size
        except OSError:
            result.skipped_errors += 1
            continue
        result.file_count += 1
        result.total_bytes += size
        if size < TINY_FILE_BYTES:
            result.tiny_file_count += 1
    return result

