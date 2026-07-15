from __future__ import annotations

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget, QWizard, QWizardPage

from .branding import application_icon


class OnboardingWizard(QWizard):
    SETTINGS_KEY = "onboarding/phase7Complete"

    def __init__(self, settings: QSettings, parent=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Welcome to AIOrganizer")
        self.setWindowIcon(application_icon())
        self.setMinimumSize(680, 440)
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        # Keep the Python wrapper alive for as long as QWizard owns the widget.
        # PySide can otherwise collect a temporary side widget after reparenting,
        # leaving Qt with an empty/compressed side area (and, on some builds, a
        # dangling wrapper during repaint).
        self._brand_side_widget = _brand_panel()
        self.setSideWidget(self._brand_side_widget)
        # Retain a disabled Back control on page one so the native header title
        # does not jump sideways when the arrow becomes active on page two.
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, False)
        self.addPage(
            _page(
                "Review first, apply second",
                "AI and MCP tools can inspect cached evidence and stage proposals. They cannot "
                "approve or commit changes. Every filesystem or mailbox mutation remains an "
                "explicit desktop review with current-state validation.",
            )
        )
        self.addPage(
            _page(
                "Fast Python development",
                "dev.cmd runs Python source directly. CI and release packaging remain manual-only. "
                "Normal development does not invoke Nuitka, C compilation, installers, or signing.",
            )
        )
        self.addPage(
            _page(
                "Start with copied data",
                "Create or open a .aioworkspace, add a copied test folder under Sources & Categories, "
                "then scan Inventory. Workspaces retain review state and operation journals; they do "
                "not contain provider or Microsoft access tokens.",
            )
        )
        self.addPage(
            _page(
                "Backups and privacy",
                "File > Backup Workspace creates a complete SQLite backup. Review exports can contain "
                "local paths and proposals; diagnostic exports deliberately exclude paths, filenames, "
                "document text, email metadata, and credentials.",
            )
        )

    def accept(self) -> None:
        self.settings.setValue(self.SETTINGS_KEY, True)
        super().accept()


def _page(title: str, body: str) -> QWizardPage:
    page = QWizardPage()
    page.setTitle(title)
    layout = QVBoxLayout(page)
    text = QLabel(body)
    text.setWordWrap(True)
    text.setAccessibleName(title)
    layout.addWidget(text)
    layout.addStretch()
    return page


def _brand_panel() -> QWidget:
    panel = QWidget()
    panel.setFixedWidth(190)
    panel.setObjectName("onboardingBrandPanel")
    panel.setStyleSheet(
        "QWidget#onboardingBrandPanel {"
        "background: qlineargradient(x1:0,y1:1,x2:1,y2:0, stop:0 #102A43, stop:1 #0F766E);"
        "} QLabel { color: white; background: transparent; }"
    )
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(22, 34, 22, 28)
    icon = QLabel()
    icon.setPixmap(application_icon().pixmap(112, 112))
    icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
    icon.setAccessibleName("AIOrganizer application icon")
    layout.addWidget(icon)
    name = QLabel("AIOrganizer")
    name.setAlignment(Qt.AlignmentFlag.AlignCenter)
    name.setStyleSheet("font-size: 21px; font-weight: 700;")
    layout.addWidget(name)
    tagline = QLabel("Human-approved\nAI-assisted")
    tagline.setAlignment(Qt.AlignmentFlag.AlignCenter)
    tagline.setStyleSheet("color: #CCFBF1; font-size: 12px;")
    layout.addWidget(tagline)
    layout.addStretch()
    boundary = QLabel("Review first.\nApply explicitly.")
    boundary.setAlignment(Qt.AlignmentFlag.AlignCenter)
    boundary.setStyleSheet("color: #D7FAF4; font-size: 12px;")
    layout.addWidget(boundary)
    return panel
