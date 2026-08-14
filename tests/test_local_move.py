from __future__ import annotations

from pathlib import Path

import pytest

from app.core.local_move import move_local_paths, plan_local_moves


def test_move_local_paths_moves_selected_items_into_folder(tmp_path: Path) -> None:
    source_file = tmp_path / "one.txt"
    source_file.write_text("one", encoding="utf-8")
    source_folder = tmp_path / "photos"
    source_folder.mkdir()
    (source_folder / "image.jpg").write_bytes(b"image")
    destination = tmp_path / "archive"
    destination.mkdir()

    moved = move_local_paths([source_file, source_folder], destination, tmp_path)

    assert moved == [
        (source_file, destination / "one.txt"),
        (source_folder, destination / "photos"),
    ]
    assert (destination / "one.txt").read_text(encoding="utf-8") == "one"
    assert (destination / "photos" / "image.jpg").read_bytes() == b"image"


def test_plan_local_moves_rejects_overwrite_without_moving(tmp_path: Path) -> None:
    source = tmp_path / "one.txt"
    source.write_text("source", encoding="utf-8")
    destination = tmp_path / "archive"
    destination.mkdir()
    (destination / "one.txt").write_text("existing", encoding="utf-8")

    with pytest.raises(ValueError, match="already exists"):
        plan_local_moves([source], destination, tmp_path)

    assert source.exists()


def test_plan_local_moves_rejects_folder_descendant_target(tmp_path: Path) -> None:
    source = tmp_path / "photos"
    destination = source / "sorted"
    destination.mkdir(parents=True)

    with pytest.raises(ValueError, match="itself"):
        plan_local_moves([source], destination, tmp_path)


def test_plan_local_moves_deduplicates_child_of_selected_folder(tmp_path: Path) -> None:
    source = tmp_path / "photos"
    source.mkdir()
    child = source / "image.jpg"
    child.write_bytes(b"image")
    destination = tmp_path / "archive"
    destination.mkdir()

    moves = plan_local_moves([child, source], destination, tmp_path)

    assert moves == [(source, destination / "photos")]
