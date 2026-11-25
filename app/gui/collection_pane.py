from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QGroupBox,
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

from app.core.profiles import Profile, fallback_hosts, profile_with_host
from app.core.remote_dirs import RemoteDirectoryCache, RemoteEntry, build_list_remote_entries_command
from app.core.rsync_command import collection_key
from app.gui.drag_views import RemoteMoveTable, populate_remote_table, selected_remote_entries
from app.gui.remote_browser_dialog import RemoteListWorker


def fill_collection_combo(combo: QComboBox, collections: list[tuple[str, str]]) -> None:
    """Fill a picker with (label, remote path) pairs, e.g. profile names."""
    combo.blockSignals(True)
    combo.clear()
    seen: set[str] = set()
    for label, path in collections:
        if path and path not in seen:
            seen.add(path)
            combo.addItem(label, path)
            combo.setItemData(combo.count() - 1, path, Qt.ToolTipRole)
    combo.blockSignals(False)


def _key_or_none(path: str) -> tuple[str, str] | None:
    try:
        return collection_key(path)
    except ValueError:
        return None


def exclude_collection(combo: QComboBox, excluded_path: str) -> None:
    """Grey out every picker entry in the same collection as excluded_path.

    Entries are matched by collection ID, so /data and /QRISdata spellings and
    profiles saved at a subfolder are all caught. Pass "" to enable all.
    """
    excluded = _key_or_none(excluded_path) if excluded_path.strip() else None
    model = combo.model()
    for index in range(combo.count()):
        entry = _key_or_none(str(combo.itemData(index) or ""))
        blocked = bool(excluded and entry and entry[0] == excluded[0])
        item = model.item(index) if hasattr(model, "item") else None
        if item is not None:
            item.setEnabled(not blocked)


def sync_collection_combo(combo: QComboBox, current_path: str) -> None:
    """Select the picker entry whose folder contains current_path, or nothing."""
    current = _key_or_none(current_path)
    match = -1
    if current is not None:
        for index in range(combo.count()):
            entry = _key_or_none(str(combo.itemData(index) or ""))
            if entry is None or entry[0] != current[0]:
                continue
            if not entry[1] or current[1] == entry[1] or current[1].startswith(entry[1] + "/"):
                match = index
                break
    combo.blockSignals(True)
    combo.setCurrentIndex(match)
    combo.blockSignals(False)


