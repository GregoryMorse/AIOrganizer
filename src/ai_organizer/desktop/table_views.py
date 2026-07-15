from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QItemSelectionModel, QPoint, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QMenu,
    QTableView,
    QTreeWidget,
)

from .table_models import DictTableModel


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


def configure_data_tree(tree: QTreeWidget, *, sortable: bool = True) -> None:
    """Apply multi-selection and user-resizable columns to a data tree/list."""
    tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    tree.setSortingEnabled(sortable)
    header = tree.header()
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setSectionsMovable(True)
    header.setStretchLastSection(True)
    header.setMinimumSectionSize(48)


def selected_table_rows(table: QTableView, model: DictTableModel) -> list[dict[str, Any]]:
    indexes = sorted(
        {index.row() for index in table.selectionModel().selectedRows()}
    )
    return [model.rows[index] for index in indexes if 0 <= index < len(model.rows)]


def install_table_context_menu(
    table: QTableView,
    actions: Callable[[list[dict[str, Any]]], list[tuple[str, Callable[[], None]]]],
) -> None:
    table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def show(position: QPoint) -> None:
        index = table.indexAt(position)
        if index.isValid() and not table.selectionModel().isRowSelected(
            index.row(), index.parent()
        ):
            table.selectRow(index.row())
        model = table.model()
        if not isinstance(model, DictTableModel):
            return
        rows = selected_table_rows(table, model)
        if not rows:
            return
        menu = QMenu(table)
        for label, callback in actions(rows):
            action = QAction(label, menu)
            action.triggered.connect(callback)
            menu.addAction(action)
        if not menu.isEmpty():
            menu.exec(table.viewport().mapToGlobal(position))

    table.customContextMenuRequested.connect(show)


def install_tree_context_menu(
    tree: QTreeWidget,
    actions: Callable[[list[Any]], list[tuple[str, Callable[[], None]]]],
) -> None:
    tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def show(position: QPoint) -> None:
        item = tree.itemAt(position)
        if item is not None and not item.isSelected():
            tree.clearSelection()
            item.setSelected(True)
        items = tree.selectedItems()
        if not items:
            return
        menu = QMenu(tree)
        for label, callback in actions(items):
            action = QAction(label, menu)
            action.triggered.connect(callback)
            menu.addAction(action)
        if not menu.isEmpty():
            menu.exec(tree.viewport().mapToGlobal(position))

    tree.customContextMenuRequested.connect(show)
