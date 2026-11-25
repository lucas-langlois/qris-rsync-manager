from __future__ import annotations

import threading
import time

from PySide6.QtCore import QItemSelectionModel, QThread, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox, QTableWidgetItem

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
        # Row 0 is the ".." parent row; row 1 is the folder entry.
        item = window.remote_table.item(1, 0)
        assert item is not None
        window._remote_double_clicked(item)
        app.processEvents()
        assert window.remote_table.rowCount() == 2

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
    assert window.remote_table.rowCount() == 2  # ".." plus ready.txt
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
    plan = analyze_upload_tree(source, min_same_type=1)
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
    assert "more than 50 files of the same type" in shown[0]
    assert "uncompressed TAR archive" in shown[0]
    assert "source folder will not be changed" in shown[0]
    assert "will not be deleted automatically" in shown[0]
    assert "survey.tar" in shown[0]
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


def test_collection_mode_swaps_left_pane_and_relabels_transfers(monkeypatch, tmp_path) -> None:
    window = _window(monkeypatch, tmp_path)
    monkeypatch.setattr(window.collection_pane, "refresh", lambda *args, **kwargs: None)

    window.mode_combo.setCurrentIndex(window.mode_combo.findData("collections"))

    assert window.left_stack.currentWidget() is window.collection_pane
    assert "→ right" in window.upload_button.text()
    assert "→ left" in window.download_button.text()
    assert window.build_selection_button.isHidden()

    window.mode_combo.setCurrentIndex(window.mode_combo.findData("local"))

    assert window.left_stack.currentIndex() == 0
    assert window.upload_button.text() == "Upload folder"
    assert not window.build_selection_button.isHidden()
    window.close()


def test_collection_copy_confirms_and_starts_server_side_rsync(monkeypatch, tmp_path) -> None:
    window = _window(monkeypatch, tmp_path)
    profile = window.current_profile()
    assert profile is not None
    monkeypatch.setattr(window.collection_pane, "refresh", lambda *args, **kwargs: None)
    window.mode_combo.setCurrentIndex(window.mode_combo.findData("collections"))
    window.collection_pane.path_edit.setText("/data/Q0202/source")
    window.collection_pane._apply([], "/data/Q0202/source")
    window.remote_path_edit.setText("/data/Q0101/dest")
    window._apply_remote_entries([], "/data/Q0101/dest")
    monkeypatch.setattr(window, "_profile_errors", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(window, "_get_session_passphrase", lambda _profile: "")
    shown: dict[str, str] = {}

    def accept(_parent, title, message, *_args) -> int:
        shown["message"] = message
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", accept)
    started: list[tuple[object, str, dict[str, object]]] = []
    monkeypatch.setattr(window, "_start_worker", lambda worker, label, **kwargs: started.append((worker, label, kwargs)))

    window._start_rsync(dry_run=False, direction="upload")

    assert "/data/Q0202/source\n  → /data/Q0101/dest/source" in shown["message"]
    assert "nothing is deleted" in shown["message"]
    worker, label, kwargs = started[0]
    assert isinstance(worker, FallbackCommandWorker)
    assert label == "Collection copy"
    assert kwargs["refresh_remote"] is True
    remote_command = worker.command_factory(profile)[-1]
    assert remote_command.startswith("rsync ")
    assert remote_command.endswith("-- /data/Q0202/source /data/Q0101/dest/")
    window.close()


def test_collection_copy_requires_both_panes_loaded(monkeypatch, tmp_path) -> None:
    window = _window(monkeypatch, tmp_path)
    monkeypatch.setattr(window.collection_pane, "refresh", lambda *args, **kwargs: None)
    window.mode_combo.setCurrentIndex(window.mode_combo.findData("collections"))
    window.collection_pane.path_edit.setText("/data/Q0202")
    monkeypatch.setattr(window, "_profile_errors", lambda *_args, **_kwargs: [])
    errors: list[list[str]] = []
    monkeypatch.setattr(window, "_show_errors", errors.append)
    started: list[object] = []
    monkeypatch.setattr(window, "_start_worker", lambda *args, **kwargs: started.append(args))

    window._start_rsync(dry_run=True, direction="download")

    assert errors and "Load both panes" in errors[0][0]
    assert not started
    window.close()


def test_close_cancels_collection_pane_listing_without_blocking(monkeypatch, tmp_path) -> None:
    app = _application()
    window = _window(monkeypatch, tmp_path)
    release = threading.Event()
    thread = _BlockingThread(release)
    worker = _CancellableWorker(release)
    window.collection_pane.list_thread = thread
    window.collection_pane.worker = worker
    thread.start()
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.Yes)
    event = QCloseEvent()

    started = time.monotonic()
    window.closeEvent(event)
    elapsed = time.monotonic() - started

    assert not event.isAccepted()
    assert elapsed < 0.5
    assert worker.cancel_calls == 1
    assert thread.wait(2000)
    window.collection_pane.list_thread = None
    window._close_when_operations_stop()
    app.processEvents()
    assert not window._close_after_stop


