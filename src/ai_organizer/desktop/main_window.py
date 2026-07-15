from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
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

from .about import AboutDialog
from .branding import application_icon
from .controller import WorkspaceController
from .onboarding import OnboardingWizard
from .pages import (
    ActivityPage,
    AuditPage,
    CleanupPage,
    EmailPage,
    FocusedActionsPage,
    FolderPlanPage,
    InventoryPage,
    MovePage,
    OverviewPage,
    RecurrencesPage,
    RenamePage,
    SettingsPage,
    SourcesCategoriesPage,
    UpdatesPage,
)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("AIOrganizer")
        self.setWindowIcon(application_icon())
        self.resize(1280, 820)
        self.controller = WorkspaceController()
        self.settings = QSettings("AIOrganizer", "AIOrganizer")
        self.navigation = QListWidget()
        self.navigation.setFixedWidth(210)
        self.navigation.setAccessibleName("Application sections")
        self.navigation.setToolTip("Use Up and Down arrows to move between application sections")
        self.stack = QStackedWidget()
        pages = [
            ("Overview", OverviewPage(self.controller), True),
            ("Sources & Categories", SourcesCategoriesPage(self.controller), True),
            ("Inventory", InventoryPage(self.controller), True),
            ("Audit", AuditPage(self.controller), True),
            ("Updates", UpdatesPage(self.controller), True),
            ("Rename", RenamePage(self.controller), True),
            ("Folder Plan", FolderPlanPage(self.controller), True),
            ("Move", MovePage(self.controller), True),
            ("Cleanup", CleanupPage(self.controller), True),
            ("Recurrences", RecurrencesPage(self.controller), True),
            ("Focused Actions", FocusedActionsPage(self.controller), True),
            ("Activity", ActivityPage(self.controller), True),
            ("Settings", SettingsPage(self.controller), True),
            ("Email", EmailPage(self.controller), True),
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
            QListWidget { padding: 6px; }
            QListWidget::item { padding: 9px; }
            """
        )
        if not self.settings.value(OnboardingWizard.SETTINGS_KEY, False, bool):
            from PySide6.QtWidgets import QApplication

            application = QApplication.instance()
            if application is not None and not bool(
                application.property("aiorganizerSmokeTest")
            ):
                QTimer.singleShot(0, self.show_onboarding)

    def _create_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        create = QAction("New workspace…", self)
        create.setShortcut(QKeySequence.StandardKey.New)
        create.setStatusTip("Create a new local AIOrganizer workspace")
        create.triggered.connect(self.new_workspace)
        open_action = QAction("Open workspace…", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.setStatusTip("Open an existing local AIOrganizer workspace")
        open_action.triggered.connect(self.open_workspace)
        save_as = QAction("Save workspace as…", self)
        save_as.setShortcut(QKeySequence.StandardKey.SaveAs)
        save_as.triggered.connect(self.save_as)
        backup = QAction("Backup workspace…", self)
        backup.triggered.connect(self.backup_workspace)
        export_review = QAction("Export review bundle…", self)
        export_review.triggered.connect(self.export_review_bundle)
        export_diagnostics = QAction("Export diagnostic bundle…", self)
        export_diagnostics.triggered.connect(self.export_diagnostic_bundle)
        import_outlook = QAction("Import Outlook selection metadata…", self)
        import_outlook.triggered.connect(self.import_outlook_handoff)
        quit_action = QAction("Exit", self)
        quit_action.triggered.connect(self.close)
        file_menu.addActions([create, open_action, save_as, backup])
        export_menu = file_menu.addMenu("Export")
        export_menu.addActions([export_review, export_diagnostics])
        file_menu.addAction(import_outlook)
        self.recent_menu = file_menu.addMenu("Open recent")
        self._refresh_recent_menu()
        file_menu.addSeparator()
        file_menu.addAction(quit_action)
        help_menu = self.menuBar().addMenu("&Help")
        welcome = QAction("Welcome and safety tour…", self)
        welcome.triggered.connect(self.show_onboarding)
        help_menu.addAction(welcome)
        help_menu.addSeparator()
        about = QAction("About AIOrganizer…", self)
        about.triggered.connect(self.show_about)
        help_menu.addAction(about)

    def show_onboarding(self) -> None:
        self.onboarding = OnboardingWizard(self.settings, self)
        self.onboarding.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.onboarding.show()

    def show_about(self) -> None:
        self.about_dialog = AboutDialog(self)
        self.about_dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.about_dialog.show()

    def backup_workspace(self) -> None:
        if not self.controller.store:
            QMessageBox.warning(self, "Workspace required", "Open a workspace first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Backup workspace", "", "AIOrganizer workspace (*.aioworkspace)"
        )
        if not path:
            return
        try:
            from ai_organizer.application.export_service import WorkspaceExportService

            target = WorkspaceExportService(self.controller.store).backup(Path(path))
            QMessageBox.information(self, "Backup complete", f"Workspace backed up to {target}")
        except Exception as error:
            QMessageBox.critical(self, "Backup failed", str(error))

    def export_review_bundle(self) -> None:
        if not self.controller.store:
            QMessageBox.warning(self, "Workspace required", "Open a workspace first.")
            return
        answer = QMessageBox.warning(
            self,
            "Review export contains private metadata",
            "This export can contain local paths, filenames, proposal rationales, and operation history. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export review bundle", "", "ZIP archive (*.zip)"
        )
        if path:
            self._export_bundle(Path(path), diagnostics=False)

    def export_diagnostic_bundle(self) -> None:
        if not self.controller.store:
            QMessageBox.warning(self, "Workspace required", "Open a workspace first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export private-data-free diagnostics", "", "ZIP archive (*.zip)"
        )
        if path:
            self._export_bundle(Path(path), diagnostics=True)

    def _export_bundle(self, path: Path, *, diagnostics: bool) -> None:
        try:
            from ai_organizer.application.export_service import WorkspaceExportService

            if not self.controller.store:
                raise RuntimeError("Open a workspace first")
            exporter = WorkspaceExportService(self.controller.store)
            target = (
                exporter.export_diagnostic_bundle(path)
                if diagnostics
                else exporter.export_review_bundle(path)
            )
            QMessageBox.information(self, "Export complete", f"Created {target}")
        except Exception as error:
            QMessageBox.critical(self, "Export failed", str(error))

    def import_outlook_handoff(self) -> None:
        if not self.controller.store:
            QMessageBox.warning(self, "Workspace required", "Open a workspace first.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Outlook selection metadata",
            "",
            "AIOrganizer Outlook metadata (*.json)",
        )
        if not path:
            return
        try:
            from ai_organizer.application.outlook_handoff import OutlookHandoffService

            handoff_id = OutlookHandoffService(self.controller.store).import_file(Path(path))
            self.controller.workspace_changed.emit()
            QMessageBox.information(
                self,
                "Outlook metadata imported",
                f"Imported {handoff_id}. The payload remains untrusted and has no apply authority.",
            )
        except Exception as error:
            QMessageBox.critical(self, "Outlook metadata rejected", str(error))

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
