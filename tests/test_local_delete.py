from __future__ import annotations

import threading
from pathlib import Path

from app.core.local_delete import delete_local_path, delete_local_paths


def test_delete_local_path_moves_exact_target_to_trash(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    calls: list[str] = []
    monkeypatch.setattr("app.core.local_delete.QFile.moveToTrash", lambda path: calls.append(path) or True)
    progress: list[int] = []

    result = delete_local_path(target, progress_callback=lambda update: progress.append(update.deleted_count))

    assert calls == [str(target.absolute())]
    assert result.recycled_items == 1
    assert result.failures == []
    assert progress == [1]


def test_delete_local_path_reports_trash_failure(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("keep", encoding="utf-8")
    monkeypatch.setattr("app.core.local_delete.QFile.moveToTrash", lambda _path: False)

    result = delete_local_path(target)

    assert result.recycled_items == 0
    assert result.skipped_errors == 1
    assert "Recycle Bin" in result.failures[0]


def test_delete_local_paths_observes_cancellation_between_targets(monkeypatch, tmp_path: Path) -> None:
    targets = [tmp_path / name for name in ("one.txt", "two.txt", "three.txt")]
    for target in targets:
        target.write_text(target.name, encoding="utf-8")
    cancelled = threading.Event()
    calls: list[str] = []

    def move(path: str) -> bool:
        calls.append(path)
        cancelled.set()
        return True

    monkeypatch.setattr("app.core.local_delete.QFile.moveToTrash", move)
    result = delete_local_paths(targets, cancel_event=cancelled)

    assert result.cancelled
    assert result.recycled_items == 1
    assert calls == [str(targets[0].absolute())]