def test_collection_pane_listing_blocks_new_operations_and_profile_changes(monkeypatch, tmp_path) -> None:
    window = _window(monkeypatch, tmp_path)
    window.collection_pane.list_thread = QThread()
    window._collection_pane_busy_changed(True)
    infos: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda _parent, _title, text, *_args: infos.append(text))

    window._start_worker(FolderScanWorker(str(tmp_path)), "Scan")

    assert window.current_thread is None
    assert infos and "already running" in infos[0]
    assert not window.profile_box.isEnabled()
    assert not window.mode_combo.isEnabled()
    window.collection_pane.list_thread = None
    window._collection_pane_busy_changed(False)
    assert window.profile_box.isEnabled()
    window.close()


def _two_collection_window(monkeypatch, tmp_path) -> MainWindow:
    from app.core.profiles import Profile

    window = _window(monkeypatch, tmp_path)
    window.profiles = [
        Profile(name="Q9560 Girringun", username="me", collection_id="Q9560", remote_path="/data/Q9560"),
        Profile(name="Q8940 Drone", username="me", collection_id="Q8940", remote_path="/data/Q8940"),
        Profile(name="Other login", username="someone", collection_id="Q1111", remote_path="/data/Q1111"),
    ]
    monkeypatch.setattr(window, "_refresh_remote_table", lambda *args, **kwargs: None)
    monkeypatch.setattr(window.collection_pane, "refresh", lambda *args, **kwargs: None)
    window._load_profile_combo("Q9560 Girringun")
    return window


def test_collection_pickers_show_profile_names_for_the_same_login(monkeypatch, tmp_path) -> None:
    window = _two_collection_window(monkeypatch, tmp_path)

    for combo in (window.remote_collection_combo, window.collection_pane.collection_combo):
        labels = [combo.itemText(index) for index in range(combo.count())]
        paths = [combo.itemData(index) for index in range(combo.count())]
        assert labels == ["Q9560 Girringun", "Q8940 Drone"]
        assert paths == ["/data/Q9560", "/data/Q8940"]
    assert window.remote_collection_combo.currentText() == "Q9560 Girringun"
    assert window.collection_pane.path_edit.text() == "/data/Q8940"
    assert window.collection_pane.collection_combo.currentText() == "Q8940 Drone"
    window.close()


def test_remote_collection_picker_navigates_without_changing_profile(monkeypatch, tmp_path) -> None:
    window = _two_collection_window(monkeypatch, tmp_path)

    window._remote_collection_picked(window.remote_collection_combo.findText("Q8940 Drone"))

    assert window.remote_path_edit.text() == "/data/Q8940"
    assert window.profile_combo.currentText() == "Q9560 Girringun"
    window._navigate_remote_path("/data/Q9560/sub")
    assert window.remote_collection_combo.currentText() == "Q9560 Girringun"
    window._navigate_remote_path("/data")
    assert window.remote_collection_combo.currentIndex() == -1
    window.close()


