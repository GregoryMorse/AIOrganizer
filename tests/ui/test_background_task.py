from __future__ import annotations

from threading import Event

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog

from ai_organizer.desktop.background_task import BackgroundTaskDialog


@pytest.mark.ui
def test_cancellable_background_task_closes_when_cancelled(qtbot) -> None:  # type: ignore[no-untyped-def]
    cancelled = Event()

    def operation() -> None:
        cancelled.wait(2)
        if cancelled.is_set():
            raise RuntimeError("cancelled")

    dialog = BackgroundTaskDialog(
        "Waiting",
        "Waiting for a cancellable operation",
        operation,
        cancel_operation=cancelled.set,
    )
    qtbot.addWidget(dialog)
    QTimer.singleShot(50, dialog.reject)

    result = dialog.run()

    assert result == QDialog.DialogCode.Rejected
    assert dialog.cancel_requested
    assert cancelled.is_set()
