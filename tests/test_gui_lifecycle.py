from __future__ import annotations

import threading
import time

from PySide6.QtCore import QThread
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from app.core.remote_dirs import RemoteEntry
from app.gui.main_window import FolderScanWorker, MainWindow
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


def test_selected_directory_is_not_silently_uploaded_as_folder(monkeypatch, tmp_path) -> None:
    window = _window(monkeypatch, tmp_path)
    window.transfer_scope_combo.setCurrentIndex(window.transfer_scope_combo.findData("selected"))
    monkeypatch.setattr(window, "_selected_local_paths", lambda: [tmp_path])
    errors: list[str] = []
    monkeypatch.setattr(window, "_show_errors", lambda messages: errors.extend(messages))
    started: list[object] = []
    monkeypatch.setattr(window, "_start_worker", lambda *args, **kwargs: started.append(args[0]))

    window._start_rsync(dry_run=False, direction="upload")

    assert not started
    assert any("do not include folders" in message for message in errors)
    window.close()


def test_folder_upload_starts_background_scan_before_transfer(monkeypatch, tmp_path) -> None:
    window = _window(monkeypatch, tmp_path)
    window.local_folder_edit.setText(str(tmp_path))
    monkeypatch.setattr(window, "_profile_errors", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("app.gui.main_window.validate_transfer_inputs", lambda *_args, **_kwargs: [])
    started: list[tuple[object, str, object]] = []

    def capture(worker, label, **kwargs) -> None:
        started.append((worker, label, kwargs.get("after_finish")))

    monkeypatch.setattr(window, "_start_worker", capture)

    window._start_rsync(dry_run=False, direction="upload")

    assert len(started) == 1
    assert isinstance(started[0][0], FolderScanWorker)
    assert started[0][1] == "Scan upload folder"
    assert callable(started[0][2])
    window.close()


def test_transfer_confirmation_shows_scope_paths_and_dry_run(monkeypatch, tmp_path) -> None:
    window = _window(monkeypatch, tmp_path)
    profile = window.current_profile()
    assert profile is not None
    shown: dict[str, str] = {}

    def decline(_parent, title, message, *_args) -> int:
        shown["title"] = title
        shown["message"] = message
        return QMessageBox.No

    monkeypatch.setattr(QMessageBox, "question", decline)
    started: list[object] = []
    monkeypatch.setattr(window, "_start_worker", lambda *args, **kwargs: started.append(args[0]))

    window._start_resolved_rsync(
        profile,
        True,
        "upload",
        "folder",
        [],
        [],
        None,
        None,
        str(tmp_path),
        "/data/Q0101",
        None,
        None,
    )

    assert shown["title"] == "Confirm upload"
    assert str(tmp_path) in shown["message"]
    assert "/data/Q0101" in shown["message"]
    assert "Dry run / compare" in shown["message"]
    assert not started
    window.close()


def test_remote_listing_busy_disables_transfer_actions(monkeypatch, tmp_path) -> None:
    window = _window(monkeypatch, tmp_path)

    window._set_remote_busy(True)

    assert not window.transfer_scope_combo.isEnabled()
    assert not window.upload_button.isEnabled()
    assert not window.download_button.isEnabled()
    assert not window.dry_run_button.isEnabled()
    assert not window.build_selection_button.isEnabled()
    assert not window.recall_button.isEnabled()
    window._set_remote_busy(False)
    window.close()


def test_build_sync_selection_refuses_remote_listing_overlap(monkeypatch, tmp_path) -> None:
    window = _window(monkeypatch, tmp_path)
    window.remote_thread = QThread()
    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda _parent, _title, message, *_args: shown.append(message))

    window._build_sync_selection()

    assert window.compare_thread is None
    assert shown == ["Wait for the current operation to finish."]
    window.remote_thread = None
    window.close()


def test_close_suppresses_successful_scan_continuation(monkeypatch, tmp_path) -> None:
    window = _window(monkeypatch, tmp_path)
    called: list[bool] = []
    worker = FolderScanWorker(str(tmp_path))
    window.current_worker = worker
    window.current_after_finish = lambda *_args: called.append(True)
    window.current_exit_code = 0
    window.current_finished_handled = True
    window._close_after_stop = True
    thread = QThread()
    thread.operation_id = 1
    window._current_operation_id = 1
    window.current_thread = thread
    thread.finished.connect(window._current_thread_finished)

    thread.finished.emit()

    assert not called
    window._close_after_stop = False
    window.close()


def test_profile_save_failure_preserves_in_memory_profiles(monkeypatch, tmp_path) -> None:
    window = _window(monkeypatch, tmp_path)
    original = list(window.profiles)
    messages: list[str] = []
    monkeypatch.setattr("app.gui.main_window.save_profiles", lambda _profiles: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(QMessageBox, "critical", lambda _parent, _title, message, *_args: messages.append(message))

    saved = window._persist_profiles([*original, original[0]])

    assert not saved
    assert window.profiles == original
    assert "disk full" in messages[0]
    window.close()


def test_path_fields_keep_useful_width_at_normal_window_size(monkeypatch, tmp_path) -> None:
    app = _application()
    window = _window(monkeypatch, tmp_path)
    window.resize(1200, 800)
    window.show()
    app.processEvents()

    assert window.local_folder_edit.width() >= 300
    assert window.remote_path_edit.width() >= 300
    window.close()