def test_remote_drag_move_is_bound_to_the_displayed_collection(monkeypatch, tmp_path) -> None:
    window = _two_collection_window(monkeypatch, tmp_path)
    source = "/data/Q8940/photo.jpg"
    window._apply_remote_entries([RemoteEntry("f", "photo.jpg", 1, "now", source)], "/data/Q8940")
    window.remote_path_edit.setText("/data/Q8940")
    monkeypatch.setattr(window, "_profile_errors", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(window, "_get_session_passphrase", lambda _profile: "")
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.Yes)
    errors: list[list[str]] = []
    monkeypatch.setattr(window, "_show_errors", errors.append)
    started: list[object] = []
    monkeypatch.setattr(window, "_start_worker", lambda worker, label, **kwargs: started.append(worker))

    window._move_remote_items_by_drop([source], "/data/Q9560/archive")
    assert errors and "inside the active collection" in errors[0][0]
    assert not started

    window._move_remote_items_by_drop([source], "/data/Q8940/archive")
    assert len(started) == 1
    window.close()


def test_collection_mode_greys_out_the_other_panes_collection(monkeypatch, tmp_path) -> None:
    window = _two_collection_window(monkeypatch, tmp_path)

    def enabled(combo, name):
        return combo.model().item(combo.findText(name)).isEnabled()

    assert enabled(window.remote_collection_combo, "Q8940 Drone")
    window.mode_combo.setCurrentIndex(window.mode_combo.findData("collections"))

    assert not enabled(window.remote_collection_combo, "Q8940 Drone")
    assert enabled(window.remote_collection_combo, "Q9560 Girringun")
    assert not enabled(window.collection_pane.collection_combo, "Q9560 Girringun")
    assert enabled(window.collection_pane.collection_combo, "Q8940 Drone")

    window.mode_combo.setCurrentIndex(window.mode_combo.findData("local"))
    assert enabled(window.remote_collection_combo, "Q8940 Drone")
    window.close()


def test_entering_collection_mode_moves_left_pane_off_the_right_collection(monkeypatch, tmp_path) -> None:
    window = _two_collection_window(monkeypatch, tmp_path)
    window._remote_collection_picked(window.remote_collection_combo.findText("Q8940 Drone"))
    assert window.collection_pane.current_path() == "/data/Q8940"

    window.mode_combo.setCurrentIndex(window.mode_combo.findData("collections"))

    assert window.collection_pane.current_path() == "/data/Q9560"
    window.close()


def test_collection_copy_within_one_collection_is_refused(monkeypatch, tmp_path) -> None:
    window = _two_collection_window(monkeypatch, tmp_path)
    window.mode_combo.setCurrentIndex(window.mode_combo.findData("collections"))
    window.collection_pane.path_edit.setText("/data/Q9560/a")
    window.collection_pane._apply([], "/data/Q9560/a")
    window._apply_remote_entries([], "/data/Q9560")
    window.remote_path_edit.setText("/data/Q9560")
    monkeypatch.setattr(window, "_profile_errors", lambda *_args, **_kwargs: [])
    errors: list[list[str]] = []
    monkeypatch.setattr(window, "_show_errors", errors.append)
    started: list[object] = []
    monkeypatch.setattr(window, "_start_worker", lambda *args, **kwargs: started.append(args))

    window._start_rsync(dry_run=True, direction="upload")

    assert errors and "both in Q9560" in errors[0][0]
    assert not started
    window.close()


def test_parent_row_is_not_selectable_and_selection_maps_by_path(monkeypatch, tmp_path) -> None:
    window = _window(monkeypatch, tmp_path)
    entries = [
        RemoteEntry("d", "alpha", 0, "now", "/data/Q0101/sub/alpha"),
        RemoteEntry("f", "beta.txt", 1, "now", "/data/Q0101/sub/beta.txt"),
    ]
    window.remote_path_edit.setText("/data/Q0101/sub")
    window._apply_remote_entries(entries, "/data/Q0101/sub")

    parent = window.remote_table.item(0, 0)
    assert parent.text() == ".."
    assert not (parent.flags() & Qt.ItemIsSelectable)
    window.remote_table.selectRow(0)
    assert window._selected_remote_entries() == []

    window.remote_table.selectRow(2)
    assert window._selected_remote_entries() == [entries[1]]
    window.remote_table.selectAll()
    assert window._selected_remote_entries() == entries
    window.close()


def test_parent_row_is_a_drop_target_for_the_parent_folder(monkeypatch, tmp_path) -> None:
    window = _window(monkeypatch, tmp_path)
    window.remote_path_edit.setText("/data/Q0101/sub")
    window._apply_remote_entries([], "/data/Q0101/sub")

    position = window.remote_table.visualItemRect(window.remote_table.item(0, 0)).center()
    assert window.remote_table._target_folder(position) == "/data/Q0101"
    window.close()


