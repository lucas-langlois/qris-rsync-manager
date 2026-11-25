"""WinSCP-style right-click menus and keyboard shortcuts for the file panes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QAbstractItemView, QMenu


@dataclass
class PaneAction:
    label: str
    shortcuts: tuple[str, ...]
    callback: Callable[[], None]
    enabled: Callable[[], bool]


def install_pane_actions(view: QAbstractItemView, actions: list[PaneAction | None]) -> dict[str, QAction]:
    """Attach shortcuts and a context menu to an item view.

    None in the list draws a separator in the menu. Shortcuts only fire while
    the view itself has focus, so keys such as Backspace and Delete keep
    working normally in the path fields. Each action runs only when its
    enabled() check passes, which mirrors the matching button, so a shortcut
    can never bypass a disabled button.
    """
    # Installing twice would give each key two actions on one widget, which Qt
    # treats as an ambiguous shortcut and ignores. Replace any earlier set.
    for old in getattr(view, "pane_actions", {}).values():
        view.removeAction(old)
        old.deleteLater()
    old_menu_handler = getattr(view, "_pane_menu_handler", None)
    if old_menu_handler is not None:
        view.customContextMenuRequested.disconnect(old_menu_handler)

    created: dict[str, QAction] = {}
    ordered: list[tuple[QAction, PaneAction] | None] = []
    for spec in actions:
        if spec is None:
            ordered.append(None)
            continue
        action = QAction(spec.label, view)
        action.setShortcuts([QKeySequence(key) for key in spec.shortcuts])
        action.setShortcutContext(Qt.WidgetShortcut)
        action.triggered.connect(lambda _checked=False, spec=spec: _run(spec))
        view.addAction(action)
        created[spec.label] = action
        ordered.append((action, spec))

    def show_menu(position) -> None:
        _select_row_under_cursor(view, position)
        menu = QMenu(view)
        for entry in ordered:
            if entry is None:
                menu.addSeparator()
                continue
            action, spec = entry
            action.setEnabled(_safe_enabled(spec))
            menu.addAction(action)
        try:
            _show_menu(menu, view.viewport().mapToGlobal(position))
        finally:
            # Shortcuts re-check enabled() when they fire; keep actions live.
            for entry in ordered:
                if entry is not None:
                    entry[0].setEnabled(True)

    view.setContextMenuPolicy(Qt.CustomContextMenu)
    view.customContextMenuRequested.connect(show_menu)
    view._pane_menu_handler = show_menu
    view.pane_actions = created  # kept alive with the view; also handy in tests
    return created


def _show_menu(menu: QMenu, global_position) -> None:
    """Show a context menu modally (separate so tests can replace it)."""
    menu.exec(global_position)


def _run(spec: PaneAction) -> None:
    if _safe_enabled(spec):
        spec.callback()


def _safe_enabled(spec: PaneAction) -> bool:
    try:
        return bool(spec.enabled())
    except Exception:
        return False


def _select_row_under_cursor(view: QAbstractItemView, position) -> None:
    """Right-click acts on what is under the cursor, like a file manager.

    Clicking a row that is not selected selects just that row; clicking empty
    space clears the selection so actions apply to the current folder. This
    stops a menu action from silently using an older, off-screen selection.
    """
    index = view.indexAt(position)
    selection = view.selectionModel()
    if selection is None:
        return
    if not index.isValid():
        view.clearSelection()
        return
    if selection.isSelected(index):
        return
    if index.flags() & Qt.ItemIsSelectable:
        selection.select(index, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
        selection.setCurrentIndex(index, QItemSelectionModel.NoUpdate)
    else:
        view.clearSelection()
