from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from .branding import application_icon, application_version


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About AIOrganizer")
        self.setWindowIcon(application_icon())
        self.setMinimumWidth(590)

        outer = QVBoxLayout(self)
        content = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(application_icon().pixmap(112, 112))
        icon.setFixedSize(128, 128)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setAccessibleName("AIOrganizer application icon")
        content.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        text_layout = QVBoxLayout()
        name = QLabel("AIOrganizer")
        name.setObjectName("aboutProductName")
        name.setStyleSheet("font-size: 30px; font-weight: 700; color: #102A43;")
        text_layout.addWidget(name)
        version_label = QLabel(f"Version {application_version()} · alpha")
        version_label.setStyleSheet("color: #526777; font-size: 14px;")
        text_layout.addWidget(version_label)
        summary = QLabel(
            "Human-approved, AI-assisted organization for local files, software, research, "
            "records, and email metadata."
        )
        summary.setWordWrap(True)
        text_layout.addWidget(summary)
        safety = QLabel(
            "AI proposes; you review. Filesystem and mailbox changes require explicit validation "
            "and approval."
        )
        safety.setWordWrap(True)
        safety.setStyleSheet(
            "background: #ECFDF5; color: #134E4A; padding: 9px; border-radius: 5px;"
        )
        text_layout.addWidget(safety)
        project = QLabel(
            '<a href="https://github.com/GregoryMorse/AIOrganizer">'
            "github.com/GregoryMorse/AIOrganizer</a> · Apache-2.0"
        )
        project.setOpenExternalLinks(True)
        project.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        text_layout.addWidget(project)
        content.addLayout(text_layout, 1)
        outer.addLayout(content)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
