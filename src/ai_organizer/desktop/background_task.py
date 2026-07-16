from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)


class BackgroundTaskWorker(QObject):
    completed = Signal(object)
    failed = Signal(str)
    progressed = Signal(int, int, str)

    def __init__(self, operation: Callable[..., Any], progress_aware: bool = False) -> None:
        super().__init__()
        self.operation = operation
        self.progress_aware = progress_aware

    def report_progress(self, completed: int, total: int, message: str = "") -> None:
        self.progressed.emit(max(0, completed), max(0, total), message)

    @Slot()
    def run(self) -> None:
        try:
            value = (
                self.operation(self.report_progress) if self.progress_aware else self.operation()
            )
            self.completed.emit(value)
        except Exception as error:
            self.failed.emit(f"{type(error).__name__}: {error}")


class BackgroundTaskDialog(QDialog):
    """Run a bounded extraction/provider call without blocking the Qt event loop."""

    def __init__(
        self,
        title: str,
        message: str,
        operation: Callable[..., Any],
        parent: QWidget | None = None,
        *,
        progress_aware: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(480)
        self.result_value: Any = None
        self.error_message = ""
        layout = QVBoxLayout(self)
        self.label = QLabel(message)
        self.label.setWordWrap(True)
        layout.addWidget(self.label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        layout.addWidget(self.progress)
        self.thread = QThread(self)
        self.worker = BackgroundTaskWorker(operation, progress_aware)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.completed.connect(self._completed)
        self.worker.failed.connect(self._failed)
        self.worker.progressed.connect(self._progressed)
        self.worker.completed.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)

    def run(self) -> int:
        self.thread.start()
        code = self.exec()
        self.thread.wait()
        return code

    def reject(self) -> None:
        if not self.thread.isRunning():
            super().reject()

    @Slot(object)
    def _completed(self, value: object) -> None:
        self.result_value = value
        self.accept()

    @Slot(str)
    def _failed(self, message: str) -> None:
        self.error_message = message
        QDialog.reject(self)

    @Slot(int, int, str)
    def _progressed(self, completed: int, total: int, message: str) -> None:
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(min(completed, total))
            self.progress.setFormat("%v / %m  (%p%)")
        else:
            self.progress.setRange(0, 0)
        if message:
            self.label.setText(message)
