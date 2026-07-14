from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from ai_organizer.desktop.main_window import MainWindow


@pytest.mark.ui
def test_main_window_navigation(qtbot) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    assert window.windowTitle() == "AIOrganizer"
    assert window.navigation.count() == 12
    assert not bool(
        window.navigation.item(9).flags() & window.navigation.item(9).flags().ItemIsEnabled
    )


@pytest.mark.ui
def test_smoke_flag_starts_and_stops(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtWidgets import QApplication

    from ai_organizer.bootstrap.main import main

    application = QApplication.instance()
    assert application is not None
    monkeypatch.setattr("sys.argv", ["aiorganizer", "--smoke-test"])
    assert main() == 0
