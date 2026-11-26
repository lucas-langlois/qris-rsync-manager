"""WinSCP-style Synchronise window: side-by-side differences with tick boxes."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTableView,
    QVBoxLayout,
)

from app.core.remote_dirs import format_bytes
from app.core.sync_compare import (
    DOWNLOAD,
    MODE_BOTH,
    MODE_DOWNLOAD,
    MODE_UPLOAD,
    UPLOAD,
    SyncItem,
    SyncPlan,
    SyncStatus,
)

COL_TICK, COL_NAME, COL_LOCAL_SIZE, COL_LOCAL_TIME, COL_ACTION, COL_REMOTE_SIZE, COL_REMOTE_TIME, COL_STATUS = range(8)
HEADERS = [
    "",
    "Name",
    "This computer\nSize",
    "This computer\nModified",
    "Action",
    "QRIScloud\nSize",
    "QRIScloud\nModified",
    "Why it is listed",
]
SORT_ROLE = Qt.UserRole + 1

# Pale row colours; text is forced dark so rows stay readable in dark mode.
UPLOAD_COLOUR = QColor("#e3f4e1")
DOWNLOAD_COLOUR = QColor("#e1ecfa")
CONFLICT_COLOUR = QColor("#fdf0d5")
ARCHIVE_COLOUR = QColor("#efe6f8")
UNTICKED_TEXT = QColor("#8a8f94")
DARK_TEXT = QColor("#1f2a33")

STATUS_COLOURS = {
    SyncStatus.LOCAL_ONLY: UPLOAD_COLOUR,
    SyncStatus.LOCAL_NEWER: UPLOAD_COLOUR,
    SyncStatus.REMOTE_ONLY: DOWNLOAD_COLOUR,
    SyncStatus.REMOTE_NEWER: DOWNLOAD_COLOUR,
    SyncStatus.CONFLICT: CONFLICT_COLOUR,
    SyncStatus.ARCHIVE_NEW: ARCHIVE_COLOUR,
    SyncStatus.ARCHIVE_CHANGED: ARCHIVE_COLOUR,
    SyncStatus.REMOTE_ARCHIVE: ARCHIVE_COLOUR,
}

STATUS_HELP = {
    SyncStatus.LOCAL_ONLY: "Not on QRIScloud yet.",
    SyncStatus.REMOTE_ONLY: "Not on this computer yet.",
    SyncStatus.LOCAL_NEWER: "This computer has a newer copy.",
    SyncStatus.REMOTE_NEWER: "QRIScloud has a newer copy.",
    SyncStatus.CONFLICT: "Same time but different size. Check which one you want.",
    SyncStatus.ARCHIVE_NEW: "Many files of one type: uploaded as one .tar archive.",
    SyncStatus.ARCHIVE_CHANGED: "Files changed since this folder was archived. Upload rebuilds the archive.",
    SyncStatus.REMOTE_ARCHIVE: "Archive on QRIScloud only. Downloading copies the .tar; it is not unpacked.",
}


def _when(timestamp: float | None) -> str:
    if timestamp is None:
        return ""
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def _archive_label(item: SyncItem) -> str:
    archive = item.archive
    assert archive is not None
    folder = f"{archive.relative_folder}/" if archive.relative_folder else "(this folder)/"
    names = archive.parts[0].archive_name if len(archive.parts) == 1 else f"{len(archive.parts)} .tar parts"
    return f"\U0001F4E6 {folder}  — {archive.member_count:,} {archive.category} files → {names}"


class SyncTableModel(QAbstractTableModel):
    changed = Signal()

    def __init__(self, plan: SyncPlan, parent=None) -> None:
        super().__init__(parent)
        self.plan = plan

    # --- Qt model API -------------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802 (Qt API)
        return 0 if parent.isValid() else len(self.plan.items)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return HEADERS[section]
        return None

    def flags(self, index):
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() == COL_TICK:
            return base | Qt.ItemIsUserCheckable
        return base

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        item = self.plan.items[index.row()]
        column = index.column()
        if role == Qt.CheckStateRole and column == COL_TICK:
            return Qt.Checked if item.checked else Qt.Unchecked
        if role == Qt.DisplayRole:
            return self._display(item, column)
        if role == SORT_ROLE:
            return self._sort_value(item, column)
        if role == Qt.BackgroundRole:
            return QBrush(STATUS_COLOURS.get(item.status, QColor("white")))
        if role == Qt.ForegroundRole:
            return QBrush(DARK_TEXT if item.checked else UNTICKED_TEXT)
        if role == Qt.FontRole and column == COL_ACTION:
            font = QFont()
            font.setBold(item.checked)
            return font
        if role == Qt.TextAlignmentRole and column in (COL_LOCAL_SIZE, COL_REMOTE_SIZE):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.TextAlignmentRole and column == COL_ACTION:
            return int(Qt.AlignCenter)
        if role == Qt.ToolTipRole:
            tip = STATUS_HELP.get(item.status, "")
            if column == COL_ACTION and len(item.allowed_directions) > 1:
                tip += "\nClick to change the direction."
            if item.checked and item.replaces_newer:
                tip += "\nWarning: this replaces the newer copy."
            return tip
        return None

    def setData(self, index, value, role=Qt.EditRole):  # noqa: N802
        if index.isValid() and index.column() == COL_TICK and role == Qt.CheckStateRole:
            item = self.plan.items[index.row()]
            item.checked = Qt.CheckState(value) == Qt.Checked
            self._row_changed(index.row())
            return True
        return False

    # --- helpers ----------------------------------------------------------------

    def _display(self, item: SyncItem, column: int) -> str:
        if column == COL_NAME:
            return _archive_label(item) if item.is_archive else item.path
        if column == COL_LOCAL_SIZE:
            if item.is_archive:
                return format_bytes(item.archive.member_bytes)
            return format_bytes(item.local.size) if item.local else ""
        if column == COL_LOCAL_TIME:
            return _when(item.local.modified_timestamp) if item.local else ""
        if column == COL_REMOTE_SIZE:
            return format_bytes(item.remote.size) if item.remote else ""
        if column == COL_REMOTE_TIME:
            if item.status == SyncStatus.ARCHIVE_CHANGED:
                return "older archive"
            return _when(item.remote.modified_timestamp) if item.remote else ""
        if column == COL_ACTION:
            arrow = "→ Upload" if item.direction == UPLOAD else "← Download"
            return arrow if item.checked else f"({arrow.split()[1].lower()} – skipped)"
        if column == COL_STATUS:
            return item.status.value
        return ""

    def _sort_value(self, item: SyncItem, column: int):
        if column == COL_TICK:
            return int(item.checked)
        if column == COL_LOCAL_SIZE:
            return item.archive.member_bytes if item.is_archive else (item.local.size if item.local else -1)
        if column == COL_LOCAL_TIME:
            return item.local.modified_timestamp if item.local else -1.0
        if column == COL_REMOTE_SIZE:
            return item.remote.size if item.remote else -1
        if column == COL_REMOTE_TIME:
            return item.remote.modified_timestamp if item.remote else -1.0
        if column == COL_NAME:
            return item.path.casefold()
        return self._display(item, column)

    def _row_changed(self, row: int) -> None:
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(HEADERS) - 1))
        self.changed.emit()

    def toggle_direction(self, row: int) -> None:
        item = self.plan.items[row]
        directions = item.allowed_directions
        if len(directions) > 1:
            item.set_direction(DOWNLOAD if item.direction == UPLOAD else UPLOAD)
            item.checked = True
        else:
            item.checked = not item.checked
        self._row_changed(row)

    def update_rows(self, rows: list[int], *, checked: bool | None = None, direction: str | None = None) -> int:
        """Bulk-edit rows. Rows that cannot go in *direction* are left alone."""
        changed = 0
        for row in rows:
            item = self.plan.items[row]
            if direction is not None:
                if direction not in item.allowed_directions:
                    continue
                item.set_direction(direction)
            if checked is not None:
                item.checked = checked
            changed += 1
        if rows:
            self.dataChanged.emit(self.index(min(rows), 0), self.index(max(rows), len(HEADERS) - 1))
            self.changed.emit()
        return changed

    def reset_to_mode(self, mode: str) -> None:
        self.beginResetModel()
        self.plan.apply_mode(mode)
        self.endResetModel()
        self.changed.emit()


class SyncFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.text = ""
        self.ticked_only = False
        self.setSortRole(SORT_ROLE)

    def set_text(self, text: str) -> None:
        self._refilter(lambda: setattr(self, "text", text.casefold().strip()))

    def set_ticked_only(self, enabled: bool) -> None:
        self._refilter(lambda: setattr(self, "ticked_only", enabled))

    def _refilter(self, change) -> None:
        # Qt 6.9+ replaced invalidateFilter() with begin/endFilterChange().
        if hasattr(self, "beginFilterChange"):
            self.beginFilterChange()
            change()
            self.endFilterChange()
        else:
            change()
            self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent) -> bool:  # noqa: N802
        item = self.sourceModel().plan.items[source_row]
        if self.ticked_only and not item.checked:
            return False
        return not self.text or self.text in item.path.casefold()


class SynchronizeDialog(QDialog):
    """Review differences, tick what to transfer, and pick each direction.

    Result codes: Accepted (transfer the ticked rows), Rejected, or
    RECOMPARE / RECOMPARE_SUGGESTED (the caller runs the comparison again).
    """

    RECOMPARE = 2
    RECOMPARE_SUGGESTED = 3

    def __init__(
        self,
        plan: SyncPlan,
        local_folder: str,
        remote_path: str,
        parent=None,
        *,
        suggested_remote_path: str | None = None,
        confirm=None,
    ) -> None:
        super().__init__(parent)
        self.plan = plan
        self.local_folder = local_folder
        self.remote_path = remote_path
        self.suggested_remote_path = suggested_remote_path
        # Replaceable in tests; returns True to go ahead.
        self._confirm = confirm or self._ask_confirmation
        self.setWindowTitle("Synchronise folders")
        self.resize(1180, 720)
        self.setSizeGripEnabled(True)

        layout = QVBoxLayout(self)
        layout.addWidget(self._paths_box())
        if suggested_remote_path:
            layout.addWidget(self._suggestion_banner(suggested_remote_path))
        layout.addLayout(self._mode_row())
        layout.addLayout(self._filter_row())

        self.model = SyncTableModel(plan, self)
        self.proxy = SyncFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(COL_NAME, Qt.AscendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAlternatingRowColors(False)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(24)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        for column, width in (
            (COL_TICK, 28),
            (COL_LOCAL_SIZE, 90),
            (COL_LOCAL_TIME, 120),
            (COL_ACTION, 140),
            (COL_REMOTE_SIZE, 90),
            (COL_REMOTE_TIME, 120),
            (COL_STATUS, 190),
        ):
            self.table.setColumnWidth(column, width)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_menu)
        self.table.clicked.connect(self._cell_clicked)
        layout.addWidget(self.table, stretch=1)

        layout.addLayout(self._bulk_row())
        layout.addWidget(self._legend())
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        summary_font = self.summary_label.font()
        summary_font.setPointSizeF(summary_font.pointSizeF() + 1)
        self.summary_label.setFont(summary_font)
        layout.addWidget(self.summary_label)
        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color: #9a5b00;")
        layout.addWidget(self.warning_label)
        layout.addLayout(self._button_row())

        self.model.changed.connect(self._update_summary)
        self._update_summary()

    # --- layout -------------------------------------------------------------

    def _paths_box(self) -> QFrame:
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        grid = QGridLayout(frame)
        local_title = QLabel("<b>This computer</b>")
        remote_title = QLabel("<b>QRIScloud</b>")
        local_path = QLabel(self.local_folder)
        remote_path = QLabel(self.remote_path)
        for label in (local_path, remote_path):
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        grid.addWidget(local_title, 0, 0)
        grid.addWidget(local_path, 0, 1)
        grid.addWidget(remote_title, 1, 0)
        grid.addWidget(remote_path, 1, 1)
        grid.setColumnStretch(1, 1)
        counts = QLabel(self._counts_text())
        counts.setStyleSheet("color: #5b6670;")
        grid.addWidget(counts, 0, 2, 2, 1, Qt.AlignRight | Qt.AlignVCenter)
        return frame

    def _counts_text(self) -> str:
        same = self.plan.identical_files + self.plan.identical_archives
        parts = [f"{len(self.plan.items):,} difference(s)", f"{same:,} already the same"]
        return "<br>".join(parts)

    def _suggestion_banner(self, path: str) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet("QFrame { background: #fff4d6; border: 1px solid #e3c27a; border-radius: 4px; }")
        row = QHBoxLayout(frame)
        folder = self.local_folder.rstrip("\\/").rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        text = QLabel(
            f"Nothing matches between the two folders, but QRIScloud has a folder named <b>{folder}</b> here. "
            f"Did you mean to compare with <b>{path}</b>?"
        )
        text.setWordWrap(True)
        button = QPushButton("Compare with that folder")
        button.clicked.connect(lambda: self.done(self.RECOMPARE_SUGGESTED))
        row.addWidget(text, stretch=1)
        row.addWidget(button)
        return frame

    def _mode_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("<b>Direction</b>"))
        self.mode_buttons = QButtonGroup(self)
        for mode, text, tip in (
            (MODE_BOTH, "Both ways (newer file wins)", "Copy missing files both ways and replace older copies."),
            (MODE_UPLOAD, "Upload only  (this computer → QRIScloud)", "Only tick files to send to QRIScloud."),
            (MODE_DOWNLOAD, "Download only  (QRIScloud → this computer)", "Only tick files to bring to this computer."),
        ):
            button = QRadioButton(text)
            button.setToolTip(tip + " You can still change any row.")
            button.setProperty("mode", mode)
            self.mode_buttons.addButton(button)
            row.addWidget(button)
            if mode == MODE_BOTH:
                button.setChecked(True)
        self.mode_buttons.buttonClicked.connect(lambda button: self.model.reset_to_mode(button.property("mode")))
        row.addStretch()
        return row

    def _filter_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter by name…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self.proxy_text_changed)
        self.ticked_only_box = QCheckBox("Show only ticked")
        self.ticked_only_box.toggled.connect(lambda on: self.proxy.set_ticked_only(on))
        row.addWidget(self.filter_edit, stretch=1)
        row.addWidget(self.ticked_only_box)
        return row

    def proxy_text_changed(self, text: str) -> None:
        self.proxy.set_text(text)

    def _bulk_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("Selected rows:"))
        for text, handler in (
            ("Tick", lambda: self._bulk(checked=True)),
            ("Untick", lambda: self._bulk(checked=False)),
            ("→ Upload", lambda: self._bulk(checked=True, direction=UPLOAD)),
            ("← Download", lambda: self._bulk(checked=True, direction=DOWNLOAD)),
        ):
            button = QPushButton(text)
            button.clicked.connect(handler)
            row.addWidget(button)
        row.addSpacing(20)
        tick_all = QPushButton("Tick all")
        untick_all = QPushButton("Untick all")
        tick_all.clicked.connect(lambda: self._bulk(checked=True, all_rows=True))
        untick_all.clicked.connect(lambda: self._bulk(checked=False, all_rows=True))
        row.addWidget(tick_all)
        row.addWidget(untick_all)
        row.addStretch()
        return row

    def _legend(self) -> QLabel:
        def chip(colour: QColor, text: str) -> str:
            return (
                f"<span style='background:{colour.name()}; color:#1f2a33;'>&nbsp;&nbsp;{text}&nbsp;&nbsp;</span>"
            )

        label = QLabel(
            " ".join(
                (
                    chip(UPLOAD_COLOUR, "upload"),
                    chip(DOWNLOAD_COLOUR, "download"),
                    chip(CONFLICT_COLOUR, "check this one"),
                    chip(ARCHIVE_COLOUR, "archive (.tar)"),
                )
            )
            + "&nbsp;&nbsp; Click the <b>Action</b> cell to swap direction. "
            "Nothing is ever deleted: files on only one side are copied across or left alone."
        )
        label.setWordWrap(True)
        return label

    def _button_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        recompare = QPushButton("Compare again")
        recompare.clicked.connect(lambda: self.done(self.RECOMPARE))
        row.addWidget(recompare)
        row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.sync_button = QPushButton("Synchronise")
        self.sync_button.setDefault(True)
        self.sync_button.clicked.connect(self._synchronise_clicked)
        row.addWidget(cancel)
        row.addWidget(self.sync_button)
        return row

    # --- behaviour -----------------------------------------------------------

    def _selected_source_rows(self) -> list[int]:
        rows = {self.proxy.mapToSource(index).row() for index in self.table.selectionModel().selectedRows()}
        return sorted(rows)

    def _bulk(self, *, checked: bool | None = None, direction: str | None = None, all_rows: bool = False) -> None:
        if all_rows:
            rows = [self.proxy.mapToSource(self.proxy.index(row, 0)).row() for row in range(self.proxy.rowCount())]
        else:
            rows = self._selected_source_rows()
        if not rows:
            return
        self.model.update_rows(rows, checked=checked, direction=direction)

    def _cell_clicked(self, proxy_index) -> None:
        if proxy_index.column() == COL_ACTION:
            self.model.toggle_direction(self.proxy.mapToSource(proxy_index).row())

    def _show_menu(self, position) -> None:
        index = self.table.indexAt(position)
        if index.isValid() and not self.table.selectionModel().isRowSelected(index.row(), QModelIndex()):
            self.table.selectRow(index.row())
        menu = QMenu(self)
        menu.addAction("Tick", lambda: self._bulk(checked=True))
        menu.addAction("Untick", lambda: self._bulk(checked=False))
        menu.addSeparator()
        menu.addAction("→ Upload to QRIScloud", lambda: self._bulk(checked=True, direction=UPLOAD))
        menu.addAction("← Download to this computer", lambda: self._bulk(checked=True, direction=DOWNLOAD))
        menu.exec(self.table.viewport().mapToGlobal(position))

    def totals(self) -> dict[str, tuple[int, int]]:
        result = {}
        for direction in (UPLOAD, DOWNLOAD):
            items = self.plan.chosen(direction)
            result[direction] = (len(items), sum(item.transfer_bytes() for item in items))
        return result

    def _update_summary(self) -> None:
        totals = self.totals()
        up_count, up_bytes = totals[UPLOAD]
        down_count, down_bytes = totals[DOWNLOAD]
        archives = sum(1 for item in self.plan.chosen(UPLOAD) if item.is_archive)
        up_text = f"<b>→ Upload {up_count:,}</b> item(s), {format_bytes(up_bytes)}"
        if archives:
            up_text += f" (including {archives:,} archive folder(s))"
        down_text = f"<b>← Download {down_count:,}</b> item(s), {format_bytes(down_bytes)}"
        self.summary_label.setText(f"{up_text} &nbsp;&nbsp;·&nbsp;&nbsp; {down_text}")
        newer = [item for item in (*self.plan.chosen(UPLOAD), *self.plan.chosen(DOWNLOAD)) if item.replaces_newer]
        conflicts = sum(
            1 for item in self.plan.items if item.checked and item.status == SyncStatus.CONFLICT
        )
        warnings = []
        if newer:
            warnings.append(f"⚠ {len(newer):,} ticked file(s) will replace a newer copy.")
        if conflicts:
            warnings.append(f"⚠ {conflicts:,} ticked file(s) differ in size but have the same time.")
        self.warning_label.setText("  ".join(warnings))
        self.warning_label.setVisible(bool(warnings))
        self.sync_button.setEnabled(up_count + down_count > 0)

    def confirmation_text(self) -> str:
        totals = self.totals()
        up_count, up_bytes = totals[UPLOAD]
        down_count, down_bytes = totals[DOWNLOAD]
        uploads = self.plan.chosen(UPLOAD)
        downloads = self.plan.chosen(DOWNLOAD)
        lines = []
        if up_count:
            lines.append(f"Upload {up_count:,} item(s), {format_bytes(up_bytes)}, to:\n    {self.remote_path}")
            archives = [item for item in uploads if item.is_archive]
            if archives:
                archived_bytes = sum(item.archive.member_bytes for item in archives)
                lines.append(
                    f"    {len(archives):,} folder(s) are packed into .tar archives first. "
                    f"This needs about {format_bytes(archived_bytes)} of free space beside the local folder."
                )
            replaced = sum(1 for item in uploads if item.replaces_existing)
            if replaced:
                lines.append(f"    {replaced:,} existing file(s) on QRIScloud will be replaced.")
        if down_count:
            lines.append(f"Download {down_count:,} item(s), {format_bytes(down_bytes)}, to:\n    {self.local_folder}")
            replaced = sum(1 for item in downloads if item.replaces_existing)
            if replaced:
                lines.append(f"    {replaced:,} existing file(s) on this computer will be replaced.")
        newer = sum(1 for item in (*uploads, *downloads) if item.replaces_newer)
        if newer:
            lines.append(f"\n⚠ {newer:,} of the replaced file(s) are newer than the copy replacing them.")
        lines.append("\nNothing is deleted on either side.")
        return "\n".join(lines)

    def _ask_confirmation(self) -> bool:
        return (
            QMessageBox.question(self, "Start synchronising?", self.confirmation_text(), QMessageBox.Yes | QMessageBox.No)
            == QMessageBox.Yes
        )

    def _synchronise_clicked(self) -> None:
        if not (self.plan.chosen(UPLOAD) or self.plan.chosen(DOWNLOAD)):
            return
        if self._confirm():
            self.accept()