def test_collection_root_listing_shows_parent_row_but_not_at_filesystem_root(monkeypatch, tmp_path) -> None:
    window = _window(monkeypatch, tmp_path)
    window.remote_path_edit.setText("/")
    window._apply_remote_entries([], "/")
    assert window.remote_table.rowCount() == 0
    window.close()


# --- WinSCP-style file actions ------------------------------------------------


def _file_action_window(monkeypatch, tmp_path):
    window = _two_collection_window(monkeypatch, tmp_path)
    window.mode_combo.setCurrentIndex(window.mode_combo.findData("collections"))
    right = [
        RemoteEntry("d", "archive", 0, "now", "/data/Q9560/archive"),
        RemoteEntry("f", "a.txt", 1, "now", "/data/Q9560/a.txt"),
    ]
    left = [
        RemoteEntry("d", "inbox", 0, "now", "/data/Q8940/inbox"),
        RemoteEntry("f", "b.txt", 1, "now", "/data/Q8940/b.txt"),
    ]
    window.remote_path_edit.setText("/data/Q9560")
    window._apply_remote_entries(right, "/data/Q9560")
    window.collection_pane.path_edit.setText("/data/Q8940")
    window.collection_pane._apply(left, "/data/Q8940")
    monkeypatch.setattr(window, "_profile_errors", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(window, "_get_session_passphrase", lambda _profile: "")
    started: list[tuple[object, str, dict[str, object]]] = []
    monkeypatch.setattr(window, "_start_worker", lambda worker, label, **kwargs: started.append((worker, label, kwargs)))
    errors: list[list[str]] = []
    monkeypatch.setattr(window, "_show_errors", errors.append)
    return window, started, errors


def _select_by_name(table, name: str) -> None:
    table.clearSelection()
    for row in range(table.rowCount()):
        if table.item(row, 0).text() == name:
            table.selectRow(row)
            return
    raise AssertionError(name)


def test_left_pane_new_folder_rename_and_delete_run_in_left_collection(monkeypatch, tmp_path) -> None:
    window, started, errors = _file_action_window(monkeypatch, tmp_path)
    profile = window.current_profile()
    texts = iter([("New stuff", True), ("renamed.txt", True), ("DELETE", True)])
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: next(texts))
    refreshed: list[bool] = []
    monkeypatch.setattr(window.collection_pane, "refresh", lambda force=False: refreshed.append(force))

    window.collection_pane.new_folder_button.click()
    _select_by_name(window.collection_pane.table, "b.txt")
    window.collection_pane.rename_button.click()
    window.collection_pane.delete_button.click()

    assert not errors
    commands = [worker.command_factory(profile)[-1] for worker, _label, _kwargs in started]
    assert "mkdir -p -- '/data/Q8940/New stuff'" in commands[0]
    assert "mv -- /data/Q8940/b.txt /data/Q8940/renamed.txt" in commands[1]
    assert "rm -rf -- /data/Q8940/b.txt" in commands[2]
    for _worker, _label, kwargs in started:
        assert kwargs["refresh_remote"] is False
        kwargs["cleanup"](None, 1)  # refreshes even after a failure
    assert refreshed == [True, True, True]
    window.close()


def test_remote_rename_is_name_only(monkeypatch, tmp_path) -> None:
    window, started, errors = _file_action_window(monkeypatch, tmp_path)
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: ("/data/Q8940/escaped.txt", True))
    _select_by_name(window.remote_table, "a.txt")

    window._remote_rename("right")

    assert errors and "name only" in errors[0][0]
    assert not started
    window.close()


def test_left_pane_drag_moves_within_its_collection(monkeypatch, tmp_path) -> None:
    window, started, errors = _file_action_window(monkeypatch, tmp_path)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.Yes)

    window.collection_pane.table.moveDropped.emit(["/data/Q8940/b.txt"], "/data/Q8940/inbox")

    assert not errors
    command = started[0][0].command_factory(window.current_profile())[-1]
    assert "mv -n -- /data/Q8940/b.txt /data/Q8940/inbox/b.txt" in command
    window.close()


