from __future__ import annotations

import threading
import time

from PySide6.QtCore import QItemSelectionModel, QThread, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMessageBox, QTableWidgetItem

from app.core.archive_upload import analyze_upload_tree
from app.core.remote_dirs import RemoteEntry
from app.gui.main_window import (
    FallbackCommandWorker,
    FolderScanWorker,
    LocalMoveWorker,
    MainWindow,
    PackagedUploadWorker,
    UploadAnalysisWorker,
)
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


def test_remote_double_click_queues_folder_until_previous_listing_thread_clears(monkeypatch, tmp_path) -> None:
    app = _application()
    window = _window(monkeypatch, tmp_path)
    release = threading.Event()
    thread = _BlockingThread(release)
    thread.request_id = 1
    window._remote_request_id = 1
    window.remote_thread = thread
    thread.finished.connect(window._remote_thread_finished)
    thread.start()
    item = QTableWidgetItem("child")
    item.setData(Qt.UserRole, "/data/Q9560/child")
    item.setData(Qt.UserRole + 1, True)

    window._remote_double_clicked(item)
    app.processEvents()

    assert window.remote_path_edit.text() == "/data/Q9560/child"
    assert window._pending_remote_path == "/data/Q9560/child"
    assert "Waiting to open" in window.remote_status_label.text()

    refreshed: list[str] = []
    monkeypatch.setattr(window, "_refresh_remote_table", lambda force=False: refreshed.append(window.remote_path_edit.text()))
    release.set()
    deadline = time.monotonic() + 2
    while thread.isRunning() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert thread.wait(1000)
    app.processEvents()

    assert refreshed == ["/data/Q9560/child"]
    assert window._pending_remote_path is None
    assert window.remote_thread is None
    window.close()


def test_remote_double_click_defers_navigation_until_signal_returns(monkeypatch, tmp_path) -> None:
    app = _application()
    window = _window(monkeypatch, tmp_path)
    navigated: list[str] = []
    monkeypatch.setattr(window, "_navigate_remote_path", navigated.append)
    item = QTableWidgetItem("child")
    item.setData(Qt.UserRole, "/data/Q9560/child")
    item.setData(Qt.UserRole + 1, True)

    window._remote_double_clicked(item)

    assert navigated == []
    app.processEvents()
    assert navigated == ["/data/Q9560/child"]
    window.close()


def test_repeated_cached_double_click_navigation_keeps_table_items_valid(monkeypatch, tmp_path) -> None:
    app = _application()
    window = _window(monkeypatch, tmp_path)
    profile = window.current_profile()
    assert profile is not None
    paths = ("/data/Q9560/one", "/data/Q9560/two")
    for current, target in ((paths[0], paths[1]), (paths[1], paths[0])):
        window.remote_directory_cache.put(
            profile,
            current,
            [RemoteEntry(kind="d", name=target.rsplit("/", 1)[-1], size=0, modified="now", path=target)],
        )
    window.remote_path_edit.setText(paths[0])
    window._refresh_remote_table()

    for _ in range(100):
        item = window.remote_table.item(0, 0)
        assert item is not None
        window._remote_double_clicked(item)
        app.processEvents()
        assert window.remote_table.rowCount() == 1

    assert window.remote_thread is None
    window.close()


def test_repeated_threaded_remote_navigation_keeps_workers_alive_until_result_delivery(monkeypatch, tmp_path) -> None:
    app = _application()
    window = _window(monkeypatch, tmp_path)
    monkeypatch.setattr(window, "_profile_errors", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(window, "_get_session_passphrase", lambda *_args: "")

    class FakeProcess:
        returncode = 0

        def __init__(self, command: list[str]) -> None:
            remote_path = command[-1].split(" -mindepth", 1)[0].removeprefix("find ").strip("'")
            child_path = f"{remote_path.rstrip('/')}/child"
            self.output = f"d\tchild\t0\t2026-01-01 00:00\t{child_path}\n"

        def poll(self):
            return self.returncode

        def communicate(self, timeout=None):
            return self.output, None

    monkeypatch.setattr(
        "app.gui.remote_browser_dialog.subprocess.Popen",
        lambda command, **_kwargs: FakeProcess(command),
    )
    window.remote_path_edit.setText("/data/Q9560")

    for _ in range(30):
        window._refresh_remote_table(force=True)
        deadline = time.monotonic() + 2
        while window.remote_thread is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.001)
        assert window.remote_thread is None
        item = window.remote_table.item(0, 0)
        assert item is not None
        window._remote_double_clicked(item)
        app.processEvents()

    deadline = time.monotonic() + 2
    while window.remote_thread is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.001)
    assert window.remote_thread is None
    window.close()


