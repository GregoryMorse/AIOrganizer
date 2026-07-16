from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableView

from ai_organizer.desktop.table_models import DictTableModel
from ai_organizer.desktop.table_views import configure_data_table


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