def test_cut_and_paste_moves_within_the_same_pane_only(monkeypatch, tmp_path) -> None:
    window, started, errors = _file_action_window(monkeypatch, tmp_path)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.Yes)
    _select_by_name(window.remote_table, "a.txt")
    window._cut_items("right")

    window._paste_items("left")
    assert errors and "same pane" in errors[0][0]
    assert window._has_cut_items("right")  # a refused cross-pane paste keeps the cut list

    _select_by_name(window.remote_table, "archive")
    window._paste_items("right")
    command = started[0][0].command_factory(window.current_profile())[-1]
    assert "mv -n -- /data/Q9560/a.txt /data/Q9560/archive/a.txt" in command
    assert not window._has_cut_items("right")
    window.close()


def test_paste_into_another_collection_is_refused(monkeypatch, tmp_path) -> None:
    window, started, errors = _file_action_window(monkeypatch, tmp_path)
    _select_by_name(window.remote_table, "a.txt")
    window._cut_items("right")
    window.remote_path_edit.setText("/data/Q8940")
    window._apply_remote_entries([], "/data/Q8940")

    window._paste_items("right")

    assert errors and "inside the active collection" in errors[0][0]
    assert not started
    window.close()


def test_shortcut_actions_respect_disabled_buttons(monkeypatch, tmp_path) -> None:
    window, started, errors = _file_action_window(monkeypatch, tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(window, "_remote_delete_selected", lambda side="right": calls.append(side))
    window._install_pane_actions()  # rebind to the patched handler
    _select_by_name(window.remote_table, "a.txt")
    delete = window.remote_table.pane_actions["Delete…"]

    window.remote_delete_button.setEnabled(False)
    delete.trigger()
    assert calls == []

    window.remote_delete_button.setEnabled(True)
    delete.trigger()
    assert calls == ["right"]
    window.close()


def test_right_click_selects_row_under_cursor_and_clears_on_empty_space(monkeypatch, tmp_path) -> None:
    from PySide6.QtCore import QPoint

    from app.gui.pane_actions import _select_row_under_cursor

    window, _started, _errors = _file_action_window(monkeypatch, tmp_path)
    table = window.remote_table
    window.resize(1200, 800)
    window.show()
    _application().processEvents()
    _select_by_name(table, "archive")

    a_row = next(row for row in range(table.rowCount()) if table.item(row, 0).text() == "a.txt")
    _select_row_under_cursor(table, table.visualItemRect(table.item(a_row, 0)).center())
    assert [entry.name for entry in window._selected_remote_entries()] == ["a.txt"]

    _select_row_under_cursor(table, table.visualItemRect(table.item(0, 0)).center())  # ".." row
    assert window._selected_remote_entries() == []

    _select_by_name(table, "a.txt")
    _select_row_under_cursor(table, QPoint(5, table.viewport().height() - 5))
    assert window._selected_remote_entries() == []
    window.close()


def test_shortcuts_are_bound_to_the_list_not_the_path_field(monkeypatch, tmp_path) -> None:
    window, _started, _errors = _file_action_window(monkeypatch, tmp_path)
    for view in (window.local_tree, window.remote_table, window.collection_pane.table):
        for action in view.pane_actions.values():
            assert action.shortcutContext() == Qt.WidgetShortcut
    labels = {action.shortcut().toString(): label for label, action in window.remote_table.pane_actions.items()}
    expected = {
        "F2": "Rename…",
        "F5": "Refresh",
        "F6": "Move to…",
        "F7": "New folder…",
        "Del": "Delete…",
        "Backspace": "Up one level",
        "Ctrl+X": "Cut",
        "Ctrl+V": "Paste (move here)",
    }
    for key, label in expected.items():
        assert labels[key] == label
    window.close()


def test_local_cut_and_paste_moves_to_another_folder_on_the_same_drive(monkeypatch, tmp_path) -> None:
    window, started, errors = _file_action_window(monkeypatch, tmp_path)
    window.mode_combo.setCurrentIndex(window.mode_combo.findData("local"))
    source_dir = tmp_path / "from"
    target_dir = tmp_path / "to"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "data.csv").write_text("x", encoding="utf-8")
    monkeypatch.setattr(window, "_selected_local_paths", lambda: [source_dir / "data.csv"])
    window._cut_items("local")
    monkeypatch.setattr(window, "_selected_local_paths", lambda: [])
    window.local_folder_edit.setText(str(target_dir))
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.Yes)

    window._paste_items("local")

    assert not errors
    worker, _label, _kwargs = started[0]
    assert isinstance(worker, LocalMoveWorker)
    assert worker.destination == target_dir
    assert str(worker.allowed_root) == target_dir.anchor
    window.close()


