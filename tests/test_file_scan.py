from __future__ import annotations

from pathlib import Path

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
