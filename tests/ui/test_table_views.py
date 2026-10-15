from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHeaderView,
    QListWidget,
    QListWidgetItem,
    QTableView,
    QTreeWidget,
    QTreeWidgetItem,
)

from ai_organizer.desktop.table_models import DictTableModel
from ai_organizer.desktop.table_views import (
    _copy_list_selection,
    _copy_table_selection,
    _copy_tree_selection,
    configure_data_list,
    configure_data_table,
    configure_data_tree,
)


def test_bulk_table_is_multiselect_resizable_and_typed_sortable(qtbot) -> None:  # type: ignore[no-untyped-def]
    model = DictTableModel([("name", "Name"), ("detections", "Defender detections")])
    model.set_rows(
        [
            {"name": "ten", "detections": 10},
            {"name": "two", "detections": 2},
        ]
    )
    table = QTableView()
    qtbot.addWidget(table)
    table.setModel(model)
    configure_data_table(table)

    assert table.selectionMode() == QAbstractItemView.SelectionMode.ExtendedSelection
    assert table.isSortingEnabled()
    assert table.horizontalHeader().sectionResizeMode(0) == QHeaderView.ResizeMode.Interactive

    ten_row = next(index for index, row in enumerate(model.rows) if row["detections"] == 10)
    table.selectRow(ten_row)
    model.sort(1, Qt.SortOrder.AscendingOrder)
    assert [row["detections"] for row in model.rows] == [2, 10]
    selected = table.selectionModel().selectedRows()
    assert len(selected) == 1
    assert model.row(selected[0])["detections"] == 10  # type: ignore[index]
    assert model.remove_rows([0]) == 1
    assert model.rows[0]["detections"] == 10


def test_standard_context_menu_and_copy_support_for_all_data_views(qtbot) -> None:  # type: ignore[no-untyped-def]
    model = DictTableModel([("name", "Name"), ("status", "Status")])
    model.set_rows([{"name": "statement.pdf", "status": "OCR ready"}])
    table = QTableView()
    qtbot.addWidget(table)
    table.setModel(model)
    configure_data_table(table)
    table.selectRow(0)
    _copy_table_selection(table)
    assert QApplication.clipboard().text() == "statement.pdf\tOCR ready"

    tree = QTreeWidget()
    qtbot.addWidget(tree)
    tree.setColumnCount(2)
    tree_item = QTreeWidgetItem(["sender@example.test", "Attachment missing"])
    tree.addTopLevelItem(tree_item)
    configure_data_tree(tree)
    tree_item.setSelected(True)
    _copy_tree_selection(tree)
    assert QApplication.clipboard().text() == "sender@example.test\tAttachment missing"

    listing = QListWidget()
    qtbot.addWidget(listing)
    listing.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    list_item = QListWidgetItem("Source A")
    listing.addItem(list_item)
    configure_data_list(listing)
    list_item.setSelected(True)
    _copy_list_selection(listing)
    assert QApplication.clipboard().text() == "Source A"

    assert table.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu
    assert tree.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu
    assert listing.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu
