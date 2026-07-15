from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from ai_organizer.desktop.guidance import GuidanceContextBar, GuidancePanel
from ai_organizer.desktop.main_window import MainWindow
from ai_organizer.desktop.pages import FolderPlanPage, SettingsPage
from ai_organizer.domain.models import FolderRole


@pytest.mark.ui
def test_main_window_navigation(qtbot) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    assert window.windowTitle() == "AIOrganizer"
    assert window.navigation.count() == 14
    assert window.navigation.item(3).text() == "Audit"
    assert window.navigation.item(4).text() == "Updates"
    assert window.navigation.item(8).text() == "Cleanup"
    assert bool(
        window.navigation.item(8).flags() & window.navigation.item(8).flags().ItemIsEnabled
    )
    assert window.navigation.item(9).text() == "Recurrences"
    assert bool(
        window.navigation.item(9).flags() & window.navigation.item(9).flags().ItemIsEnabled
    )
    assert window.navigation.item(13).text() == "Email"
    assert bool(
        window.navigation.item(13).flags() & window.navigation.item(13).flags().ItemIsEnabled
    )
    rename_page = window.stack.widget(5)
    assert len(rename_page.findChildren(GuidanceContextBar)) == 1
    assert not rename_page.findChildren(GuidancePanel)
    settings_page = window.stack.widget(12)
    assert isinstance(settings_page, SettingsPage)
    assert settings_page.tabs.count() == 11
    assert len(settings_page.findChildren(GuidancePanel)) == 8
    assert "Updates" in [
        settings_page.tabs.tabText(index) for index in range(settings_page.tabs.count())
    ]
    updates_panel = settings_page.guidance_panels["updates"]
    assert updates_panel.provider.findText("deepseek") >= 0
    assert updates_panel.provider.findText("openrouter") >= 0


@pytest.mark.ui
def test_folder_plan_shows_only_encoding_safe_review_delta(qtbot, tmp_path) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow()
    qtbot.addWidget(window)
    source_path = tmp_path / "destination"
    source_path.mkdir()
    window.controller.create_workspace(tmp_path / "delta.aioworkspace", "Delta")
    personal_id = next(
        value["id"]
        for value in window.controller.store.list_category_payloads()
        if value.get("semantic_key") == "personal"
    )
    source = window.controller.add_source(
        source_path,
        roles={FolderRole.DESTINATION},
        category_ids={personal_id},
    )
    window.controller.items = [
        {
            "id": "existing",
            "root_id": source.id,
            "relative_path": "Personal",
            "is_dir": True,
        }
    ]
    page = window.stack.widget(6)
    assert isinstance(page, FolderPlanPage)

    page.generate()

    assert page.model.rows
    assert {row["action"] for row in page.model.rows} <= {"create", "rename"}
    assert all("â" not in str(row["node"]) for row in page.model.rows)


@pytest.mark.ui
def test_smoke_flag_starts_and_stops(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtWidgets import QApplication

    from ai_organizer.bootstrap.main import main

    application = QApplication.instance()
    assert application is not None
    monkeypatch.setattr("sys.argv", ["aiorganizer", "--smoke-test"])
    assert main() == 0
