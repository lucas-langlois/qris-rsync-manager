from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from app.core.file_scan import FolderScan, scan_folder


def test_scan_folder_counts_files_and_bytes(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("abc", encoding="utf-8")
    nested = tmp_path / "Nested Folder"
    nested.mkdir()
    (nested / "b.txt").write_text("hello", encoding="utf-8")

    result = scan_folder(tmp_path)

    assert result.file_count == 2
    assert result.total_bytes == 8
    assert result.tiny_file_count == 2


def test_folder_scan_warning_for_high_file_count() -> None:
    scan = FolderScan(file_count=100_000, tiny_file_count=50_000)

    warnings = scan.warnings()

    assert any("100,000 files" in warning for warning in warnings)
    assert any("50,000 files under 1 MB" in warning for warning in warnings)


def test_scan_folder_reports_progress_and_stops_after_cancellation(tmp_path: Path) -> None:
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    cancelled = threading.Event()
    progress: list[int] = []

    def on_progress(scan: FolderScan) -> None:
        progress.append(scan.file_count)
        cancelled.set()

    result = scan_folder(tmp_path, cancel_event=cancelled, progress_callback=on_progress)

    assert result.cancelled
    assert result.file_count == 1
    assert progress == [1]


def test_scan_folder_does_not_follow_directory_symlink(tmp_path: Path) -> None:
    external = tmp_path.with_name(f"{tmp_path.name}-external")
    external.mkdir()
    (external / "outside.txt").write_text("outside", encoding="utf-8")
    linked = tmp_path / "linked"
    try:
        os.symlink(external, linked, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Directory symlinks are unavailable: {exc}")
    (tmp_path / "inside.txt").write_text("inside", encoding="utf-8")

    result = scan_folder(tmp_path)

    assert result.file_count == 1
    assert result.total_bytes == len("inside")


def test_scan_folder_rejects_symlink_root(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    (external / "outside.txt").write_text("outside", encoding="utf-8")
    linked = tmp_path / "linked-root"
    try:
        os.symlink(external, linked, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Directory symlinks are unavailable: {exc}")

    with pytest.raises(ValueError, match="cannot be a symbolic link"):
        scan_folder(linked)
