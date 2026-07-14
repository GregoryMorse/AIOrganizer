from __future__ import annotations

from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

INVALID_INDEX = QModelIndex()


class DictTableModel(QAbstractTableModel):
    def __init__(self, columns: list[tuple[str, str]], parent: Any = None) -> None:
        super().__init__(parent)
        self.columns = columns
        self.rows: list[dict[str, Any]] = []

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

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

    def row(self, index: QModelIndex) -> dict[str, Any] | None:
        return self.rows[index.row()] if index.isValid() else None
