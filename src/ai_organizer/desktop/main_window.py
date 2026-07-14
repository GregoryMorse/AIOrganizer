from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFileDialog,
    QInputDialog,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStackedWidget,
)

from .controller import WorkspaceController
from .pages import (
    ActivityPage,
    FocusedActionsPage,
    FolderPlanPage,
    InventoryPage,
    MovePage,
    OverviewPage,
    RenamePage,
    RoadmapPage,
    SettingsPage,
    SourcesCategoriesPage,
)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("AIOrganizer")
        self.resize(1280, 820)
        self.controller = WorkspaceController()
        self.settings = QSettings("AIOrganizer", "AIOrganizer")
        self.navigation = QListWidget()
        self.navigation.setFixedWidth(210)
        self.stack = QStackedWidget()
        pages = [
            ("Overview", OverviewPage(self.controller), True),
            ("Sources & Categories", SourcesCategoriesPage(self.controller), True),
            ("Inventory", InventoryPage(self.controller), True),
            ("Rename", RenamePage(self.controller), True),
            ("Folder Plan", FolderPlanPage(self.controller), True),
            ("Move", MovePage(self.controller), True),
            ("Focused Actions", FocusedActionsPage(self.controller), True),
            ("Activity", ActivityPage(self.controller), True),
            ("Settings", SettingsPage(self.controller), True),
            (
                "Cleanup (later)",
                RoadmapPage(
                    "Cleanup",
                    "Future explicit review for empty folders, artifacts, and quarantine.",
                ),
                False,
            ),
            (
                "Recurrences (later)",
                RoadmapPage(
                    "Recurrences", "Future recurring-document coverage and missing-period analysis."
                ),
                False,
            ),
            (
                "Email (later)",
                RoadmapPage(
                    "Email", "Future one-account-at-a-time Outlook organization workflows."
                ),
                False,
            ),
        ]
        for label, page, enabled in pages:
            item = QListWidgetItem(label)
            item.setFlags(item.flags() if enabled else item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self.navigation.addItem(item)
            self.stack.addWidget(page)
        self.navigation.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.navigation.setCurrentRow(0)
        splitter = QSplitter()
        splitter.addWidget(self.navigation)
        splitter.addWidget(self.stack)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)
        self._create_menu()
        self.setStyleSheet(
            """
            QLabel#pageTitle { font-size: 24px; font-weight: 600; margin-bottom: 8px; }
            QLabel#safetyBanner { background: #fff4ce; color: #5c4500; padding: 12px; border-radius: 4px; }
            QListWidget { font-size: 14px; padding: 6px; }
            QListWidget::item { padding: 9px; }
            """
        )

    def _create_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        create = QAction("New workspace…", self)
        create.triggered.connect(self.new_workspace)
        open_action = QAction("Open workspace…", self)
        open_action.triggered.connect(self.open_workspace)
        save_as = QAction("Save workspace as…", self)
        save_as.triggered.connect(self.save_as)
        quit_action = QAction("Exit", self)
        quit_action.triggered.connect(self.close)
        file_menu.addActions([create, open_action, save_as])
        self.recent_menu = file_menu.addMenu("Open recent")
        self._refresh_recent_menu()
        file_menu.addSeparator()
        file_menu.addAction(quit_action)

    def new_workspace(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Create workspace", "", "AIOrganizer workspace (*.aioworkspace)"
        )
        if not path:
            return
        name, accepted = QInputDialog.getText(self, "Workspace name", "Name")
        if not accepted or not name.strip():
            return
        try:
            self.controller.create_workspace(Path(path), name.strip())
            self._remember_workspace(path)
        except Exception as error:
            QMessageBox.critical(self, "Cannot create workspace", str(error))

    def open_workspace(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open workspace", "", "AIOrganizer workspace (*.aioworkspace)"
        )
        if not path:
            return
        try:
            self.controller.open_workspace(Path(path))
            self._remember_workspace(path)
        except Exception as error:
            QMessageBox.critical(self, "Cannot open workspace", str(error))

    def save_as(self) -> None:
        if not self.controller.store:
            QMessageBox.warning(self, "Workspace required", "Open a workspace first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save workspace as", "", "AIOrganizer workspace (*.aioworkspace)"
        )
        if path:
            self.controller.store.save_as(Path(path))

    def _remember_workspace(self, path: str) -> None:
        recent = self.settings.value("recentWorkspaces", [], list)
        normalized = str(Path(path).resolve(strict=False))
        values = [normalized, *(value for value in recent if value != normalized)][:10]
        self.settings.setValue("recentWorkspaces", values)
        self._refresh_recent_menu()

    def _refresh_recent_menu(self) -> None:
        self.recent_menu.clear()
        recent = self.settings.value("recentWorkspaces", [], list)
        if not recent:
            empty = self.recent_menu.addAction("No recent workspaces")
            empty.setEnabled(False)
            return
        for path in recent:
            action = self.recent_menu.addAction(str(path))
            action.triggered.connect(
                lambda checked=False, value=str(path): self._open_recent(value)
            )

    def _open_recent(self, path: str) -> None:
        try:
            self.controller.open_workspace(Path(path))
            self._remember_workspace(path)
        except Exception as error:
            QMessageBox.critical(self, "Cannot open workspace", str(error))

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self.controller.close()
        super().closeEvent(event)
