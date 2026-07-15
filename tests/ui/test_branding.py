from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QLabel, QWizard

from ai_organizer.desktop.about import AboutDialog
from ai_organizer.desktop.branding import application_icon, brand_asset
from ai_organizer.desktop.main_window import MainWindow
from ai_organizer.desktop.onboarding import OnboardingWizard


def test_packaged_brand_svg_is_valid_and_scalable() -> None:
    asset = brand_asset()

    assert asset.is_file()
    root = ET.parse(asset).getroot()
    assert root.tag.endswith("svg")
    assert root.attrib["viewBox"] == "0 0 512 512"
    assert not application_icon().isNull()


@pytest.mark.ui
def test_onboarding_keeps_back_button_space_and_uses_brand_logo(qtbot, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = QSettings(str(tmp_path / "onboarding.ini"), QSettings.Format.IniFormat)
    wizard = OnboardingWizard(settings)
    qtbot.addWidget(wizard)

    assert not wizard.testOption(QWizard.WizardOption.NoBackButtonOnStartPage)
    assert wizard.sideWidget() is not None
    assert not wizard.windowIcon().isNull()


@pytest.mark.ui
def test_main_window_and_about_dialog_use_application_identity(qtbot) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow()
    about = AboutDialog(window)
    qtbot.addWidget(window)
    qtbot.addWidget(about)

    assert not window.windowIcon().isNull()
    assert not about.windowIcon().isNull()
    assert about.windowTitle() == "About AIOrganizer"
    assert any(label.text() == "AIOrganizer" for label in about.findChildren(QLabel))