class CollectionPane(QGroupBox):
    """A second QRIScloud browser used in collection-to-collection mode.

    It browses with the active profile's account, so both panes are always
    reachable from the same SSH login. It only lists folders; copying is
    started by the main window.
    """

    busyChanged = Signal(bool)
    selectionChanged = Signal()
    pathChanged = Signal()

    def __init__(
        self,
        profile_provider: Callable[[], Profile | None],
        passphrase_provider: Callable[[Profile], str | None],
        ssh_path: str,
        parent=None,
    ) -> None:
        super().__init__("QRIScloud (second collection)", parent)
        self._profile_provider = profile_provider
        self._passphrase_provider = passphrase_provider
        self.ssh_path = ssh_path
        self.list_thread: QThread | None = None
        self.worker: RemoteListWorker | None = None
        self.entries: list[RemoteEntry] = []
        self.entries_path = ""
        self._pending_path: str | None = None
        self._pending_force = False
        self._cache = RemoteDirectoryCache()
        self._locked = False

        self.collection_combo = QComboBox()
        self.collection_combo.setToolTip("Collections from your saved profiles. You can also type any path.")
        self.path_edit = QLineEdit()
        self.load_button = QPushButton("Load")
        self.up_button = QPushButton("Up")
        self.refresh_button = QPushButton("Refresh")
        # File actions; the main window connects these to the shared remote
        # handlers, which keep every action inside this pane's collection.
        self.new_folder_button = QPushButton("New folder")
        self.rename_button = QPushButton("Rename")
        self.move_button = QPushButton("Move to…")
        self.delete_button = QPushButton("Delete")
        self.status_label = QLabel("Choose a collection and click Load.")
        self.table = RemoteMoveTable(0, 4)
        self.table.setHorizontalHeaderLabels(["Name", "Size", "Type", "Date Modified"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.setColumnWidth(0, 260)
        self.table.setColumnWidth(1, 90)
        self.table.setColumnWidth(2, 110)
        self.table.setColumnWidth(3, 150)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setToolTip("Drag selected items onto a folder (or '..') to move them within this collection.")

        layout = QVBoxLayout(self)
        pick_row = QHBoxLayout()
        pick_row.addWidget(QLabel("Collection"))
        pick_row.addWidget(self.collection_combo, stretch=1)
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Path"))
        path_row.addWidget(self.path_edit, stretch=1)
        path_row.addWidget(self.load_button)
        action_row = QHBoxLayout()
        action_row.addWidget(self.up_button)
        action_row.addWidget(self.refresh_button)
        action_row.addWidget(self.new_folder_button)
        action_row.addWidget(self.rename_button)
        action_row.addWidget(self.move_button)
        action_row.addWidget(self.delete_button)
        action_row.addStretch()
        layout.addLayout(pick_row)
        layout.addLayout(path_row)
        layout.addLayout(action_row)
        layout.addWidget(self.table, stretch=1)
        layout.addWidget(self.status_label)

        self.collection_combo.activated.connect(self._collection_picked)
        self.load_button.clicked.connect(lambda: self.refresh(force=True))
        self.refresh_button.clicked.connect(lambda: self.refresh(force=True))
        self.up_button.clicked.connect(self.go_up)
        self.path_edit.returnPressed.connect(lambda: self.refresh(force=True))
        self.path_edit.editingFinished.connect(self._path_edited)
        self.table.itemDoubleClicked.connect(self._double_clicked)
        self.table.itemSelectionChanged.connect(self.selectionChanged.emit)

    # Public API -----------------------------------------------------------

    def set_collections(self, collections: list[tuple[str, str]], preferred: str = "") -> None:
        fill_collection_combo(self.collection_combo, collections)
        sync_collection_combo(self.collection_combo, self.current_path())
        # Keep a path the user has loaded; otherwise start in a listed collection.
        unlisted = self.collection_combo.currentIndex() < 0
        if not self.path_edit.text().strip() or (unlisted and not self.entries_path):
            first = self.collection_combo.itemData(0) if self.collection_combo.count() else ""
            self.path_edit.setText(preferred or first or "/data/")
            sync_collection_combo(self.collection_combo, self.current_path())
        self.pathChanged.emit()

    def current_path(self) -> str:
        return self.path_edit.text().strip().rstrip("/") or "/"

    def selected_entries(self) -> list[RemoteEntry]:
        if self.entries_path != self.current_path():
            return []
        return selected_remote_entries(self.table, self.entries)

    def is_busy(self) -> bool:
        return self.list_thread is not None

    def set_locked(self, locked: bool) -> None:
        """Disable browsing while another operation is running."""
        self._locked = locked
        self._update_controls()

    def clear(self) -> None:
        self._pending_path = None
        self._cache.clear()
        self.entries = []
        self.entries_path = ""
        self.table.setRowCount(0)
        self.status_label.setText("Choose a collection and click Load.")

    def invalidate(self, profile: Profile, path: str) -> None:
        self._cache.invalidate(profile, path, subdirectories=True)

    def cancel(self) -> None:
        self._pending_path = None
        if self.worker is not None:
            self.worker.cancel()

    def refresh(self, force: bool = False) -> None:
        profile = self._profile_provider()
        if profile is None:
            return
        requested = self.current_path()
        if self.list_thread is not None:
            self._pending_path = requested
            self._pending_force = self._pending_force or force
            self.status_label.setText(f"Waiting to open {requested}...")
            return
        cached = None if force else self._cache.get(profile, requested)
        if cached is not None:
            self._apply(cached, requested)
            self.status_label.setText(f"Showing cached {requested}. Use Refresh to reload.")
            return
        if not profile.username:
            QMessageBox.warning(self, "Cannot load", "Username is required in the selected profile.")
            return
        passphrase = self._passphrase_provider(profile)
        if passphrase is None:
            return
        try:
            attempts = [
                (
                    host,
                    build_list_remote_entries_command(
                        profile_with_host(profile, host),
                        requested,
                        ssh_path=self.ssh_path,
                        batch_mode=not bool(passphrase),
                    ),
                )
                for host in fallback_hosts(profile)
            ]
        except ValueError as exc:
            QMessageBox.warning(self, "Remote path", str(exc))
            return
        worker = RemoteListWorker(attempts[0][1], passphrase, attempts=attempts)
        worker.cache_profile = profile
        worker.cache_path = requested
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.finished.connect(self._listing_finished)
        thread.finished.connect(self._thread_finished)
        thread.finished.connect(thread.deleteLater)
        thread.started.connect(worker.run)
        self.list_thread = thread
        self.worker = worker
        self.status_label.setText(f"Loading {requested}...")
        self._update_controls()
        self.busyChanged.emit(True)
        thread.start()

    def go_up(self) -> None:
        current = self.current_path()
        if current == "/":
            return
        self.navigate(current.rsplit("/", 1)[0] or "/")

    def navigate(self, path: str) -> None:
        self.path_edit.setText(path.strip().rstrip("/") or "/")
        self._path_edited()
        self.refresh()

    def _path_edited(self) -> None:
        sync_collection_combo(self.collection_combo, self.current_path())
        self.pathChanged.emit()

    # Internals ------------------------------------------------------------

    def _collection_picked(self, index: int) -> None:
        path = self.collection_combo.itemData(index)
        if path:
            self.navigate(str(path))

    def _double_clicked(self, item: QTableWidgetItem) -> None:
        if item.data(Qt.UserRole + 1):
            path = str(item.data(Qt.UserRole))
            QTimer.singleShot(0, lambda path=path: self.navigate(path))

    @Slot(object, str)
    def _listing_finished(self, entries: object, error: str) -> None:
        worker = self.sender()
        if not isinstance(worker, RemoteListWorker):
            return
        try:
            if worker is not self.worker:
                return
            if self.current_path() != worker.cache_path:
                self.status_label.setText("Path changed while loading. Refresh to view it.")
                return
            if error:
                if "cancelled" in error.lower():
                    self.status_label.setText("Remote listing cancelled.")
                    return
                self.status_label.setText("Remote listing failed.")
                QMessageBox.warning(
                    self,
                    "Remote listing failed",
                    f"{error}\n\nBoth panes use the selected profile's account. "
                    "That account must have access to this collection.",
                )
                return
            listed = list(entries)
            self._cache.put(worker.cache_profile, worker.cache_path, listed)
            self._apply(listed, worker.cache_path)
            self.status_label.setText(f"{len(listed)} items in {worker.cache_path}")
        finally:
            if worker is self.worker:
                worker.deleteLater()
                if self.list_thread is not None:
                    self.list_thread.quit()

    @Slot()
    def _thread_finished(self) -> None:
        if self.sender() is not self.list_thread:
            return
        self.list_thread = None
        self.worker = None
        self._update_controls()
        self.busyChanged.emit(False)
        pending = self._pending_path
        pending_force = self._pending_force
        self._pending_path = None
        self._pending_force = False
        if pending:
            self.path_edit.setText(pending)
            self.refresh(force=pending_force)

    def _apply(self, entries: list[RemoteEntry], path: str) -> None:
        self.entries = list(entries)
        self.entries_path = path.rstrip("/") or "/"
        self.table.current_remote_path = self.entries_path
        populate_remote_table(self.table, self.entries, self.entries_path)
        self.selectionChanged.emit()

    def _update_controls(self) -> None:
        enabled = not self._locked and self.list_thread is None
        for widget in (
            self.collection_combo,
            self.path_edit,
            self.load_button,
            self.refresh_button,
            self.table,
            self.new_folder_button,
            self.rename_button,
            self.move_button,
            self.delete_button,
        ):
            widget.setEnabled(enabled)
        self.up_button.setEnabled(not self._locked)
