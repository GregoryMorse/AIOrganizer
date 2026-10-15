from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
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
    DocumentRepairPage,
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
    SystemPage,
    UpdatesPage,
)
from .table_views import configure_data_list


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("AIOrganizer")
        self.setWindowIcon(application_icon())
        self.resize(1280, 820)
        self.controller = WorkspaceController()
        self.settings = QSettings("AIOrganizer", "AIOrganizer")
        self.navigation = QListWidget()
        self.navigation.setObjectName("navigationList")
        self.navigation.setFixedWidth(210)
        self.navigation.setAccessibleName("Application sections")
        self.navigation.setToolTip("Use Up and Down arrows to move between application sections")
        configure_data_list(self.navigation)
        self.stack = QStackedWidget()
        self.file_pages = [
            ("Overview", OverviewPage(self.controller), True),
            ("Sources & Categories", SourcesCategoriesPage(self.controller), True),
            ("Inventory", InventoryPage(self.controller), True),
            ("Audit", AuditPage(self.controller), True),
            ("Updates", UpdatesPage(self.controller), True),
            ("Document Repair", DocumentRepairPage(self.controller), True),
            ("Rename", RenamePage(self.controller), True),
            ("Folder Plan", FolderPlanPage(self.controller), True),
            ("Move", MovePage(self.controller), True),
            ("Cleanup", CleanupPage(self.controller), True),
            ("Recurrences", RecurrencesPage(self.controller), True),
            ("Focused Actions", FocusedActionsPage(self.controller), True),
            ("Activity", ActivityPage(self.controller), True),
            ("Settings", SettingsPage(self.controller), True),
        ]
        self.email_page = EmailPage(self.controller)
        self.mail_settings_page = SettingsPage(self.controller, mode="mail")
        self.mail_pages = [
            ("Folder Proposals", self.email_page, 0),
            ("Rule Proposals", self.email_page, 1),
            ("Focused Actions", self.email_page, 2),
            ("Settings", self.mail_settings_page, None),
        ]
        self.system_page = SystemPage(self.controller)
        self.system_application_page = UpdatesPage(self.controller, content_scope="software")
        self.system_settings_page = SettingsPage(self.controller, mode="system")
        self.system_pages = [
            ("Applications", self.system_application_page, None),
            ("Drivers", self.system_page, 1),
            ("Windows Update", self.system_page, 2),
            ("Health", self.system_page, 3),
            ("Settings", self.system_settings_page, None),
        ]
        for _label, page, _enabled in self.file_pages:
            self.stack.addWidget(page)
        self.stack.addWidget(self.email_page)
        self.stack.addWidget(self.mail_settings_page)
        self.stack.addWidget(self.system_page)
        self.stack.addWidget(self.system_application_page)
        self.stack.addWidget(self.system_settings_page)
        self.navigation.currentRowChanged.connect(self._navigate)
        splitter = QSplitter()
        splitter.addWidget(self.navigation)
        splitter.addWidget(self.stack)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)
        self._create_menu()
        self.set_mode("files")
        self.setStyleSheet(
            """
            QLabel#pageTitle { font-size: 24px; font-weight: 600; margin-bottom: 8px; }
            QLabel#safetyBanner { background: #fff4ce; color: #5c4500; padding: 12px; border-radius: 4px; }
            QListWidget#navigationList { padding: 6px; }
            QListWidget#navigationList::item { padding: 9px; }
            QListWidget[compactChecklist="true"] { padding: 1px; }
            QListWidget[compactChecklist="true"]::item { padding: 1px 4px; }
            """
        )
        if not self.settings.value(OnboardingWizard.SETTINGS_KEY, False, bool):
            from PySide6.QtWidgets import QApplication

            application = QApplication.instance()
            if application is not None and not bool(application.property("aiorganizerSmokeTest")):
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
        mode_menu = self.menuBar().addMenu("&Mode")
        self.mode_group = QActionGroup(self)
        self.mode_group.setExclusive(True)
        self.file_mode_action = QAction("Files && Folders", self)
        self.file_mode_action.setCheckable(True)
        self.file_mode_action.setData("files")
        self.file_mode_action.setStatusTip("Work with local file and folder tools")
        self.mail_mode_action = QAction("Mail", self)
        self.mail_mode_action.setCheckable(True)
        self.mail_mode_action.setData("mail")
        self.mail_mode_action.setStatusTip(
            "Switch the whole workspace between files/folders and mailbox tools"
        )
        self.system_mode_action = QAction("System", self)
        self.system_mode_action.setCheckable(True)
        self.system_mode_action.setData("system")
        self.system_mode_action.setStatusTip(
            "Assess Windows applications, drivers, updates, and system health"
        )
        for action in (
            self.file_mode_action,
            self.mail_mode_action,
            self.system_mode_action,
        ):
            self.mode_group.addAction(action)
            mode_menu.addAction(action)
            action.toggled.connect(
                lambda checked, selected=action: (
                    self.set_mode(str(selected.data()))
                    if checked and getattr(self, "current_mode", "") != selected.data()
                    else None
                )
            )
        self.mailbox_menu = self.menuBar().addMenu("&Mailbox")
        connect_mailbox = QAction("Connect another Outlook mailbox…", self)
        connect_mailbox.triggered.connect(self.connect_another_mailbox)
        self.mailbox_menu.addAction(connect_mailbox)
        self.select_mailbox_menu = self.mailbox_menu.addMenu("Select active mailbox")
        self.select_mailbox_menu.aboutToShow.connect(self._refresh_mailbox_menu)
        manage_mailboxes = QAction("Manage configured mailboxes…", self)
        manage_mailboxes.triggered.connect(self.manage_mailboxes)
        self.mailbox_menu.addAction(manage_mailboxes)
        self.mailbox_menu.aboutToShow.connect(self._refresh_mailbox_menu)
        self.controller.workspace_changed.connect(self._refresh_mailbox_menu)
        self.controller.mail_account_changed.connect(self._refresh_mailbox_menu)
        help_menu = self.menuBar().addMenu("&Help")
        welcome = QAction("Welcome and safety tour…", self)
        welcome.triggered.connect(self.show_onboarding)
        help_menu.addAction(welcome)
        help_menu.addSeparator()
        about = QAction("About AIOrganizer…", self)
        about.triggered.connect(self.show_about)
        help_menu.addAction(about)

    def set_mail_mode(self, enabled: bool) -> None:
        """Compatibility wrapper for callers that previously toggled mail mode."""
        self.set_mode("mail" if enabled else "files")

    def set_mode(self, mode: str) -> None:
        """Switch the complete navigation surface between isolated tool families."""
        if mode not in {"files", "mail", "system"}:
            raise ValueError("Mode must be files, mail, or system")
        self.current_mode = mode
        actions = {
            "files": self.file_mode_action,
            "mail": self.mail_mode_action,
            "system": self.system_mode_action,
        }
        actions[mode].setChecked(True)
        self.navigation.blockSignals(True)
        self.navigation.clear()
        if mode == "mail":
            for label, _page, _section in self.mail_pages:
                self.navigation.addItem(QListWidgetItem(label))
            self.stack.setCurrentWidget(self.email_page)
            self.email_page.set_section(0)
        elif mode == "system":
            for label, _page, _section in self.system_pages:
                self.navigation.addItem(QListWidgetItem(label))
            self.stack.setCurrentWidget(self.system_application_page)
        else:
            for label, _page, available in self.file_pages:
                item = QListWidgetItem(label)
                item.setFlags(
                    item.flags() if available else item.flags() & ~Qt.ItemFlag.ItemIsEnabled
                )
                self.navigation.addItem(item)
            self.stack.setCurrentWidget(self.file_pages[0][1])
        self.setWindowTitle("AIOrganizer")
        self.navigation.setCurrentRow(0)
        self.navigation.blockSignals(False)
        self.mailbox_menu.menuAction().setVisible(mode == "mail")
        self._refresh_mailbox_menu()

    def _refresh_mailbox_menu(self) -> None:
        if not hasattr(self, "select_mailbox_menu"):
            return
        self.select_mailbox_menu.clear()
        self.mailbox_action_group = QActionGroup(self.select_mailbox_menu)
        self.mailbox_action_group.setExclusive(True)
        accounts = self.controller.store.list_email_accounts() if self.controller.store else []
        if not accounts:
            empty = self.select_mailbox_menu.addAction("No configured mailboxes")
            empty.setEnabled(False)
            if getattr(self, "current_mode", "") == "mail":
                self.setWindowTitle("AIOrganizer — Mail")
            return
        active_username = ""
        for account in accounts:
            username = str(account.get("username", ""))
            display_name = str(account.get("display_name", ""))
            identity = f"{display_name} <{username}>" if display_name != username else username
            provider = (
                "Classic Outlook"
                if account.get("connector_kind") == "classic_outlook"
                else "Graph"
            )
            label = f"{identity} [{provider}]"
            action = QAction(label, self.select_mailbox_menu)
            action.setCheckable(True)
            action.setChecked(bool(account.get("active")))
            action.setData(str(account["id"]))
            action.triggered.connect(
                lambda checked=False, account_id=str(account["id"]): (
                    self.select_mailbox(account_id) if checked else None
                )
            )
            self.mailbox_action_group.addAction(action)
            self.select_mailbox_menu.addAction(action)
            if account.get("active"):
                active_username = username
        if getattr(self, "current_mode", "") == "mail":
            self.setWindowTitle(
                f"AIOrganizer — Mail — {active_username}" if active_username else "AIOrganizer — Mail"
            )

    def select_mailbox(self, account_id: str) -> None:
        try:
            self.email_page.activate_mailbox(account_id)
            self._refresh_mailbox_menu()
        except Exception as error:
            QMessageBox.critical(self, "Mailbox could not be activated", str(error))

    def connect_another_mailbox(self) -> None:
        if not self.controller.store:
            QMessageBox.warning(self, "Workspace required", "Open a workspace first.")
            return
        self.set_mode("mail")
        self.email_page.sign_in()
        self._refresh_mailbox_menu()

    def manage_mailboxes(self) -> None:
        self.set_mode("mail")
        self.navigation.setCurrentRow(3)
        self.mail_settings_page.show_outlook_settings()

    def _navigate(self, row: int) -> None:
        if row < 0:
            return
        if self.current_mode == "mail":
            if row < len(self.mail_pages):
                _label, page, section = self.mail_pages[row]
                self.stack.setCurrentWidget(page)
                if section is not None:
                    self.email_page.set_section(section)
            return
        if self.current_mode == "system":
            if row < len(self.system_pages):
                _label, page, section = self.system_pages[row]
                self.stack.setCurrentWidget(page)
                if section is not None:
                    self.system_page.set_section(section)
            return
        if row < len(self.file_pages):
            self.stack.setCurrentWidget(self.file_pages[row][1])

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
            self.set_mode("mail")
            self.navigation.setCurrentRow(2)
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

    def _remove_recent_workspace(self, path: str) -> None:
        recent = self.settings.value("recentWorkspaces", [], list)
        values = [value for value in recent if str(value) != path]
        self.settings.setValue("recentWorkspaces", values)
        self._refresh_recent_menu()

    def _open_recent(self, path: str) -> None:
        if not Path(path).is_file():
            answer = QMessageBox.question(
                self,
                "Recent workspace is unavailable",
                f"This workspace no longer exists:\n\n{path}\n\nRemove it from Open recent?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._remove_recent_workspace(path)
            return
        try:
            self.controller.open_workspace(Path(path))
            self._remember_workspace(path)
        except Exception as error:
            QMessageBox.critical(self, "Cannot open workspace", str(error))

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self.controller.close()
        super().closeEvent(event)