def test_cached_remote_navigation_does_not_start_ssh(monkeypatch, tmp_path) -> None:
    window = _window(monkeypatch, tmp_path)
    profile = window.current_profile()
    assert profile is not None
    target = "/data/Q0101/cached"
    entries = [RemoteEntry(kind="f", name="ready.txt", size=1, modified="now", path=f"{target}/ready.txt")]
    window.remote_directory_cache.put(profile, target, entries)
    window.remote_path_edit.setText(target)
    monkeypatch.setattr(
        window,
        "_get_session_passphrase",
        lambda *_args: (_ for _ in ()).throw(AssertionError("cache hit attempted SSH authentication")),
    )

    window._refresh_remote_table()

    assert window.remote_thread is None
    assert window.remote_entries == entries
    assert "cached" in window.remote_status_label.text().lower()
    window.close()


def test_cached_remote_navigation_populates_immediately_while_previous_refresh_finishes(monkeypatch, tmp_path) -> None:
    window = _window(monkeypatch, tmp_path)
    profile = window.current_profile()
    assert profile is not None
    target = "/data/Q0101/cached"
    entries = [RemoteEntry(kind="f", name="ready.txt", size=1, modified="now", path=f"{target}/ready.txt")]
    window.remote_directory_cache.put(profile, target, entries)
    window.remote_thread = QThread()
    window.remote_path_edit.setText(target)

    window._refresh_remote_table()

    assert window.remote_entries == entries
    assert window.remote_entries_path == target
    assert window.remote_table.rowCount() == 1
    assert window.remote_table.isEnabled()
    assert window._pending_remote_path == target
    assert "cached" in window.remote_status_label.text().lower()
    window.remote_thread = None
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


def test_selected_directory_starts_background_archive_scan(monkeypatch, tmp_path) -> None:
    window = _window(monkeypatch, tmp_path)
    window.transfer_scope_combo.setCurrentIndex(window.transfer_scope_combo.findData("selected"))
    monkeypatch.setattr(window, "_selected_local_paths", lambda: [tmp_path])
    monkeypatch.setattr(window, "_profile_errors", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("app.gui.main_window.validate_transfer_inputs", lambda *_args, **_kwargs: [])
    started: list[object] = []
    monkeypatch.setattr(window, "_start_worker", lambda *args, **kwargs: started.append(args[0]))

    window._start_rsync(dry_run=False, direction="upload")

    assert len(started) == 1
    assert isinstance(started[0], UploadAnalysisWorker)
    assert started[0].path == str(tmp_path)
    window.close()


def test_selected_local_folder_is_counted_as_an_item(monkeypatch, tmp_path) -> None:
    window = _window(monkeypatch, tmp_path)
    window.transfer_scope_combo.setCurrentIndex(window.transfer_scope_combo.findData("selected"))
    monkeypatch.setattr(window, "_selected_local_paths", lambda: [tmp_path])

    window._update_transfer_scope_ui()

    assert window.upload_button.text() == "Upload selected items (1)"
    assert window.upload_button.isEnabled()
    assert "1 local item(s)" in window.selection_status_label.text()
    window.close()


def test_real_local_tree_folder_selection_updates_transfer_scope(monkeypatch, tmp_path) -> None:
    folder = tmp_path / "Lucinda"
    folder.mkdir()
    app = _application()
    window = _window(monkeypatch, tmp_path)
    window._set_local_root(str(tmp_path))
    deadline = time.monotonic() + 2
    index = window.local_model.index(str(folder))
    while not index.isValid() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
        index = window.local_model.index(str(folder))
    assert index.isValid()

    window.transfer_scope_combo.setCurrentIndex(window.transfer_scope_combo.findData("selected"))
    window.local_tree.selectionModel().select(
        index,
        QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows,
    )
    app.processEvents()

    assert window._selected_local_paths() == [folder]
    assert window.upload_button.text() == "Upload selected items (1)"
    assert window.upload_button.isEnabled()
    window.close()


def test_local_drag_move_confirms_and_starts_background_worker(monkeypatch, tmp_path) -> None:
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"photo")
    destination = tmp_path / "archive"
    destination.mkdir()
    window = _window(monkeypatch, tmp_path)
    window.local_folder_edit.setText(str(tmp_path))
    shown: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda _parent, _title, message, *_args: shown.append(message) or QMessageBox.Yes,
    )
    started: list[tuple[object, str, dict[str, object]]] = []
    monkeypatch.setattr(
        window,
        "_start_worker",
        lambda worker, label, **kwargs: started.append((worker, label, kwargs)),
    )

    window._move_local_items_by_drop([str(source)], str(destination))

    assert str(source) in shown[0]
    assert str(destination / source.name) in shown[0]
    assert isinstance(started[0][0], LocalMoveWorker)
    assert started[0][1] == "Move local items"
    assert started[0][2]["refresh_local"] is True
    window.close()


