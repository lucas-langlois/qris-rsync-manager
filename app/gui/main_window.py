from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFileSystemModel,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QInputDialog,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from app.core.askpass import build_askpass_environment, scrub_askpass_environment
from app.core.logging_utils import new_log_file
from app.core.file_scan import scan_folder
from app.core.paths import detect_ssh, is_executable_file
from app.core.profiles import Profile, fallback_hosts, load_profiles, profile_with_host, save_profiles, upsert_profile
from app.core.progress import parse_rsync_progress
from app.core.remote_dirs import RemoteEntry, build_list_remote_entries_command
from app.core.rsync_command import build_rsync_command, validate_transfer_inputs
from app.core.rsync_runner import RsyncRunner
from app.core.ssh_test import run_ssh_test
from app.core.sync_compare import (
    build_remote_manifest_command,
    compare_manifests,
    parse_remote_manifest,
    scan_local_manifest,
    write_files_from,
)
from app.gui.profile_dialog import ProfileDialog
from app.gui.remote_browser_dialog import RemoteListWorker


class CommandWorker(QObject):
    output = Signal(str)
    finished = Signal(int)

    def __init__(self, command: list[str], log_prefix: str, passphrase: str = "", ssh_path: str | None = None) -> None:
        super().__init__()
        self.command = command
        self.log_prefix = log_prefix
        self.passphrase = passphrase
        self.ssh_path = ssh_path
        self.runner = RsyncRunner()

    @Slot()
    def run(self) -> None:
        log_file = new_log_file(self.log_prefix)
        code = self.runner.run(self.command, log_file, self.output.emit, passphrase=self.passphrase, ssh_path=self.ssh_path)
        self.output.emit(f"\nLog saved to: {log_file}\n")
        self.finished.emit(code)

    @Slot()
    def cancel(self) -> None:
        self.runner.cancel()


class FallbackCommandWorker(CommandWorker):
    def __init__(
        self,
        profile: Profile,
        command_factory,
        log_prefix: str,
        passphrase: str = "",
        ssh_path: str | None = None,
    ) -> None:
        super().__init__([], log_prefix, passphrase=passphrase, ssh_path=ssh_path)
        self.profile = profile
        self.command_factory = command_factory

    @Slot()
    def run(self) -> None:
        log_file = new_log_file(self.log_prefix)
        for host in fallback_hosts(self.profile):
            attempt_profile = profile_with_host(self.profile, host)
            self.output.emit(f"Checking {host} before transfer...\n")
            result = run_ssh_test(attempt_profile, ssh_path=self.ssh_path, passphrase=self.passphrase, timeout=30)
            if result.returncode != 0:
                self.output.emit(f"{host} unavailable for transfer: exit code {result.returncode}\n")
                continue
            self.output.emit(f"Using {host} for transfer.\n")
            self.command = self.command_factory(attempt_profile)
            code = self.runner.run(self.command, log_file, self.output.emit, passphrase=self.passphrase, ssh_path=self.ssh_path)
            self.output.emit(f"\nLog saved to: {log_file}\n")
            self.finished.emit(code)
            return
        self.output.emit("No QRIScloud SSH host was available for transfer.\n")
        self.finished.emit(124)


class SshTestWorker(QObject):
    output = Signal(str)
    finished = Signal(int)

    def __init__(self, profile: Profile, ssh_path: str, passphrase: str = "") -> None:
        super().__init__()
        self.profile = profile
        self.ssh_path = ssh_path
        self.passphrase = passphrase

    @Slot()
    def run(self) -> None:
        log_file = new_log_file(f"ssh_test_{self.profile.name}")
        self.output.emit("Testing SSH connection... This can take up to 90 seconds per host on a slow QRIScloud login.\n")
        log_parts: list[str] = []
        result = None
        for host in fallback_hosts(self.profile):
            attempt_profile = profile_with_host(self.profile, host)
            self.output.emit(f"Trying {host}...\n")
            result = run_ssh_test(attempt_profile, ssh_path=self.ssh_path, passphrase=self.passphrase)
            log_parts.append(f"=== {host} ===\n{result.output}\n")
            self.output.emit(result.output or "(SSH produced no output)\n")
            if result.returncode == 0:
                self.output.emit(f"SSH test succeeded using {host}.\n")
                break
            self.output.emit(f"SSH test failed on {host} with code {result.returncode}.\n")
        if result is None:
            result = run_ssh_test(self.profile, ssh_path=self.ssh_path, passphrase=self.passphrase)
        log_file.write_text("".join(log_parts), encoding="utf-8", errors="replace")
        self.output.emit(f"\nSSH test exited with code {result.returncode}\nLog saved to: {log_file}\n")
        self.finished.emit(result.returncode)


