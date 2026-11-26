from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QMimeData, Qt, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileIconProvider,
    QFileSystemModel,
    QHeaderView,
    QTreeView,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
)


LOCAL_MOVE_MIME = "application/x-qris-local-move"
REMOTE_MOVE_MIME = "application/x-qris-remote-move"

# Item data roles (column 0) shared by the remote folder trees.
PATH_ROLE = Qt.UserRole
IS_DIR_ROLE = Qt.UserRole + 1
PARENT_ROW_ROLE = Qt.UserRole + 2
ENTRY_ROLE = Qt.UserRole + 3
LOADED_ROLE = Qt.UserRole + 4


def populate_remote_table(tree: "RemoteMoveTree", entries: list, current_path: str) -> None:
    """Fill a remote folder tree, with a WinSCP-style ".." row first.

    The ".." row is a navigation and drop target only. It cannot be selected
    and is never part of the entries list, so no file action can apply to it.
    Folders get an expand arrow; their contents load when first expanded.
    """
    current = current_path.rstrip("/") or "/"
    # Rebuilding the items would otherwise lose the selection or, worse, keep
    # it on the wrong rows. When the same folder is redrawn, re-select by path
    # and re-open the folders that were open.
    same_folder = tree._populated_path == current
    keep: set[str] = set()
    if same_folder:
        keep = {str(item.data(0, PATH_ROLE) or "") for item in tree.selectedItems() if item.data(0, ENTRY_ROLE)}
    else:
        tree.expanded_paths.clear()
    reopen = set(tree.expanded_paths)
    tree.generation += 1
    tree.blockSignals(True)
    try:
        tree.clear()
    finally:
        tree.blockSignals(False)
    tree.setCurrentItem(None)
    if current != "/":
        parent = current.rsplit("/", 1)[0] or "/"
        item = QTreeWidgetItem(["..", "", "Parent folder", ""])
        item.setIcon(0, tree.folder_icon)
        item.setData(0, PATH_ROLE, parent)
        item.setData(0, IS_DIR_ROLE, True)
        item.setData(0, PARENT_ROW_ROLE, True)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsDropEnabled)
        item.setToolTip(0, f"Up to {parent}. Drop items here to move them up one level.")
        tree.addTopLevelItem(item)
    tree.addTopLevelItems([tree.make_item(entry) for entry in entries])
    tree._populated_path = current
    tree.current_remote_path = current
    if keep:
        tree.select_paths(keep)
    tree.expanded_paths = reopen
    tree.reopen_children(None)


def selected_remote_entries(tree: "RemoteMoveTree", entries: list) -> list:
    """Return selected entries by their stored path, never by row position.

    Entries in the listed folder come from *entries*; entries inside expanded
    subfolders come from the item itself.
    """
    by_path = {entry.path: entry for entry in entries}
    result = []
    iterator = QTreeWidgetItemIterator(tree, QTreeWidgetItemIterator.Selected)
    while iterator.value() is not None:
        item = iterator.value()
        iterator += 1
        if item.data(0, PARENT_ROW_ROLE):
            continue
        stored = item.data(0, ENTRY_ROLE)
        if stored is None:
            continue
        entry = by_path.get(str(item.data(0, PATH_ROLE) or ""), stored)
        result.append(entry)
    return result