def test_local_rename_is_name_only(monkeypatch, tmp_path) -> None:
    window, _started, errors = _file_action_window(monkeypatch, tmp_path)
    item = tmp_path / "keep.txt"
    item.write_text("x", encoding="utf-8")
    monkeypatch.setattr(window, "_selected_local_paths", lambda: [item])
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: (str(tmp_path / "elsewhere" / "x.txt"), True))

    window._local_rename()

    assert errors and "name only" in errors[0][0]
    assert item.exists()
    window.close()


def test_real_key_presses_reach_list_actions_but_not_the_path_field(monkeypatch, tmp_path) -> None:
    import pytest
    from PySide6.QtTest import QTest

    app = _application()
    window = _window(monkeypatch, tmp_path)
    window.show()
    window.activateWindow()
    if not QTest.qWaitForWindowActive(window, 2000):
        window.close()
        pytest.skip("This Qt platform cannot activate windows, so shortcuts cannot be exercised.")
    window.remote_path_edit.setText("/data/Q0101/sub")
    window._apply_remote_entries([RemoteEntry("f", "a.txt", 1, "now", "/data/Q0101/sub/a.txt")], "/data/Q0101/sub")
    calls: list[str] = []
    monkeypatch.setattr(window, "_remote_rename", lambda side="right": calls.append("F2"))
    monkeypatch.setattr(window, "_remote_delete_selected", lambda side="right": calls.append("Del"))
    monkeypatch.setattr(window, "_remote_go_up", lambda: calls.append("Backspace"))
    window._install_pane_actions()  # reinstalling must replace, not duplicate, the shortcuts
    table = window.remote_table
    table.setFocus()
    table.selectRow(1)
    app.processEvents()

    for key in (Qt.Key_F2, Qt.Key_Delete, Qt.Key_Backspace):
        QTest.keyClick(table, key)
        app.processEvents()
    assert calls == ["F2", "Del", "Backspace"]

    calls.clear()
    window.remote_path_edit.setFocus()
    app.processEvents()
    for key in (Qt.Key_Delete, Qt.Key_Backspace):
        QTest.keyClick(window.remote_path_edit, key)
        app.processEvents()
    assert calls == []
    window.close()


def test_context_menu_runs_and_enables_actions_for_the_selection(monkeypatch, tmp_path) -> None:
    from PySide6.QtCore import QPoint

    window, _started, _errors = _file_action_window(monkeypatch, tmp_path)
    window.resize(1200, 800)
    window.show()
    _application().processEvents()
    table = window.remote_table
    seen: list[dict[str, bool]] = []

    def fake_show(menu, _position) -> None:
        seen.append({action.text(): action.isEnabled() for action in menu.actions() if action.text()})

    import app.gui.pane_actions as pane_actions_module

    monkeypatch.setattr(pane_actions_module, "_show_menu", fake_show)
    a_row = next(row for row in range(table.rowCount()) if table.item(row, 0).text() == "a.txt")
    table.customContextMenuRequested.emit(table.visualItemRect(table.item(a_row, 0)).center())
    table.customContextMenuRequested.emit(QPoint(5, table.viewport().height() - 5))

    on_item, on_empty = seen
    assert on_item["Rename…"] and on_item["Delete…"] and on_item["Cut"] and on_item["Move to…"]
    assert not on_item["Paste (move here)"]
    assert not on_empty["Delete…"] and not on_empty["Cut"] and not on_empty["Move to…"]
    assert on_empty["New folder…"] and on_empty["Refresh"]
    # Shortcuts stay live after the menu closes; they re-check state when pressed.
    assert all(action.isEnabled() for action in table.pane_actions.values())
    window.close()


