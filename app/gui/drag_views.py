from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import QAbstractItemView, QFileSystemModel, QTableWidget, QTreeView


LOCAL_MOVE_MIME = "application/x-qris-local-move"
REMOTE_MOVE_MIME = "application/x-qris-remote-move"


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
