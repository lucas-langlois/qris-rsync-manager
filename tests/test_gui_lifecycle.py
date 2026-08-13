from __future__ import annotations

import threading
import time

from PySide6.QtCore import QThread
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from app.core.remote_dirs import RemoteEntry
from app.gui.main_window import MainWindow
from app.gui.remote_browser_dialog import RemoteListWorker


def _application() -> QApplication:
    return QApplication.instance() or QApplication([])


def _window(monkeypatch, tmp_path) -> MainWindow:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    _application()
    return MainWindow()


def test_stale_remote_listing_is_not_applied_to_changed_path(monkeypatch, tmp_path) -> None:
    window = _window(monkeypatch, tmp_path)
    profile = window.current_profile()
    assert profile is not None
    requested_path = window.remote_path_edit.text().strip().rstrip("/") or "/"
    worker = RemoteListWorker(["unused"], "")
    worker.request_id = 1
    worker.request_context = (
        profile.name,
        profile.host,
        profile.username,
        profile.ssh_port,
        profile.ssh_key_path,
        requested_path,
    )
    window._remote_request_id = 1
    worker.finished.connect(window._remote_listing_finished)

    window.remote_path_edit.setText("/data/Q9999")
    worker.finished.emit(
        [RemoteEntry(kind="f", name="old.txt", size=1, modified="now", path=f"{requested_path}/old.txt")],
        "",
    )

    assert window.remote_entries == []
    assert window.remote_entries_path == ""
    assert window.remote_table.rowCount() == 0
    assert "changed while loading" in window.remote_status_label.text().lower()
    window.close()


def test_old_thread_completion_cannot_clear_new_operation(monkeypatch, tmp_path) -> None:
    window = _window(monkeypatch, tmp_path)
    old_thread = QThread()
    old_thread.operation_id = 1
    new_thread = QThread()
    new_thread.operation_id = 2
    old_thread.finished.connect(window._current_thread_finished)
    window._current_operation_id = 2
    window.current_thread = new_thread
    window.current_label = "New operation"

    old_thread.finished.emit()

    assert window.current_thread is new_thread
    assert window.current_label == "New operation"
    window.current_thread = None
    window.close()


def test_remote_busy_locks_profile_and_remote_path_context(monkeypatch, tmp_path) -> None:
    window = _window(monkeypatch, tmp_path)

    window._set_remote_busy(True)

    assert not window.profile_box.isEnabled()
    assert not window.remote_path_edit.isEnabled()

    window._set_remote_busy(False)

    assert window.profile_box.isEnabled()
    assert window.remote_path_edit.isEnabled()
    window.close()


class _BlockingThread(QThread):
    def __init__(self, release: threading.Event) -> None:
        super().__init__()
        self._release = release

    def run(self) -> None:
        self._release.wait(5)


class _CancellableWorker:
    def __init__(self, release: threading.Event) -> None:
        self.release = release
        self.cancel_calls = 0

    def cancel(self) -> None:
        self.cancel_calls += 1
        self.release.set()


def test_close_cancels_without_blocking_gui_thread(monkeypatch, tmp_path) -> None:
    app = _application()
    window = _window(monkeypatch, tmp_path)
    release = threading.Event()
    thread = _BlockingThread(release)
    worker = _CancellableWorker(release)
    window.current_thread = thread
    window.current_worker = worker
    thread.start()
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.Yes)
    event = QCloseEvent()

    started = time.monotonic()
    window.closeEvent(event)
    elapsed = time.monotonic() - started

    assert not event.isAccepted()
    assert elapsed < 0.5
    assert worker.cancel_calls == 1
    deadline = time.monotonic() + 2
    while thread.isRunning() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert thread.wait(1000)
    window._close_when_operations_stop()
    app.processEvents()
    assert not window._close_after_stop


def test_close_decline_leaves_operation_running(monkeypatch, tmp_path) -> None:
    window = _window(monkeypatch, tmp_path)
    release = threading.Event()
    thread = _BlockingThread(release)
    worker = _CancellableWorker(release)
    window.current_thread = thread
    window.current_worker = worker
    thread.start()
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.No)
    event = QCloseEvent()

    window.closeEvent(event)

    assert not event.isAccepted()
    assert worker.cancel_calls == 0
    release.set()
    assert thread.wait(1000)
    window.current_thread = None
    window.close()