class _FakeBrowser:
    chosen = ""

    def __init__(self, profile, ssh_path, start_path, passphrase, parent=None) -> None:
        self.selected_path = _FakeBrowser.chosen

    def setWindowTitle(self, _title) -> None:
        pass

    def exec(self):
        from PySide6.QtWidgets import QDialog

        return QDialog.Accepted


def test_remote_move_to_uses_folder_picker_and_stays_in_collection(monkeypatch, tmp_path) -> None:
    import app.gui.main_window as main_window_module

    window, started, errors = _file_action_window(monkeypatch, tmp_path)
    monkeypatch.setattr(main_window_module, "RemoteBrowserDialog", _FakeBrowser)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.Yes)
    _select_by_name(window.remote_table, "a.txt")

    _FakeBrowser.chosen = "/data/Q9560/archive/2025"
    window._remote_move_to("right")
    command = started[0][0].command_factory(window.current_profile())[-1]
    assert "mv -n -- /data/Q9560/a.txt /data/Q9560/archive/2025/a.txt" in command

    _FakeBrowser.chosen = "/data/Q8940/inbox"
    window._remote_move_to("right")
    assert len(started) == 1
    assert errors and "inside the active collection" in errors[0][0]
    window.close()


def test_local_move_to_uses_folder_picker_and_drive_root(monkeypatch, tmp_path) -> None:
    from PySide6.QtWidgets import QFileDialog

    window, started, errors = _file_action_window(monkeypatch, tmp_path)
    window.mode_combo.setCurrentIndex(window.mode_combo.findData("local"))
    item = tmp_path / "survey.csv"
    item.write_text("x", encoding="utf-8")
    destination = tmp_path / "far" / "away"
    destination.mkdir(parents=True)
    monkeypatch.setattr(window, "_selected_local_paths", lambda: [item])
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_args, **_kwargs: str(destination))
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.Yes)

    window._local_move_to()

    assert not errors
    worker = started[0][0]
    assert isinstance(worker, LocalMoveWorker)
    assert worker.destination == destination
    assert str(worker.allowed_root) == destination.anchor
    window.close()


def test_local_move_to_another_drive_is_refused(monkeypatch, tmp_path) -> None:
    window, started, errors = _file_action_window(monkeypatch, tmp_path)
    here = tmp_path.anchor.rstrip("\\/").upper()
    other_drive = "Q:" if here != "Q:" else "R:"

    started_ok = window._move_local_items([f"{other_drive}\\data\\file.txt"], tmp_path, title="Move to…")

    assert started_ok is False
    assert errors and "same drive" in errors[0][0]
    assert not started
    window.close()


# --- Review fixes ---------------------------------------------------------------


def test_refresh_keeps_selection_by_path_not_row_number(monkeypatch, tmp_path) -> None:
    window, _started, _errors = _file_action_window(monkeypatch, tmp_path)
    for table, apply, path, prefix in (
        (window.remote_table, window._apply_remote_entries, "/data/Q9560", "/data/Q9560"),
        (window.collection_pane.table, lambda e, p: window.collection_pane._apply(e, p), "/data/Q8940", "/data/Q8940"),
    ):
        entries = [RemoteEntry("f", "a.txt", 1, "now", f"{prefix}/a.txt")]
        apply(entries, path)
        _select_by_name(table, "a.txt")
        # A refresh that adds an item above the selected one.
        apply([RemoteEntry("d", "IMPORTANT", 0, "now", f"{prefix}/IMPORTANT"), *entries], path)
        selected = [table.item(index.row(), 0).text() for index in table.selectionModel().selectedRows()]
        assert selected == ["a.txt"]
    window.close()


def test_navigating_to_another_folder_clears_the_selection(monkeypatch, tmp_path) -> None:
    window, _started, _errors = _file_action_window(monkeypatch, tmp_path)
    _select_by_name(window.remote_table, "a.txt")
    window.remote_path_edit.setText("/data/Q9560/archive")
    window._apply_remote_entries(
        [RemoteEntry("d", "raw", 0, "now", "/data/Q9560/archive/raw"), RemoteEntry("f", "x", 1, "now", "/data/Q9560/archive/x")],
        "/data/Q9560/archive",
    )
    assert window._selected_remote_entries() == []

    _select_by_name(window.collection_pane.table, "b.txt")
    window.collection_pane.path_edit.setText("/data/Q8940/inbox")
    window.collection_pane._apply(
        [RemoteEntry("d", "keep", 0, "now", "/data/Q8940/inbox/keep"), RemoteEntry("f", "y", 1, "now", "/data/Q8940/inbox/y")],
        "/data/Q8940/inbox",
    )
    assert window.collection_pane.selected_entries() == []
    window.close()


