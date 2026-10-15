from __future__ import annotations

from collections.abc import Callable
from typing import Any
from weakref import WeakKeyDictionary

from PySide6.QtCore import QItemSelectionModel, QPoint, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHeaderView,
    QListWidget,
    QMenu,
    QTableView,
    QTreeWidget,
)

from .table_models import DictTableModel

_table_actions: WeakKeyDictionary[
    QTableView, Callable[[list[dict[str, Any]]], list[tuple[str, Callable[[], None]]]]
] = WeakKeyDictionary()
_tree_actions: WeakKeyDictionary[
    QTreeWidget, Callable[[list[Any]], list[tuple[str, Callable[[], None]]]]
] = WeakKeyDictionary()
_configured_tables: WeakKeyDictionary[QTableView, bool] = WeakKeyDictionary()
_configured_trees: WeakKeyDictionary[QTreeWidget, bool] = WeakKeyDictionary()
_configured_lists: WeakKeyDictionary[QListWidget, bool] = WeakKeyDictionary()


def configure_data_table(table: QTableView) -> None:
    """Apply the standard bulk-review behavior to a model-backed data table."""
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    table.setSortingEnabled(True)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setSectionsMovable(True)
    header.setStretchLastSection(True)
    header.setMinimumSectionSize(48)
    model = table.model()
    if isinstance(model, DictTableModel):
        selected_row_ids: list[int] = []

        def remember_selection() -> None:
            selected_row_ids.clear()
            selected_row_ids.extend(
                id(model.rows[index.row()])
                for index in table.selectionModel().selectedRows()
                if 0 <= index.row() < len(model.rows)
            )

        def restore_selection() -> None:
            wanted = set(selected_row_ids)
            selection = table.selectionModel()
            selection.clearSelection()
            for row_number, row in enumerate(model.rows):
                if id(row) in wanted:
                    selection.select(
                        model.index(row_number, 0),
                        QItemSelectionModel.SelectionFlag.Select
                        | QItemSelectionModel.SelectionFlag.Rows,
                    )

        model.sortingStarted.connect(remember_selection)
        model.sortingFinished.connect(restore_selection)
    _ensure_table_context_menu(table)


def configure_data_tree(tree: QTreeWidget, *, sortable: bool = True) -> None:
    """Apply multi-selection and user-resizable columns to a data tree/list."""
    tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    tree.setSortingEnabled(sortable)
    header = tree.header()
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setSectionsMovable(True)
    header.setStretchLastSection(True)
    header.setMinimumSectionSize(48)
    _ensure_tree_context_menu(tree)


def configure_data_list(widget: QListWidget) -> None:
    """Install conventional copy and bulk-selection actions on a list widget."""
    if widget in _configured_lists:
        return
    _configured_lists[widget] = True
    widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def show(position: QPoint) -> None:
        item = widget.itemAt(position)
        if item is not None and not item.isSelected():
            if widget.selectionMode() != QAbstractItemView.SelectionMode.MultiSelection:
                widget.clearSelection()
            item.setSelected(True)
        menu = QMenu(widget)
        _add_standard_actions(
            menu,
            lambda: _copy_list_selection(widget),
            widget.selectAll,
            widget.clearSelection,
            bool(widget.selectedItems()),
        )
        menu.exec(widget.viewport().mapToGlobal(position))

    widget.customContextMenuRequested.connect(show)


def selected_table_rows(table: QTableView, model: DictTableModel) -> list[dict[str, Any]]:
    indexes = sorted({index.row() for index in table.selectionModel().selectedRows()})
    return [model.rows[index] for index in indexes if 0 <= index < len(model.rows)]


def install_table_context_menu(
    table: QTableView,
    actions: Callable[[list[dict[str, Any]]], list[tuple[str, Callable[[], None]]]],
) -> None:
    _table_actions[table] = actions
    _ensure_table_context_menu(table)


def install_tree_context_menu(
    tree: QTreeWidget,
    actions: Callable[[list[Any]], list[tuple[str, Callable[[], None]]]],
) -> None:
    _tree_actions[tree] = actions
    _ensure_tree_context_menu(tree)


