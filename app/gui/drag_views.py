from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QMimeData, Qt, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import QAbstractItemView, QFileSystemModel, QTableWidget, QTableWidgetItem, QTreeView


LOCAL_MOVE_MIME = "application/x-qris-local-move"
REMOTE_MOVE_MIME = "application/x-qris-remote-move"

# Item data roles shared by the remote tables.
PATH_ROLE = Qt.UserRole
IS_DIR_ROLE = Qt.UserRole + 1
PARENT_ROW_ROLE = Qt.UserRole + 2


def populate_remote_table(table: QTableWidget, entries: list, current_path: str) -> None:
    """Fill a 4-column remote table, with a WinSCP-style ".." row first.

    The ".." row is a navigation and drop target only. It cannot be selected
    and is never part of the entries list, so no file action can apply to it.
    """
    current = current_path.rstrip("/") or "/"
    # Qt keeps selected row *numbers* when items are replaced, which would
    # point at different files after a refresh or folder change. Clear the
    # selection, and when the same folder is redrawn re-select by path.
    keep: set[str] = set()
    if getattr(table, "_populated_path", None) == current:
        for index in table.selectionModel().selectedRows():
            item = table.item(index.row(), 0)
            if item is not None and not item.data(PARENT_ROW_ROLE):
                keep.add(str(item.data(PATH_ROLE) or ""))
    table.clearSelection()
    table.setCurrentCell(-1, -1)
    show_parent = current != "/"
    table.setRowCount(len(entries) + (1 if show_parent else 0))
    row = 0
    if show_parent:
        parent = current.rsplit("/", 1)[0] or "/"
        cells = ("..", "", "Parent folder", "")
        for column, text in enumerate(cells):
            item = QTableWidgetItem(text)
            item.setData(PATH_ROLE, parent)
            item.setData(IS_DIR_ROLE, True)
            item.setData(PARENT_ROW_ROLE, True)
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsDropEnabled)
            item.setToolTip(f"Up to {parent}. Drop items here to move them up one level.")
            table.setItem(row, column, item)
        row += 1
    for entry in entries:
        cells = (entry.name, entry.size_label, entry.type_label, entry.modified)
        for column, text in enumerate(cells):
            item = QTableWidgetItem(text)
            item.setData(PATH_ROLE, entry.path)
            item.setData(IS_DIR_ROLE, entry.is_dir)
            table.setItem(row, column, item)
        row += 1
    table._populated_path = current
    if keep:
        model = table.model()
        selection = table.selectionModel()
        for index in range(table.rowCount()):
            item = table.item(index, 0)
            if item is not None and str(item.data(PATH_ROLE) or "") in keep and not item.data(PARENT_ROW_ROLE):
                selection.select(model.index(index, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)


def selected_remote_entries(table: QTableWidget, entries: list) -> list:
    """Return selected entries by their stored path, never by row number."""
    by_path = {entry.path: entry for entry in entries}
    rows = {index.row() for index in table.selectionModel().selectedRows()}
    if not rows:
        rows = {item.row() for item in table.selectedItems()}
    result = []
    for row in sorted(rows):
        item = table.item(row, 0)
        if item is None or item.data(PARENT_ROW_ROLE):
            continue
        entry = by_path.get(str(item.data(PATH_ROLE) or ""))
        if entry is not None:
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


class RemoteMoveTable(QTableWidget):
    moveDropped = Signal(object, str)

    def __init__(self, rows: int, columns: int, parent=None) -> None:
        super().__init__(rows, columns, parent)
        self.current_remote_path = ""
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setDropIndicatorShown(True)

    def startDrag(self, _supported_actions) -> None:
        rows = sorted({index.row() for index in self.selectionModel().selectedRows()})
        paths = []
        for row in rows:
            item = self.item(row, 0)
            if item is not None and item.data(Qt.UserRole):
                paths.append(str(item.data(Qt.UserRole)))
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
        if not bool(item.data(Qt.UserRole + 1)):
            return ""
        return str(item.data(Qt.UserRole) or "")
