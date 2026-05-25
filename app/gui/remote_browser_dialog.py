from __future__ import annotations

import subprocess
import sys

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.core.askpass import build_askpass_environment, scrub_askpass_environment
from app.core.profiles import Profile, fallback_hosts, profile_with_host
from app.core.remote_dirs import RemoteEntry, build_list_remote_entries_command, parse_remote_entries


class RemoteListWorker(QObject):
    finished = Signal(object, str)

    def __init__(self, command: list[str], passphrase: str, attempts: list[tuple[str, list[str]]] | None = None) -> None:
        super().__init__()
        self.command = command
        self.passphrase = passphrase
        self.attempts = attempts or [("remote", command)]

    @Slot()
    def run(self) -> None:
        creationflags = 0
        if sys.platform.startswith("win"):
            creationflags = subprocess.CREATE_NO_WINDOW
        errors: list[str] = []
        for host, command in self.attempts:
            env = build_askpass_environment(self.passphrase, ssh_path=command[0])
            try:
                completed = subprocess.run(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    shell=False,
                    timeout=60,
                    creationflags=creationflags,
                    env=env,
                )
                if completed.returncode == 0:
                    self.finished.emit(parse_remote_entries(completed.stdout), "")
                    return
                errors.append(f"{host}: {completed.stdout or f'exit code {completed.returncode}'}")
            except subprocess.TimeoutExpired:
                errors.append(f"{host}: remote listing timed out after 60 seconds.")
            except OSError as exc:
                errors.append(f"{host}: remote listing failed to start: {exc}")
            finally:
                scrub_askpass_environment(env)
        self.finished.emit([], "\n".join(errors) or "Remote listing failed.")


class RemoteBrowserDialog(QDialog):
    def __init__(self, profile: Profile, ssh_path: str, start_path: str, passphrase: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Remote Browser")
        self.resize(860, 560)
        self.profile = profile
        self.ssh_path = ssh_path
        self.passphrase = passphrase
        self.selected_path = start_path.strip() or profile.remote_path
        self._thread: QThread | None = None
        self._entries: list[RemoteEntry] = []

        self.path_edit = QLineEdit(self.selected_path)
        self.status_label = QLabel()
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Type", "Name", "Path"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.itemDoubleClicked.connect(self._open_row)

        self.up_button = QPushButton("Up")
        self.refresh_button = QPushButton("Refresh")
        self.open_button = QPushButton("Open")
        self.use_button = QPushButton("Use selected/current path")
        self.cancel_button = QPushButton("Cancel")

        self._build_ui()
        self._connect()
        self.refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Remote path"))
        path_row.addWidget(self.path_edit, stretch=1)
        path_row.addWidget(self.up_button)
        path_row.addWidget(self.refresh_button)
        layout.addLayout(path_row)

        layout.addWidget(self.table, stretch=1)
        layout.addWidget(self.status_label)

        button_row = QHBoxLayout()
        button_row.addWidget(self.open_button)
        button_row.addStretch()
        button_row.addWidget(self.use_button)
        button_row.addWidget(self.cancel_button)
        layout.addLayout(button_row)

    def _connect(self) -> None:
        self.up_button.clicked.connect(self.go_up)
        self.refresh_button.clicked.connect(self.refresh)
        self.open_button.clicked.connect(self.open_selected)
        self.use_button.clicked.connect(self.use_path)
        self.cancel_button.clicked.connect(self.reject)
        self.path_edit.returnPressed.connect(self.refresh)

    def refresh(self) -> None:
        if self._thread:
            return
        path = self.path_edit.text().strip()
        try:
            attempts = [
                (
                    host,
                    build_list_remote_entries_command(
                        profile_with_host(self.profile, host),
                        path,
                        ssh_path=self.ssh_path,
                        batch_mode=not bool(self.passphrase),
                    ),
                )
                for host in fallback_hosts(self.profile)
            ]
        except ValueError as exc:
            QMessageBox.warning(self, "Remote path", str(exc))
            return
        self.selected_path = path.rstrip("/") or "/"
        self.status_label.setText("Loading...")
        self._set_busy(True)
        worker = RemoteListWorker(attempts[0][1], self.passphrase, attempts=attempts)
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.finished.connect(self._listing_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda: setattr(self, "_thread", None))
        thread.started.connect(worker.run)
        self._thread = thread
        thread.start()

    @Slot(object, str)
    def _listing_finished(self, entries: object, error: str) -> None:
        self._set_busy(False)
        if error:
            self.status_label.setText("Listing failed.")
            QMessageBox.warning(self, "Remote listing failed", error)
            return
        self._entries = list(entries)
        self._populate_table(self._entries)
        self.status_label.setText(f"{len(self._entries)} items in {self.selected_path}")

    def _populate_table(self, entries: list[RemoteEntry]) -> None:
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            type_item = QTableWidgetItem("Folder" if entry.is_dir else "File")
            name_item = QTableWidgetItem(entry.name)
            path_item = QTableWidgetItem(entry.path)
            for item in (type_item, name_item, path_item):
                item.setData(Qt.UserRole, entry.path)
                item.setData(Qt.UserRole + 1, entry.is_dir)
            self.table.setItem(row, 0, type_item)
            self.table.setItem(row, 1, name_item)
            self.table.setItem(row, 2, path_item)

    def _open_row(self, item: QTableWidgetItem) -> None:
        if item.data(Qt.UserRole + 1):
            self.path_edit.setText(str(item.data(Qt.UserRole)))
            self.refresh()

    def open_selected(self) -> None:
        item = self._selected_name_item()
        if item:
            self._open_row(item)

    def use_path(self) -> None:
        item = self._selected_name_item()
        if item and item.data(Qt.UserRole + 1):
            self.selected_path = str(item.data(Qt.UserRole))
        else:
            self.selected_path = self.path_edit.text().strip().rstrip("/") or "/"
        self.accept()

    def go_up(self) -> None:
        current = self.path_edit.text().strip().rstrip("/") or "/"
        if current == "/":
            return
        parent = current.rsplit("/", 1)[0] or "/"
        self.path_edit.setText(parent)
        self.refresh()

    def _selected_name_item(self) -> QTableWidgetItem | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.table.item(rows[0].row(), 1)

    def _set_busy(self, busy: bool) -> None:
        self.up_button.setEnabled(not busy)
        self.refresh_button.setEnabled(not busy)
        self.open_button.setEnabled(not busy)
        self.use_button.setEnabled(not busy)
        self.path_edit.setEnabled(not busy)