class SyncCompareWorker(QObject):
    output = Signal(str)
    finished = Signal(object, str)

    def __init__(self, profile: Profile, ssh_path: str, local_folder: str, remote_path: str, passphrase: str) -> None:
        super().__init__()
        self.profile = profile
        self.ssh_path = ssh_path
        self.local_folder = local_folder
        self.remote_path = remote_path
        self.passphrase = passphrase
        self._process: subprocess.Popen[str] | None = None
        self._cancelled = False

    @Slot()
    def run(self) -> None:
        try:
            self.output.emit("Scanning local files...\n")
            local_manifest = scan_local_manifest(self.local_folder)
            self.output.emit(f"Local manifest: {len(local_manifest):,} files.\n")

            creationflags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
            remote_output: list[str] = []
            returncode = 1
            output_text = ""
            for host in fallback_hosts(self.profile):
                attempt_profile = profile_with_host(self.profile, host)
                self.output.emit(f"Reading remote file manifest from {host}... This can take a while for large QRIScloud folders.\n")
                command = build_remote_manifest_command(
                    attempt_profile,
                    self.remote_path,
                    ssh_path=self.ssh_path,
                    batch_mode=not bool(self.passphrase),
                )
                env = build_askpass_environment(self.passphrase, ssh_path=self.ssh_path)
                remote_output = []
                try:
                    with subprocess.Popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                        shell=False,
                        creationflags=creationflags,
                        env=env,
                    ) as process:
                        self._process = process
                        line_count = 0
                        if process.stdout:
                            for line in process.stdout:
                                if self._cancelled:
                                    process.terminate()
                                    self.finished.emit(None, "Sync comparison was cancelled.")
                                    return
                                remote_output.append(line)
                                line_count += 1
                                if line_count % 500 == 0:
                                    self.output.emit(f"Remote manifest on {host}: read {line_count:,} lines...\n")
                        returncode = process.wait(timeout=300)
                finally:
                    scrub_askpass_environment(env)
                output_text = "".join(remote_output)
                if returncode == 0:
                    self.output.emit(f"Remote manifest succeeded using {host}.\n")
                    break
                self.output.emit(f"Remote manifest failed on {host} with exit code {returncode}.\n")
            if returncode != 0:
                self.finished.emit(None, output_text or f"Remote manifest failed with exit code {returncode}.")
                return

            remote_manifest = parse_remote_manifest(output_text)
            self.output.emit(f"Remote manifest: {len(remote_manifest):,} files.\n")
            selection = compare_manifests(local_manifest, remote_manifest)
            selected = selection.selected
            file_list = write_files_from(selected, "sync_selection")
            self.output.emit(
                f"Sync selection: {len(selection.missing):,} missing, {len(selection.changed):,} changed, "
                f"{len(selected):,} total selected.\n"
            )
            self.output.emit(f"Selection file list: {file_list}\n")
            self.finished.emit(
                {
                    "missing": len(selection.missing),
                    "changed": len(selection.changed),
                    "selected": len(selected),
                    "file_list": str(file_list),
                    "local_folder": self.local_folder,
                    "remote_path": self.remote_path,
                },
                "",
            )
        except Exception as exc:
            self.finished.emit(None, f"Sync comparison failed: {exc}")
        finally:
            self._process = None

    @Slot()
    def cancel(self) -> None:
        self._cancelled = True
        if self._process and self._process.poll() is None:
            self._process.terminate()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("QRIS Rsync Manager")
        self.resize(980, 720)

        self.profiles = load_profiles()
        self.current_thread: QThread | None = None
        self.current_worker: CommandWorker | SshTestWorker | None = None
        self.remote_thread: QThread | None = None
        self.remote_worker: RemoteListWorker | None = None
        self.compare_thread: QThread | None = None
        self.compare_worker: SyncCompareWorker | None = None
        self.sync_selection: dict[str, object] | None = None
        self.session_passphrase: str | None = None

        self.profile_combo = QComboBox()
        self.local_folder_edit = QLineEdit()
        self.remote_path_edit = QLineEdit()
        self.detected_ssh = detect_ssh()
        self.status_label = QLabel()
        self.transfer_progress = QProgressBar()
        self.transfer_progress.setRange(0, 100)
        self.transfer_progress.setValue(0)
        self.transfer_progress.setTextVisible(True)
        self.transfer_status_label = QLabel("No transfer running.")
        self._progress_buffer = ""
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)

        self.local_model = QFileSystemModel(self)
        self.local_model.setRootPath("")
        self.local_tree = QTreeView()
        self.local_tree.setModel(self.local_model)
        self.local_tree.setSortingEnabled(True)
        self.local_tree.sortByColumn(0, Qt.AscendingOrder)
        self.local_tree.doubleClicked.connect(self._local_double_clicked)

        self.remote_table = QTableWidget(0, 4)
        self.remote_table.setHorizontalHeaderLabels(["Name", "Size", "Type", "Date Modified"])
        self.remote_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.remote_table.setColumnWidth(0, 260)
        self.remote_table.setColumnWidth(1, 90)
        self.remote_table.setColumnWidth(2, 110)
        self.remote_table.setColumnWidth(3, 150)
        self.remote_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.remote_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.remote_table.itemDoubleClicked.connect(self._remote_double_clicked)
        self.remote_status_label = QLabel("Remote browser not loaded.")

        self.ssh_button = QPushButton("Test SSH")
        self.local_up_button = QPushButton("Up")
        self.local_refresh_button = QPushButton("Refresh")
        self.remote_load_button = QPushButton("Load")
        self.remote_up_button = QPushButton("Up")
        self.remote_refresh_button = QPushButton("Refresh")
        self.dry_run_button = QPushButton("Compare / dry-run")
        self.download_dry_run_button = QPushButton("Compare download")
        self.build_selection_button = QPushButton("Build sync selection")
        self.upload_selection_button = QPushButton("Upload selection")
        self.upload_selection_button.setEnabled(False)
        self.upload_button = QPushButton("Upload")
        self.download_button = QPushButton("Download")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)

        self._build_ui()
        self._set_local_root(str(Path.home()))
        self._load_profile_combo()
        self._update_status()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        profile_box = QGroupBox("Profile")
        profile_layout = QGridLayout(profile_box)
        profile_layout.addWidget(QLabel("Profile"), 0, 0)
        profile_layout.addWidget(self.profile_combo, 0, 1)
        new_button = QPushButton("New")
        edit_button = QPushButton("Edit")
        delete_button = QPushButton("Delete")
        save_button = QPushButton("Save profiles")
        profile_layout.addWidget(new_button, 0, 2)
        profile_layout.addWidget(edit_button, 0, 3)
        profile_layout.addWidget(delete_button, 0, 4)
        profile_layout.addWidget(save_button, 0, 5)
        profile_layout.addWidget(self.status_label, 1, 0, 1, 6)
        layout.addWidget(profile_box)

        browser = QSplitter()
        browser.addWidget(self._local_panel())
        browser.addWidget(self._remote_panel())
        browser.setSizes([480, 480])
        layout.addWidget(browser, stretch=2)

        button_row = QHBoxLayout()
        button_row.addWidget(self.ssh_button)
        button_row.addWidget(self.dry_run_button)
        button_row.addWidget(self.download_dry_run_button)
        button_row.addWidget(self.build_selection_button)
        button_row.addWidget(self.upload_selection_button)
        button_row.addWidget(self.upload_button)
        button_row.addWidget(self.download_button)
        button_row.addStretch()
        button_row.addWidget(self.stop_button)
        layout.addLayout(button_row)

        progress_row = QHBoxLayout()
        progress_row.addWidget(QLabel("Progress"))
        progress_row.addWidget(self.transfer_progress, stretch=1)
        progress_row.addWidget(self.transfer_status_label)
        layout.addLayout(progress_row)

        layout.addWidget(QLabel("Log"))
        layout.addWidget(self.log_output, stretch=1)

        self.profile_combo.currentIndexChanged.connect(self._profile_changed)
        new_button.clicked.connect(self._new_profile)
        edit_button.clicked.connect(self._edit_profile)
        delete_button.clicked.connect(self._delete_profile)
        save_button.clicked.connect(self._save_profiles)
        self.ssh_button.clicked.connect(self._start_ssh_test)
        self.local_up_button.clicked.connect(self._local_go_up)
        self.local_refresh_button.clicked.connect(self._refresh_local_tree)
        self.remote_load_button.clicked.connect(self._refresh_remote_table)
        self.remote_up_button.clicked.connect(self._remote_go_up)
        self.remote_refresh_button.clicked.connect(self._refresh_remote_table)
        self.dry_run_button.clicked.connect(lambda: self._start_rsync(dry_run=True, direction="upload"))
        self.download_dry_run_button.clicked.connect(lambda: self._start_rsync(dry_run=True, direction="download"))
        self.build_selection_button.clicked.connect(self._build_sync_selection)
        self.upload_selection_button.clicked.connect(self._upload_sync_selection)
        self.upload_button.clicked.connect(lambda: self._start_rsync(dry_run=False, direction="upload"))
        self.download_button.clicked.connect(lambda: self._start_rsync(dry_run=False, direction="download"))
        self.stop_button.clicked.connect(self._cancel_current)

    def _local_panel(self) -> QGroupBox:
        box = QGroupBox("Local")
        layout = QVBoxLayout(box)
        layout.addLayout(self._folder_row())
        layout.addWidget(self.local_tree, stretch=1)
        return box

    def _remote_panel(self) -> QGroupBox:
        box = QGroupBox("Remote")
        layout = QVBoxLayout(box)
        layout.addLayout(self._remote_path_row())
        layout.addWidget(self.remote_table, stretch=1)
        layout.addWidget(self.remote_status_label)
        return box

    def _folder_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("Path"))
        row.addWidget(self.local_folder_edit)
        button = QPushButton("Browse")
        button.clicked.connect(self._browse_local_folder)
        row.addWidget(button)
        row.addWidget(self.local_up_button)
        row.addWidget(self.local_refresh_button)
        return row

    def _remote_path_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("Path"))
        row.addWidget(self.remote_path_edit)
        row.addWidget(self.remote_load_button)
        row.addWidget(self.remote_up_button)
        row.addWidget(self.remote_refresh_button)
        return row

    def _load_profile_combo(self) -> None:
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for profile in self.profiles:
            self.profile_combo.addItem(profile.name)
        self.profile_combo.blockSignals(False)
        self._profile_changed()

    def _profile_changed(self) -> None:
        self.session_passphrase = None
        self._clear_sync_selection()
        profile = self.current_profile()
        if profile:
            self.remote_path_edit.setText(profile.remote_path)
        self._update_status()

    def current_profile(self) -> Profile | None:
        index = self.profile_combo.currentIndex()
        if 0 <= index < len(self.profiles):
            return self.profiles[index].normalized()
        return None

    def _new_profile(self) -> None:
        dialog = ProfileDialog(parent=self)
        if dialog.exec() == ProfileDialog.Accepted:
            self.profiles = upsert_profile(self.profiles, dialog.profile())
            save_profiles(self.profiles)
            self._load_profile_combo()

    def _edit_profile(self) -> None:
        profile = self.current_profile()
        if not profile:
            return
        dialog = ProfileDialog(profile, self)
        if dialog.exec() == ProfileDialog.Accepted:
            self.profiles = upsert_profile(self.profiles, dialog.profile())
            save_profiles(self.profiles)
            self._load_profile_combo()

    def _delete_profile(self) -> None:
        index = self.profile_combo.currentIndex()
        if index < 0:
            return
        if len(self.profiles) == 1:
            QMessageBox.information(self, "Profile required", "At least one profile is required.")
            return
        removed = self.profiles[index]
        if QMessageBox.question(self, "Delete profile", f"Delete profile '{removed.name}'?") == QMessageBox.Yes:
            del self.profiles[index]
            save_profiles(self.profiles)
            self._load_profile_combo()

    def _save_profiles(self) -> None:
        save_profiles(self.profiles)
        self._append_log("Profiles saved.\n")

    def _browse_local_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select local folder", str(Path.home()))
        if path:
            self.local_folder_edit.setText(path)
            self._set_local_root(path)

    def _set_local_root(self, path: str) -> None:
        local = Path(path).expanduser()
        if local.is_file():
            local = local.parent
        if not local.exists():
            return
        self.local_folder_edit.setText(str(local))
        self._clear_sync_selection()
        self.local_tree.setRootIndex(self.local_model.setRootPath(str(local)))

    def _refresh_local_tree(self) -> None:
        self._set_local_root(self.local_folder_edit.text() or str(Path.home()))

    def _local_go_up(self) -> None:
        current = Path(self.local_folder_edit.text() or str(Path.home())).expanduser()
        parent = current.parent if current.parent != current else current
        self._set_local_root(str(parent))

    def _local_double_clicked(self, index) -> None:
        path = Path(self.local_model.filePath(index))
        if path.is_dir():
            self._set_local_root(str(path))

    def _start_ssh_test(self) -> None:
        profile = self.current_profile()
        if not profile:
            return
        errors = self._profile_errors(profile, require_rsync=False)
        if errors:
            self._show_errors(errors)
            return
        if not is_executable_file(self.detected_ssh):
            self._show_errors([f"ssh.exe was not found at {self.detected_ssh}. Install MSYS2 or add OpenSSH."])
            return
        passphrase = self._get_session_passphrase(profile)
        if passphrase is None:
            return
        self._start_worker(SshTestWorker(profile, self.detected_ssh, passphrase=passphrase), "SSH test")

    def _refresh_remote_table(self) -> None:
        profile = self.current_profile()
        if not profile:
            return
        if self.remote_thread:
            return
        errors = self._profile_errors(profile, require_rsync=False)
        if errors:
            self._show_errors(errors)
            return
        passphrase = self._get_session_passphrase(profile)
        if passphrase is None:
            return
        try:
            attempts = [
                (
                    host,
                    build_list_remote_entries_command(
                        profile_with_host(profile, host),
                        self.remote_path_edit.text(),
                        ssh_path=self.detected_ssh,
                        batch_mode=not bool(passphrase),
                    ),
                )
                for host in fallback_hosts(profile)
            ]
        except ValueError as exc:
            self._show_errors([str(exc)])
            return

        self.remote_status_label.setText("Loading remote directory...")
        self._set_remote_busy(True)
        worker = RemoteListWorker(attempts[0][1], passphrase, attempts=attempts)
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.finished.connect(self._remote_listing_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._remote_thread_finished)
        thread.started.connect(worker.run)
        self.remote_thread = thread
        self.remote_worker = worker
        thread.start()

    @Slot(object, str)
    def _remote_listing_finished(self, entries: object, error: str) -> None:
        self._set_remote_busy(False)
        if error:
            self.remote_status_label.setText("Remote listing failed.")
            QMessageBox.warning(self, "Remote listing failed", error)
            return
        remote_entries = list(entries)
        self._populate_remote_table(remote_entries)
        self.remote_status_label.setText(f"{len(remote_entries)} items in {self.remote_path_edit.text().strip()}")

    def _remote_thread_finished(self) -> None:
        self.remote_thread = None
        self.remote_worker = None

    def _populate_remote_table(self, entries: list[RemoteEntry]) -> None:
        self.remote_table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            name_item = QTableWidgetItem(entry.name)
            size_item = QTableWidgetItem(entry.size_label)
            type_item = QTableWidgetItem(entry.type_label)
            modified_item = QTableWidgetItem(entry.modified)
            for item in (name_item, size_item, type_item, modified_item):
                item.setData(Qt.UserRole, entry.path)
                item.setData(Qt.UserRole + 1, entry.is_dir)
            self.remote_table.setItem(row, 0, name_item)
            self.remote_table.setItem(row, 1, size_item)
            self.remote_table.setItem(row, 2, type_item)
            self.remote_table.setItem(row, 3, modified_item)

    def _remote_double_clicked(self, item: QTableWidgetItem) -> None:
        if item.data(Qt.UserRole + 1):
            self.remote_path_edit.setText(str(item.data(Qt.UserRole)))
            self._clear_sync_selection()
            self._refresh_remote_table()

    def _remote_go_up(self) -> None:
        current = self.remote_path_edit.text().strip().rstrip("/") or "/"
        if current == "/":
            return
        parent = current.rsplit("/", 1)[0] or "/"
        self.remote_path_edit.setText(parent)
        self._clear_sync_selection()
        self._refresh_remote_table()

    def _build_sync_selection(self) -> None:
        profile = self.current_profile()
        if not profile:
            return
        if self.compare_thread:
            return
        errors = self._profile_errors(profile, require_rsync=True)
        errors.extend(validate_transfer_inputs(profile, self.local_folder_edit.text(), self.remote_path_edit.text()))
        if errors:
            self._show_errors(errors)
            return
        passphrase = self._get_session_passphrase(profile)
        if passphrase is None:
            return
        self._clear_sync_selection()
        self.log_output.clear()
        self._set_compare_running(True)
        self.transfer_progress.setRange(0, 0)
        self.transfer_status_label.setText("Building sync selection...")
        self._append_log("Building sync selection by name, size, and modified time...\n")
        worker = SyncCompareWorker(
            profile,
            self.detected_ssh,
            self.local_folder_edit.text(),
            self.remote_path_edit.text(),
            passphrase,
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.output.connect(self._append_log)
        worker.finished.connect(self._sync_compare_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._compare_thread_finished)
        thread.started.connect(worker.run)
        self.compare_thread = thread
        self.compare_worker = worker
        thread.start()

    @Slot(object, str)
    def _sync_compare_finished(self, result: object, error: str) -> None:
        self._set_compare_running(False)
        self.transfer_progress.setRange(0, 100)
        if error:
            self.transfer_progress.setValue(0)
            self.transfer_status_label.setText("Sync comparison failed.")
            self._append_log(f"\n{error}\n")
            QMessageBox.warning(self, "Sync comparison failed", error)
            return
        self.sync_selection = dict(result)
        selected = int(self.sync_selection["selected"])
        self.upload_selection_button.setEnabled(selected > 0)
        if selected == 0:
            self.transfer_progress.setValue(100)
            self.transfer_status_label.setText("Sync comparison complete: no files selected.")
            self._append_log("\nNo missing or changed files found.\n")
        else:
            self.transfer_progress.setValue(100)
            self.transfer_status_label.setText(f"Sync comparison complete: {selected:,} files selected.")
            self._append_log("\nUse Upload selection to transfer only these files.\n")

    def _compare_thread_finished(self) -> None:
        self.compare_thread = None
        self.compare_worker = None

    def _upload_sync_selection(self) -> None:
        profile = self.current_profile()
        if not profile or not self.sync_selection:
            QMessageBox.information(self, "No selection", "Build a sync selection first.")
            return
        if self.sync_selection.get("local_folder") != self.local_folder_edit.text() or self.sync_selection.get("remote_path") != self.remote_path_edit.text():
            self._clear_sync_selection()
            QMessageBox.information(self, "Selection out of date", "The local or remote path changed. Build the sync selection again.")
            return
        selected = int(self.sync_selection.get("selected", 0))
        if selected == 0:
            QMessageBox.information(self, "No files selected", "No missing or changed files were found.")
            return
        passphrase = self._get_session_passphrase(profile)
        if passphrase is None:
            return
        if (
            QMessageBox.question(
                self,
                "Upload selection",
                f"Upload {selected:,} missing or changed files to {self.remote_path_edit.text()}?",
            )
            != QMessageBox.Yes
        ):
            return
        def command_factory(attempt_profile: Profile) -> list[str]:
            return build_rsync_command(
                attempt_profile,
                self.local_folder_edit.text(),
                remote_path=self.remote_path_edit.text(),
                dry_run=False,
                ssh_path=self.detected_ssh,
                batch_mode=not bool(passphrase),
                files_from=str(self.sync_selection["file_list"]),
            )

        self._start_worker(
            FallbackCommandWorker(
                profile,
                command_factory,
                f"sync_selection_upload_{profile.name}",
                passphrase=passphrase,
                ssh_path=self.detected_ssh,
            ),
            "Upload selection",
        )

    def _start_rsync(self, dry_run: bool, direction: str = "upload") -> None:
        profile = self.current_profile()
        if not profile:
            return
        errors = self._profile_errors(profile, require_rsync=True)
        errors.extend(validate_transfer_inputs(profile, self.local_folder_edit.text(), self.remote_path_edit.text()))
        if errors:
            self._show_errors(errors)
            return
        if not dry_run and direction == "upload" and not self._confirm_upload_scan():
            return
        if not dry_run and direction == "download":
            if (
                QMessageBox.question(
                    self,
                    "Download",
                    f"Download from {self.remote_path_edit.text()} into {self.local_folder_edit.text()}?",
                )
                != QMessageBox.Yes
            ):
                return
        passphrase = self._get_session_passphrase(profile)
        if passphrase is None:
            return
        def command_factory(attempt_profile: Profile) -> list[str]:
            return build_rsync_command(
                attempt_profile,
                self.local_folder_edit.text(),
                remote_path=self.remote_path_edit.text(),
                dry_run=dry_run,
                ssh_path=self.detected_ssh,
                batch_mode=not bool(passphrase),
                direction=direction,
            )

        if dry_run:
            action = f"{direction}_dry_run"
            label = "Compare download" if direction == "download" else "Dry run"
        else:
            action = direction
            label = "Download" if direction == "download" else "Upload"
        self._start_worker(
            FallbackCommandWorker(
                profile,
                command_factory,
                f"{action}_{profile.name}",
                passphrase=passphrase,
                ssh_path=self.detected_ssh,
            ),
            label,
        )

    def _ask_passphrase(self, profile: Profile) -> str | None:
        if not profile.ssh_key_path:
            return ""
        passphrase, accepted = QInputDialog.getText(
            self,
            "SSH key passphrase",
            "Enter SSH key passphrase. Leave blank if the key is already loaded in ssh-agent.",
            QLineEdit.Password,
        )
        if not accepted:
            return None
        return passphrase

    def _get_session_passphrase(self, profile: Profile) -> str | None:
        if not profile.ssh_key_path:
            return ""
        if self.session_passphrase is not None:
            return self.session_passphrase
        passphrase = self._ask_passphrase(profile)
        if passphrase is not None:
            self.session_passphrase = passphrase
        return passphrase

    def _confirm_upload_scan(self) -> bool:
        self._append_log("Scanning local folder before upload...\n")
        scan = scan_folder(self.local_folder_edit.text())
        self._append_log(
            f"Local scan: {scan.file_count:,} files, {scan.total_bytes:,} bytes, {scan.tiny_file_count:,} files under 1 MB.\n"
        )
        warnings = scan.warnings()
        if not warnings:
            return True
        message = "\n\n".join(warnings) + "\n\nContinue upload?"
        return QMessageBox.warning(self, "Large upload warning", message, QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes

    def _start_worker(self, worker: CommandWorker | SshTestWorker, label: str) -> None:
        if self.current_thread:
            QMessageBox.information(self, "Transfer running", "A command is already running.")
            return
        self.log_output.clear()
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.output.connect(self._append_log)
        worker.finished.connect(lambda code: self._worker_finished(code, label))
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.started.connect(worker.run)
        self.current_thread = thread
        self.current_worker = worker
        self._set_running(True)
        self._reset_transfer_progress(label)
        self._append_log(f"{label} started.\n")
        thread.start()

    def _worker_finished(self, code: int, label: str) -> None:
        self._append_log(f"\n{label} finished with exit code {code}.\n")
        if code == 0 and any(word in label.lower() for word in ("upload", "dry run")):
            self.transfer_progress.setValue(100)
        self.transfer_status_label.setText(f"{label} finished with exit code {code}.")
        self.current_thread = None
        self.current_worker = None
        self._set_running(False)

    def _cancel_current(self) -> None:
        if isinstance(self.current_worker, CommandWorker):
            self._append_log("\nCancelling transfer...\n")
            self.current_worker.cancel()
        elif isinstance(self.compare_worker, SyncCompareWorker):
            self._append_log("\nCancelling sync comparison...\n")
            self.compare_worker.cancel()
        else:
            self._append_log("\nNothing cancellable is currently running.\n")

    def _set_running(self, running: bool) -> None:
        self.ssh_button.setEnabled(not running)
        self.remote_load_button.setEnabled(not running and self.remote_thread is None)
        self.remote_refresh_button.setEnabled(not running and self.remote_thread is None)
        self.remote_up_button.setEnabled(not running and self.remote_thread is None)
        self.remote_table.setEnabled(not running and self.remote_thread is None)
        self.dry_run_button.setEnabled(not running)
        self.download_dry_run_button.setEnabled(not running)
        self.build_selection_button.setEnabled(not running and self.compare_thread is None)
        self.upload_selection_button.setEnabled(not running and self.compare_thread is None and bool(self.sync_selection and self.sync_selection.get("selected")))
        self.upload_button.setEnabled(not running)
        self.download_button.setEnabled(not running)
        self.stop_button.setEnabled(running)

    def _set_remote_busy(self, busy: bool) -> None:
        self.remote_load_button.setEnabled(not busy and self.current_thread is None)
        self.remote_refresh_button.setEnabled(not busy and self.current_thread is None)
        self.remote_up_button.setEnabled(not busy)
        self.remote_table.setEnabled(not busy)

    def _set_compare_running(self, running: bool) -> None:
        self.ssh_button.setEnabled(not running and self.current_thread is None)
        self.dry_run_button.setEnabled(not running and self.current_thread is None)
        self.download_dry_run_button.setEnabled(not running and self.current_thread is None)
        self.build_selection_button.setEnabled(not running and self.current_thread is None)
        self.upload_selection_button.setEnabled(not running and bool(self.sync_selection and self.sync_selection.get("selected")))
        self.upload_button.setEnabled(not running and self.current_thread is None)
        self.download_button.setEnabled(not running and self.current_thread is None)
        self.stop_button.setEnabled(running or self.current_thread is not None)

    def _clear_sync_selection(self) -> None:
        self.sync_selection = None
        if hasattr(self, "upload_selection_button"):
            self.upload_selection_button.setEnabled(False)

    def _profile_errors(self, profile: Profile, require_rsync: bool) -> list[str]:
        errors: list[str] = []
        if not profile.username:
            errors.append("Username is required in the selected profile.")
        if require_rsync and not is_executable_file(profile.rsync_path):
            errors.append(f"rsync.exe was not found at {profile.rsync_path}. MSYS2 default is C:\\msys64\\usr\\bin\\rsync.exe.")
        return errors

    def _show_errors(self, errors: list[str]) -> None:
        QMessageBox.warning(self, "Cannot start", "\n".join(errors))

    def _append_log(self, text: str) -> None:
        self.log_output.moveCursor(QTextCursor.End)
        self.log_output.insertPlainText(text)
        self.log_output.moveCursor(QTextCursor.End)
        self._update_transfer_progress(text)

    def _reset_transfer_progress(self, label: str) -> None:
        self._progress_buffer = ""
        self.transfer_progress.setRange(0, 100)
        self.transfer_progress.setValue(0)
        self.transfer_status_label.setText(f"{label} running...")

    def _update_transfer_progress(self, text: str) -> None:
        self._progress_buffer = (self._progress_buffer + text)[-1000:]
        progress = parse_rsync_progress(self._progress_buffer)
        if not progress:
            return
        self.transfer_progress.setValue(progress.percent)
        self.transfer_status_label.setText(
            f"{progress.percent}% | {progress.transferred} | {progress.speed} | ETA {progress.eta}"
        )

    def _update_status(self) -> None:
        profile = self.current_profile()
        if not profile:
            self.status_label.setText("No profile loaded.")
            return
        rsync_state = "found" if is_executable_file(profile.rsync_path) else "not found"
        ssh_state = "found" if is_executable_file(self.detected_ssh) else "not found"
        self.status_label.setText(
            f"rsync: {rsync_state} ({profile.rsync_path}) | ssh: {ssh_state} ({self.detected_ssh})"
        )


def show() -> int:
    app = QApplication([])
    window = MainWindow()
    window.show()
    return app.exec()