def _ensure_table_context_menu(table: QTableView) -> None:
    if table in _configured_tables:
        return
    _configured_tables[table] = True
    table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def show(position: QPoint) -> None:
        index = table.indexAt(position)
        if index.isValid() and not table.selectionModel().isRowSelected(
            index.row(), index.parent()
        ):
            table.selectRow(index.row())
        menu = QMenu(table)
        _add_standard_actions(
            menu,
            lambda: _copy_table_selection(table),
            table.selectAll,
            table.clearSelection,
            bool(table.selectionModel().selectedIndexes()),
        )
        model = table.model()
        action_factory = _table_actions.get(table)
        if isinstance(model, DictTableModel) and action_factory is not None:
            rows = selected_table_rows(table, model)
            custom = action_factory(rows) if rows else []
            if custom:
                menu.addSeparator()
                _add_actions(menu, custom)
        menu.exec(table.viewport().mapToGlobal(position))

    table.customContextMenuRequested.connect(show)


def _ensure_tree_context_menu(tree: QTreeWidget) -> None:
    if tree in _configured_trees:
        return
    _configured_trees[tree] = True
    tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def show(position: QPoint) -> None:
        item = tree.itemAt(position)
        if item is not None and not item.isSelected():
            tree.clearSelection()
            item.setSelected(True)
        items = tree.selectedItems()
        menu = QMenu(tree)
        _add_standard_actions(
            menu,
            lambda: _copy_tree_selection(tree),
            tree.selectAll,
            tree.clearSelection,
            bool(items),
        )
        action_factory = _tree_actions.get(tree)
        custom = action_factory(items) if items and action_factory is not None else []
        if custom:
            menu.addSeparator()
            _add_actions(menu, custom)
        menu.exec(tree.viewport().mapToGlobal(position))

    tree.customContextMenuRequested.connect(show)


def _add_standard_actions(
    menu: QMenu,
    copy: Callable[[], None],
    select_all: Callable[[], None],
    select_none: Callable[[], None],
    has_selection: bool,
) -> None:
    copy_action = QAction("Copy", menu)
    copy_action.setEnabled(has_selection)
    copy_action.triggered.connect(copy)
    menu.addAction(copy_action)
    select_all_action = QAction("Select All", menu)
    select_all_action.triggered.connect(select_all)
    menu.addAction(select_all_action)
    select_none_action = QAction("Select None", menu)
    select_none_action.setEnabled(has_selection)
    select_none_action.triggered.connect(select_none)
    menu.addAction(select_none_action)


def _add_actions(menu: QMenu, actions: list[tuple[str, Callable[[], None]]]) -> None:
    for label, callback in actions:
        action = QAction(label, menu)
        action.triggered.connect(callback)
        menu.addAction(action)


def _copy_table_selection(table: QTableView) -> None:
    model = table.model()
    selection = table.selectionModel()
    selected_rows = sorted({index.row() for index in selection.selectedIndexes()})
    if not selected_rows:
        return
    lines = []
    for row in selected_rows:
        values = [
            str(model.data(model.index(row, column), Qt.ItemDataRole.DisplayRole) or "")
            for column in range(model.columnCount())
        ]
        lines.append("\t".join(value.replace("\r", " ").replace("\n", " ") for value in values))
    QApplication.clipboard().setText("\n".join(lines))


def _copy_tree_selection(tree: QTreeWidget) -> None:
    lines = [
        "\t".join(
            item.text(column).replace("\r", " ").replace("\n", " ")
            for column in range(tree.columnCount())
        )
        for item in tree.selectedItems()
    ]
    if lines:
        QApplication.clipboard().setText("\n".join(lines))


def _copy_list_selection(widget: QListWidget) -> None:
    values = [item.text().replace("\r", " ").replace("\n", " ") for item in widget.selectedItems()]
    if values:
        QApplication.clipboard().setText("\n".join(values))