def test_remote_drag_move_is_collection_bound_and_starts_fallback_worker(monkeypatch, tmp_path) -> None:
    window = _window(monkeypatch, tmp_path)
    profile = window.current_profile()
    assert profile is not None
    root = f"/data/{profile.collection_id}"
    source = f"{root}/photo.jpg"
    destination = f"{root}/archive"
    window.remote_entries = [RemoteEntry("f", "photo.jpg", 1, "now", source)]
    monkeypatch.setattr(window, "_profile_errors", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(window, "_get_session_passphrase", lambda _profile: "")
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.Yes)
    started: list[tuple[object, str, dict[str, object]]] = []
    monkeypatch.setattr(
        window,
        "_start_worker",
        lambda worker, label, **kwargs: started.append((worker, label, kwargs)),
    )

    window._move_remote_items_by_drop([source], destination)

    assert isinstance(started[0][0], FallbackCommandWorker)
    command = started[0][0].command_factory(profile)
    assert f"mv -n -- {source} {destination}/photo.jpg" in command[-1]
    assert started[0][1] == "Move remote items"
    assert started[0][2]["refresh_remote"] is True
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
    assert isinstance(started[0][0], UploadAnalysisWorker)
    assert started[0][1] == "Analyse upload folder"
    assert callable(started[0][2])
    resolved: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        window,
        "_start_resolved_rsync",
        lambda *args, **kwargs: resolved.append((args, kwargs)),
    )
    started[0][0].result = analyze_upload_tree(tmp_path)
    started[0][2](started[0][0], 0)
    assert resolved[0][0][3] == "folder"
    assert resolved[0][0][8] == str(tmp_path)
    assert resolved[0][1]["include_source_directory"] is True
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


def test_packaged_upload_confirmation_explains_archives_and_starts_packaged_worker(monkeypatch, tmp_path) -> None:
    window = _window(monkeypatch, tmp_path)
    profile = window.current_profile()
    assert profile is not None
    source = tmp_path / "survey"
    source.mkdir()
    (source / "one.jpg").write_bytes(b"123")
    (source / "two.jpg").write_bytes(b"456")
    plan = analyze_upload_tree(source, min_flat_files=1, min_folder_bytes=5)
    shown: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda _parent, _title, message, *_args: shown.append(message) or QMessageBox.Yes,
    )
    monkeypatch.setattr(window, "_get_session_passphrase", lambda _profile: "")
    started: list[tuple[object, str]] = []
    monkeypatch.setattr(window, "_start_worker", lambda worker, label, **_kwargs: started.append((worker, label)))

    window._start_resolved_rsync(
        profile,
        False,
        "upload",
        "folder",
        [],
        [],
        None,
        None,
        str(source),
        "/data/Q0101",
        None,
        None,
        scan=plan.scan,
        archive_plan=plan,
    )

    assert "Automatic media archiving" in shown[0]
    assert "more than 200 direct files or more than 10 GB" in shown[0]
    assert "separate uncompressed TAR archives" in shown[0]
    assert "source folder will not be changed" in shown[0]
    assert "will not be deleted automatically" in shown[0]
    assert "survey__photos.tar" in shown[0]
    assert isinstance(started[0][0], PackagedUploadWorker)
    assert started[0][1] == "Packaged upload"
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