def test_paste_after_opening_a_folder_lands_in_that_folder(monkeypatch, tmp_path) -> None:
    window, started, errors = _file_action_window(monkeypatch, tmp_path)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.Yes)
    _select_by_name(window.remote_table, "a.txt")
    window._cut_items("right")
    _select_by_name(window.remote_table, "archive")  # double-click selects the row first
    window.remote_path_edit.setText("/data/Q9560/archive")
    window._apply_remote_entries([RemoteEntry("d", "raw_data", 0, "now", "/data/Q9560/archive/raw_data")], "/data/Q9560/archive")

    window._paste_items("right")

    command = started[0][0].command_factory(window.current_profile())[-1]
    assert "/data/Q9560/archive/a.txt" in command
    assert "raw_data" not in command
    window.close()


def test_local_selection_is_cleared_when_the_displayed_folder_changes(monkeypatch, tmp_path) -> None:
    app = _application()
    window = _window(monkeypatch, tmp_path)
    folder_a = tmp_path / "A"
    folder_b = tmp_path / "B"
    folder_a.mkdir()
    folder_b.mkdir()
    (folder_a / "f.txt").write_text("x", encoding="utf-8")
    window._set_local_root(str(folder_a))
    index = window.local_model.index(str(folder_a / "f.txt"))
    window.local_tree.selectionModel().select(index, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
    assert window._selected_local_paths()

    window._set_local_root(str(folder_b))
    app.processEvents()

    assert window._selected_local_paths() == []
    window.close()


def test_profile_switch_in_collection_mode_keeps_panes_on_different_collections(monkeypatch, tmp_path) -> None:
    window = _two_collection_window(monkeypatch, tmp_path)
    window.mode_combo.setCurrentIndex(window.mode_combo.findData("collections"))
    assert window.collection_pane.current_path() == "/data/Q8940"

    window.profile_combo.setCurrentIndex(window.profile_combo.findText("Q8940 Drone"))

    assert window.remote_path_edit.text() == "/data/Q8940"
    assert window.collection_pane.current_path() == "/data/Q9560"
    window.close()


def test_cleanup_runs_after_stop_but_continuation_does_not(monkeypatch, tmp_path) -> None:
    app = _application()
    window = _window(monkeypatch, tmp_path)
    calls: list[str] = []

    window._start_worker(
        FolderScanWorker(str(tmp_path)),
        "Scan",
        after_finish=lambda _worker, _code: calls.append("after_finish"),
        cleanup=lambda _worker, _code: calls.append("cleanup"),
    )
    window._cancel_current()
    deadline = time.monotonic() + 5
    while window.current_thread is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    assert calls == ["cleanup"]
    window.close()


def test_remote_new_folder_needs_the_folder_to_be_loaded(monkeypatch, tmp_path) -> None:
    window, started, errors = _file_action_window(monkeypatch, tmp_path)
    window.remote_path_edit.setText("/data/Q9560/typo/deep")  # typed, never listed

    window._remote_new_folder("right")

    assert errors and "Load this folder first" in errors[0][0]
    assert not started
    window.close()


def test_pickers_treat_data_and_qrisdata_as_the_same_collection(monkeypatch, tmp_path) -> None:
    window = _two_collection_window(monkeypatch, tmp_path)
    window.mode_combo.setCurrentIndex(window.mode_combo.findData("collections"))
    window.collection_pane.path_edit.setText("/QRISdata/Q9560/sub")
    window.collection_pane._path_edited()

    combo = window.remote_collection_combo
    assert not combo.model().item(combo.findText("Q9560 Girringun")).isEnabled()
    assert window.collection_pane.collection_combo.currentText() == "Q9560 Girringun"
    window.close()