class LocalMoveTreeView(QTreeView):
    moveDropped = Signal(object, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setDropIndicatorShown(True)

    def startDrag(self, _supported_actions) -> None:
        model = self.model()
        if not isinstance(model, QFileSystemModel):
            return
        paths = [model.filePath(index) for index in self.selectionModel().selectedRows(0)]
        if not paths:
            return
        mime = QMimeData()
        mime.setData(LOCAL_MOVE_MIME, json.dumps(paths).encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.MoveAction, Qt.MoveAction)

    def dragEnterEvent(self, event) -> None:
        if event.source() is self and event.mimeData().hasFormat(LOCAL_MOVE_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if event.source() is self and self._target_folder(event.position().toPoint()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        target = self._target_folder(event.position().toPoint())
        if event.source() is not self or not target:
            event.ignore()
            return
        try:
            paths = json.loads(bytes(event.mimeData().data(LOCAL_MOVE_MIME)).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            event.ignore()
            return
        self.moveDropped.emit(paths, target)
        event.acceptProposedAction()

    def _target_folder(self, position) -> str:
        model = self.model()
        if not isinstance(model, QFileSystemModel):
            return ""
        index = self.indexAt(position)
        if not index.isValid():
            return model.rootPath()
        path = Path(model.filePath(index))
        return str(path if path.is_dir() else path.parent)


class RemoteMoveTree(QTreeWidget):
    """QRIScloud folder browser that looks like the local pane.

    Folders show an expand arrow. Expanding one for the first time emits
    ``folderExpandRequested(path)``; the owner lists that folder and calls
    :meth:`set_children`. Double-clicking a folder opens it instead.
    """

    moveDropped = Signal(object, str)
    folderExpandRequested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.current_remote_path = ""
        self._populated_path: str | None = None
        self.expanded_paths: set[str] = set()
        # Bumped on every repopulate, so late folder listings can be ignored.
        self.generation = 0
        icons = QFileIconProvider()
        self.folder_icon = icons.icon(QFileIconProvider.Folder)
        self.file_icon = icons.icon(QFileIconProvider.File)
        self.setColumnCount(4)
        self.setHeaderLabels(["Name", "Size", "Type", "Date Modified"])
        header = self.header()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        for column, width in ((0, 280), (1, 90), (2, 110), (3, 150)):
            self.setColumnWidth(column, width)
        self.setUniformRowHeights(True)
        self.setRootIsDecorated(True)
        self.setAllColumnsShowFocus(True)
        self.setExpandsOnDoubleClick(False)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setDropIndicatorShown(True)
        self.itemExpanded.connect(self._expanded)
        self.itemCollapsed.connect(self._collapsed)

    # --- items ------------------------------------------------------------------

    def make_item(self, entry) -> QTreeWidgetItem:
        item = QTreeWidgetItem([entry.name, entry.size_label, entry.type_label, entry.modified])
        item.setIcon(0, self.folder_icon if entry.is_dir else self.file_icon)
        item.setData(0, PATH_ROLE, entry.path)
        item.setData(0, IS_DIR_ROLE, entry.is_dir)
        item.setData(0, ENTRY_ROLE, entry)
        item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
        if entry.is_dir:
            item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
        return item

    def _note_item(self, text: str) -> QTreeWidgetItem:
        item = QTreeWidgetItem([text, "", "", ""])
        item.setFlags(Qt.ItemIsEnabled)
        font = item.font(0)
        font.setItalic(True)
        item.setFont(0, font)
        return item

    def find_folder(self, path: str) -> QTreeWidgetItem | None:
        iterator = QTreeWidgetItemIterator(self)
        while iterator.value() is not None:
            item = iterator.value()
            iterator += 1
            if item.data(0, IS_DIR_ROLE) and not item.data(0, PARENT_ROW_ROLE) and item.data(0, PATH_ROLE) == path:
                return item
        return None

    def select_paths(self, paths: set[str]) -> None:
        iterator = QTreeWidgetItemIterator(self)
        selection = self.selectionModel()
        while iterator.value() is not None:
            item = iterator.value()
            iterator += 1
            if item.data(0, ENTRY_ROLE) and str(item.data(0, PATH_ROLE) or "") in paths:
                selection.select(self.indexFromItem(item), QItemSelectionModel.Select | QItemSelectionModel.Rows)

    def set_children(self, path: str, entries: list | None, error: str = "") -> None:
        """Show a folder's contents after it was listed (or why it could not be)."""
        item = self.find_folder(path)
        if item is None:
            return
        keep = {str(child.data(0, PATH_ROLE) or "") for child in self.selectedItems() if child.parent() is item}
        item.takeChildren()
        if entries is None:
            item.setData(0, LOADED_ROLE, False)
            item.addChild(self._note_item(f"Could not load this folder: {error.strip().splitlines()[0] if error.strip() else 'unknown error'}"))
            return
        item.setData(0, LOADED_ROLE, True)
        if entries:
            item.addChildren([self.make_item(entry) for entry in entries])
        else:
            item.addChild(self._note_item("(empty folder)"))
        if keep:
            self.select_paths(keep)
        self.reopen_children(item)

    def reopen_children(self, parent: QTreeWidgetItem | None) -> None:
        """Re-open folders the user had open, after a refresh."""
        count = self.topLevelItemCount() if parent is None else parent.childCount()
        for index in range(count):
            child = self.topLevelItem(index) if parent is None else parent.child(index)
            if child.data(0, IS_DIR_ROLE) and child.data(0, PATH_ROLE) in self.expanded_paths:
                child.setExpanded(True)

    def _expanded(self, item: QTreeWidgetItem) -> None:
        path = str(item.data(0, PATH_ROLE) or "")
        if not path or item.data(0, PARENT_ROW_ROLE):
            return
        self.expanded_paths.add(path)
        if item.data(0, LOADED_ROLE):
            return
        item.takeChildren()
        item.addChild(self._note_item("Loading…"))
        self.folderExpandRequested.emit(path)

    def _collapsed(self, item: QTreeWidgetItem) -> None:
        self.expanded_paths.discard(str(item.data(0, PATH_ROLE) or ""))

    # --- drag and drop ------------------------------------------------------------

    def startDrag(self, _supported_actions) -> None:
        paths = [
            str(item.data(0, PATH_ROLE))
            for item in self.selectedItems()
            if item.data(0, ENTRY_ROLE) and item.data(0, PATH_ROLE)
        ]
        if not paths:
            return
        mime = QMimeData()
        mime.setData(REMOTE_MOVE_MIME, json.dumps(paths).encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.MoveAction, Qt.MoveAction)

    def dragEnterEvent(self, event) -> None:
        if event.source() is self and event.mimeData().hasFormat(REMOTE_MOVE_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if event.source() is self and self._target_folder(event.position().toPoint()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        target = self._target_folder(event.position().toPoint())
        if event.source() is not self or not target:
            event.ignore()
            return
        try:
            paths = json.loads(bytes(event.mimeData().data(REMOTE_MOVE_MIME)).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            event.ignore()
            return
        self.moveDropped.emit(paths, target)
        event.acceptProposedAction()

    def _target_folder(self, position) -> str:
        item = self.itemAt(position)
        if item is None:
            return self.current_remote_path
        if not bool(item.data(0, IS_DIR_ROLE)):
            return ""
        return str(item.data(0, PATH_ROLE) or "")
