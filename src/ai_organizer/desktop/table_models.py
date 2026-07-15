from __future__ import annotations

from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal

INVALID_INDEX = QModelIndex()


class DictTableModel(QAbstractTableModel):
    sortingStarted = Signal()
    sortingFinished = Signal()

    def __init__(self, columns: list[tuple[str, str]], parent: Any = None) -> None:
        super().__init__(parent)
        self.columns = columns
        self.rows: list[dict[str, Any]] = []
        self._sort_column: int | None = None
        self._sort_order = Qt.SortOrder.AscendingOrder

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()
        if self._sort_column is not None:
            self.sort(self._sort_column, self._sort_order)

    def remove_rows(self, row_numbers: list[int]) -> int:
        """Remove view rows in one reset while retaining the original row dictionaries."""
        selected = {value for value in row_numbers if 0 <= value < len(self.rows)}
        if not selected:
            return 0
        self.beginResetModel()
        self.rows = [row for index, row in enumerate(self.rows) if index not in selected]
        self.endResetModel()
        return len(selected)

    def set_columns_and_rows(
        self, columns: list[tuple[str, str]], rows: list[dict[str, Any]]
    ) -> None:
        self.beginResetModel()
        self.columns = columns
        self.rows = rows
        self.endResetModel()
        if self._sort_column is not None and self._sort_column < len(columns):
            self.sort(self._sort_column, self._sort_order)

    def rowCount(self, parent: QModelIndex = INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        key = self.columns[index.column()][0]
        value = self.rows[index.row()].get(key, "")
        if key == "selected" and role == Qt.ItemDataRole.CheckStateRole:
            return Qt.CheckState.Checked if bool(value) else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.EditRole:
            return value
        if role not in {
            Qt.ItemDataRole.DisplayRole,
            Qt.ItemDataRole.ToolTipRole,
        }:
            return None
        if key == "selected":
            return ""
        if isinstance(value, (list, set, tuple)):
            return ", ".join(map(str, value))
        if isinstance(value, bool):
            return "Yes" if value else "No"
        return str(value)

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        flags = super().flags(index)
        if not index.isValid():
            return flags
        key = self.columns[index.column()][0]
        if key == "selected":
            return flags | Qt.ItemFlag.ItemIsUserCheckable
        if key in {"proposed", "projected", "destination"}:
            return flags | Qt.ItemFlag.ItemIsEditable
        return flags

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if not index.isValid():
            return False
        key = self.columns[index.column()][0]
        if key == "selected" and role == Qt.ItemDataRole.CheckStateRole:
            self.rows[index.row()][key] = value == Qt.CheckState.Checked.value
        elif key in {"proposed", "projected", "destination"} and role == Qt.ItemDataRole.EditRole:
            self.rows[index.row()][key] = str(value)
        else:
            return False
        self.dataChanged.emit(index, index, [role])
        return True

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.columns[section][1]
        return super().headerData(section, orientation, role)

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        """Provide stable, typed sorting for QTableView header clicks."""
        if not 0 <= column < len(self.columns):
            return
        self._sort_column = column
        self._sort_order = order
        key = self.columns[column][0]

        def sortable(row: dict[str, Any]) -> tuple[int, Any]:
            value = row.get(key)
            if value is None or value == "":
                return (3, "")
            if isinstance(value, bool):
                return (0, int(value))
            if isinstance(value, (int, float)):
                return (0, value)
            if isinstance(value, (list, set, tuple)):
                return (1, len(value))
            return (2, str(value).casefold())

        old_rows = list(self.rows)
        old_indexes = [
            self.index(row_index, column_index)
            for row_index in range(len(old_rows))
            for column_index in range(len(self.columns))
        ]
        self.sortingStarted.emit()
        self.layoutAboutToBeChanged.emit()
        self.rows.sort(
            key=sortable,
            reverse=order == Qt.SortOrder.DescendingOrder,
        )
        new_positions = {id(row): index for index, row in enumerate(self.rows)}
        new_indexes = [
            self.index(new_positions[id(row)], column_index)
            for row in old_rows
            for column_index in range(len(self.columns))
        ]
        self.changePersistentIndexList(old_indexes, new_indexes)
        self.layoutChanged.emit()
        self.sortingFinished.emit()

    def row(self, index: QModelIndex) -> dict[str, Any] | None:
        return self.rows[index.row()] if index.isValid() else None
