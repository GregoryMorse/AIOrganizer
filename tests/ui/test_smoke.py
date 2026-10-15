from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from pypdf import PdfWriter
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

import ai_organizer.desktop.pages as pages_module
from ai_organizer.adapters.email import ClassicOutlookMailbox
from ai_organizer.desktop.guidance import GuidanceContextBar, GuidancePanel
from ai_organizer.desktop.main_window import MainWindow
from ai_organizer.desktop.pages import (
    AuditPage,
    DocumentRepairPage,
    FolderPlanPage,
    InventoryPage,
    SettingsPage,
    _audit_proposal_row,
    _outlook_auth_configuration,
)
from ai_organizer.desktop.preview import DocumentRepairPreview
from ai_organizer.domain.email import EmailAccount
from ai_organizer.domain.models import Evidence, FolderRole


@pytest.mark.ui
def test_main_window_navigation(qtbot) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    assert window.windowTitle() == "AIOrganizer"
    assert window.navigation.objectName() == "navigationList"
    assert window.navigation.count() == 14
    assert window.navigation.item(3).text() == "Audit"
    assert window.navigation.item(4).text() == "Updates"
    assert window.navigation.item(5).text() == "Document Repair"
    repair_page = window.stack.widget(5)
    assert isinstance(repair_page, DocumentRepairPage)
    assert isinstance(repair_page.preview, DocumentRepairPreview)
    assert repair_page.preview.tabs.count() == 7
    assert [repair_page.commit_choice.itemData(index) for index in range(3)] == [
        "ocr",
        "compression",
        "both",
    ]
    assert window.navigation.item(9).text() == "Cleanup"
    assert bool(window.navigation.item(9).flags() & window.navigation.item(9).flags().ItemIsEnabled)
    assert window.navigation.item(10).text() == "Recurrences"
    assert bool(
        window.navigation.item(10).flags() & window.navigation.item(10).flags().ItemIsEnabled
    )
    rename_page = window.stack.widget(6)
    assert len(rename_page.findChildren(GuidanceContextBar)) == 1
    assert not hasattr(rename_page.findChild(GuidanceContextBar), "content_kind")
    assert not rename_page.findChildren(GuidancePanel)
    settings_page = window.stack.widget(13)
    assert isinstance(settings_page, SettingsPage)
    assert settings_page.tabs.count() == 14
    assert len(settings_page.findChildren(GuidancePanel)) == 10
    assert "Updates" in [
        settings_page.tabs.tabText(index) for index in range(settings_page.tabs.count())
    ]
    assert "Sources & Categories" in [
        settings_page.tabs.tabText(index) for index in range(settings_page.tabs.count())
    ]
    assert "Privacy & Redaction" in [
        settings_page.tabs.tabText(index) for index in range(settings_page.tabs.count())
    ]
    assert "Outlook & Permissions" not in [
        settings_page.tabs.tabText(index) for index in range(settings_page.tabs.count())
    ]
    updates_panel = settings_page.guidance_panels["updates"]
    assert updates_panel.provider.findText("deepseek") >= 0
    assert updates_panel.provider.findText("openrouter") >= 0

    window.mail_mode_action.setChecked(True)
    assert window.navigation.count() == 4
    assert window.navigation.item(0).text() == "Folder Proposals"
    assert window.navigation.item(1).text() == "Rule Proposals"
    assert window.navigation.item(2).text() == "Focused Actions"
    assert window.navigation.item(3).text() == "Settings"
    assert window.stack.currentWidget() is window.email_page
    assert window.email_page.sections.count() == 3
    assert window.email_page.folder_inspector.tabs.count() == 4
    assert window.email_page.rule_inspector.tabs.count() == 4
    assert window.email_page.action_inspector.tabs.count() == 4
    assert [
        window.email_page.focused_action_filter.itemText(index)
        for index in range(window.email_page.focused_action_filter.count())
    ] == [
        "All focused findings",
        "Find registrations & accounts",
        "Find invoices & receipts",
        "Find forgotten / urgent tasks",
        "Find unsaved attachments",
        "Find renewals & subscriptions",
        "Find security & account changes",
    ]
    window.navigation.setCurrentRow(1)
    assert window.email_page.sections.currentIndex() == 1
    window.navigation.setCurrentRow(3)
    assert window.stack.currentWidget() is window.mail_settings_page
    assert window.mail_settings_page.property("settingsMode") == "mail"
    assert set(window.mail_settings_page.guidance_panels) == {
        "mail_folder",
        "mail_rule",
        "mail_action",
    }
    mail_settings_labels = [
        window.mail_settings_page.tabs.tabText(index)
        for index in range(window.mail_settings_page.tabs.count())
    ]
    assert {"General", "Accessibility & Language", "Privacy & Redaction", "Providers"} <= set(
        mail_settings_labels
    )
    assert "Outlook & Permissions" in mail_settings_labels
    assert "Rename" not in mail_settings_labels
    assert "Updates" not in mail_settings_labels

    window.system_mode_action.setChecked(True)
    assert window.navigation.count() == 5
    assert [window.navigation.item(index).text() for index in range(5)] == [
        "Applications",
        "Drivers",
        "Windows Update",
        "Health",
        "Settings",
    ]
    assert window.stack.currentWidget() is window.system_application_page
    window.navigation.setCurrentRow(3)
    assert window.system_page.sections.currentIndex() == 3
    assert window.system_page.fragmentation_button.isVisible()
    window.navigation.setCurrentRow(4)
    assert window.stack.currentWidget() is window.system_settings_page
    assert window.system_settings_page.property("settingsMode") == "system"
    assert set(window.system_settings_page.guidance_panels) == {
        "system_apps",
        "system_drivers",
        "system_os_updates",
        "system_health",
    }
    system_settings_labels = [
        window.system_settings_page.tabs.tabText(index)
        for index in range(window.system_settings_page.tabs.count())
    ]
    assert "Windows & Safety" in system_settings_labels
    assert "Outlook & Permissions" not in system_settings_labels
    assert "Rename" not in system_settings_labels

    window.file_mode_action.setChecked(True)
    assert window.navigation.count() == 14
    assert window.navigation.item(2).text() == "Inventory"


@pytest.mark.ui
@pytest.mark.parametrize(
    ("answer", "remaining"),
    [
        (QMessageBox.StandardButton.Yes, []),
        (QMessageBox.StandardButton.No, ["missing.aioworkspace"]),
    ],
)
def test_missing_recent_workspace_can_be_removed(
    qtbot, tmp_path, monkeypatch, answer, remaining
) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow()
    qtbot.addWidget(window)
    window.settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window.settings.setValue("recentWorkspaces", ["missing.aioworkspace"])
    window._refresh_recent_menu()
    monkeypatch.setattr(QMessageBox, "question", lambda *args: answer)

    window._open_recent("missing.aioworkspace")

    assert window.settings.value("recentWorkspaces", [], list) == remaining


@pytest.mark.ui
def test_outlook_binding_can_be_saved_in_mail_settings(qtbot, tmp_path) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow()
    qtbot.addWidget(window)
    page = window.mail_settings_page
    page.settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    page.refresh_outlook_binding()
    page.outlook_client_id.setText("11111111-2222-3333-4444-555555555555")
    page.outlook_tenant.setText("organizations")

    page.save_outlook_binding()

    client_id, authority, source = _outlook_auth_configuration(page.settings)
    assert client_id == "11111111-2222-3333-4444-555555555555"
    assert authority == "https://login.microsoftonline.com/organizations"
    assert source == "in-app settings"
    assert "Connect another Outlook mailbox" in page.status.text()

    page.clear_outlook_binding()

    assert page.settings.value("outlook/clientId") is None
    assert page.settings.value("outlook/tenant") is None


@pytest.mark.ui
def test_mailbox_menu_switches_between_configured_accounts(qtbot, tmp_path) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow()
    qtbot.addWidget(window)
    window.controller.create_workspace(tmp_path / "mailboxes.aioworkspace", "Mailboxes")
    assert window.controller.store is not None
    window.controller.store.save_email_account(
        EmailAccount("first@elte.example", "First ELTE", id="first")
    )
    window.controller.store.save_email_account(
        EmailAccount("second@elte.example", "Second ELTE", id="second")
    )

    window.set_mode("mail")
    window._refresh_mailbox_menu()

    assert window.mailbox_menu.menuAction().isVisible()
    actions = {action.text(): action for action in window.select_mailbox_menu.actions()}
    assert set(actions) == {
        "Second ELTE <second@elte.example> [Graph]",
        "First ELTE <first@elte.example> [Graph]",
    }
    assert actions["Second ELTE <second@elte.example> [Graph]"].isChecked()

    window.select_mailbox("first")

    active = [
        value for value in window.controller.store.list_email_accounts() if value["active"]
    ]
    assert [value["id"] for value in active] == ["first"]
    assert "first@elte.example" in window.windowTitle()
    assert window.mail_settings_page.outlook_accounts.topLevelItemCount() == 2

    window.set_mode("files")
    assert not window.mailbox_menu.menuAction().isVisible()


@pytest.mark.ui
def test_classic_outlook_connection_registers_local_mailboxes(
    qtbot, tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow()
    qtbot.addWidget(window)
    window.controller.create_workspace(tmp_path / "classic-mail.aioworkspace", "Classic Mail")

    class FakeClassicClient:
        def discover_mailboxes(self):  # type: ignore[no-untyped-def]
            return [ClassicOutlookMailbox("store-one", "person@example.com", "Person")]

    class FakeBackgroundTaskDialog:
        def __init__(self, _title, _message, operation, _parent) -> None:  # type: ignore[no-untyped-def]
            self.operation = operation
            self.result_value = None
            self.error_message = ""

        def run(self) -> int:
            self.result_value = self.operation()
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(pages_module, "ClassicOutlookClient", FakeClassicClient)
    monkeypatch.setattr(pages_module, "BackgroundTaskDialog", FakeBackgroundTaskDialog)
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: None)

    window.email_page.connect_classic_outlook()

    assert window.controller.store is not None
    accounts = window.controller.store.list_email_accounts()
    assert len(accounts) == 1
    assert accounts[0]["connector_kind"] == "classic_outlook"
    assert accounts[0]["connector_ref"] == "store-one"
    assert window.mail_settings_page.outlook_accounts.topLevelItem(0).text(3) == (
        "Classic Outlook (local)"
    )


@pytest.mark.ui
def test_microsoft_device_code_is_shown_before_browser_opens(qtbot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow()
    qtbot.addWidget(window)
    page = window.email_page
    events: list[str] = []
    prompt = SimpleNamespace(
        user_code="ABCD-EFGH",
        verification_uri="https://microsoft.com/devicelogin",
        verification_uri_complete="https://microsoft.com/devicelogin?otc=ABCD-EFGH",
        message="ignored",
    )

    class FakeAuth:
        def begin_device_flow(self, scopes):  # type: ignore[no-untyped-def]
            assert scopes == ("Mail.Read",)
            return prompt

        def complete_device_flow(self, received_prompt):  # type: ignore[no-untyped-def]
            assert received_prompt is prompt
            events.append("complete")
            return {"access_token": "token"}

        def cancel_device_flow(self, received_prompt) -> None:  # type: ignore[no-untyped-def]
            assert received_prompt is prompt

    class FakeBackgroundTaskDialog:
        def __init__(
            self, _title, _message, operation, _parent, *, cancel_operation=None
        ) -> None:  # type: ignore[no-untyped-def]
            self.operation = operation
            assert cancel_operation is not None
            self.result_value = None
            self.error_message = ""

        def run(self) -> int:
            self.result_value = self.operation()
            return QDialog.DialogCode.Accepted

    clipboard = QApplication.clipboard()
    clipboard.setText("previous clipboard")
    monkeypatch.setattr(page, "_device_auth", lambda: FakeAuth())
    monkeypatch.setattr(pages_module, "BackgroundTaskDialog", FakeBackgroundTaskDialog)

    def show_information(*_args) -> None:  # type: ignore[no-untyped-def]
        assert clipboard.text() == "ABCD-EFGH"
        events.append("information")

    def open_url(url) -> bool:  # type: ignore[no-untyped-def]
        assert "otc=ABCD-EFGH" in url.toString()
        events.append("open")
        return True

    monkeypatch.setattr(QMessageBox, "information", show_information)
    monkeypatch.setattr(QDesktopServices, "openUrl", open_url)

    _auth, result = page._interactive_token(("Mail.Read",))

    assert result == {"access_token": "token"}
    assert events == ["information", "open", "complete"]
    assert clipboard.text() == "previous clipboard"


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
    page = window.stack.widget(7)
    assert isinstance(page, FolderPlanPage)

    page.generate()

    assert page.model.rows
    assert {row["action"] for row in page.model.rows} <= {"create", "rename"}
    assert all("â" not in str(row["node"]) for row in page.model.rows)


@pytest.mark.ui
def test_inventory_focus_filters_multiple_sources_and_types(qtbot, tmp_path) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow()
    qtbot.addWidget(window)
    window.controller.create_workspace(tmp_path / "focus.aioworkspace", "Focus")
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    first_path.mkdir()
    second_path.mkdir()
    first = window.controller.add_source(first_path)
    second = window.controller.add_source(second_path)
    window.controller.items = [
        {
            "id": "pdf-first",
            "root_id": first.id,
            "relative_path": "documents/one.pdf",
            "mime_type": "application/pdf",
            "size": 1,
        },
        {
            "id": "image-first",
            "root_id": first.id,
            "relative_path": "images/one.png",
            "mime_type": "image/png",
            "size": 2,
        },
        {
            "id": "pdf-second",
            "root_id": second.id,
            "relative_path": "documents/two.pdf",
            "mime_type": "application/pdf",
            "size": 3,
        },
    ]
    page = window.stack.widget(2)
    assert isinstance(page, InventoryPage)

    page.focus.type_filter.setText("pdf")
    page.focus.sources.item(1).setCheckState(Qt.CheckState.Unchecked)

    assert [row["id"] for row in page.model.rows] == ["pdf-first"]
    assert page.focus.count.text() == "1 of 3 inventory item(s)"
    assert page.focus.sources.property("compactChecklist") is True
    assert page.focus.sources.sizeHintForRow(0) < window.navigation.sizeHintForRow(0)


def test_audit_ui_normalizes_provider_dictionary_proposals() -> None:
    row = _audit_proposal_row(
        {
            "target": "rename",
            "pattern": "Repeated statement names",
            "confidence": 0.8,
            "evidence": "Metadata summary",
            "guidance": "Prefer statement dates",
        },
        selected=True,
    )
    assert row["target_label"] == "Rename"
    assert row["confidence"] == "80%"
    assert row["guidance"] == "Prefer statement dates"


@pytest.mark.ui
def test_audit_preview_tracks_exact_proposal_identity_across_selection_and_sorting(
    qtbot, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow()
    qtbot.addWidget(window)
    window.controller.create_workspace(tmp_path / "audit-preview.aioworkspace", "Audit")
    page = window.stack.widget(3)
    assert isinstance(page, AuditPage)
    rows = [
        _audit_proposal_row(
            {
                "target": "rename",
                "pattern": f"Pattern {number}",
                "guidance": f"Exact guidance {number}",
                "confidence": confidence,
            },
            selected=True,
        )
        for number, confidence in ((1, 0.2), (2, 0.9), (3, 0.5))
    ]
    page.model.set_rows(rows)
    selected_id = rows[1]["_proposal_id"]
    page.table.setCurrentIndex(page.model.index(1, 0))
    page.table.selectRow(1)
    qtbot.wait(10)

    assert page.preview.property("proposalId") == selected_id
    assert "Exact guidance 2" in page.preview.toPlainText()

    page.model.sort(7, Qt.SortOrder.AscendingOrder)
    qtbot.wait(10)

    assert page.preview.property("proposalId") == selected_id
    assert page.model.row(page.table.currentIndex())["_proposal_id"] == selected_id
    assert "Exact guidance 2" in page.preview.toPlainText()


@pytest.mark.ui
def test_document_repair_preview_has_six_pdf_views_and_cached_ocr(qtbot, tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "document.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(path)
    preview = DocumentRepairPreview()
    qtbot.addWidget(preview)

    preview.show_path(path, extracted_text="Locally cached OCR text")

    visible = [
        preview.tabs.tabText(index)
        for index in range(preview.tabs.count())
        if preview.tabs.isTabVisible(index)
    ]
    assert visible == [
        "Preview",
        "Extracted / OCR Text",
        "Metadata",
        "Hex",
        "Proposed OCR Text",
        "Proposed Compression",
    ]
    assert preview.text.toPlainText() == "Locally cached OCR text"


@pytest.mark.ui
def test_rename_preview_reads_cached_ocr_without_running_extraction(qtbot, tmp_path) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow()
    qtbot.addWidget(window)
    source_path = tmp_path / "source"
    source_path.mkdir()
    pdf_path = source_path / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(pdf_path)
    window.controller.create_workspace(tmp_path / "rename-ocr.aioworkspace", "Rename OCR")
    source = window.controller.add_source(source_path, roles={FolderRole.INBOX})
    stat = pdf_path.stat()
    window.controller.items = [
        {
            "id": "item_scan",
            "root_id": source.id,
            "relative_path": "scan.pdf",
            "mime_type": "application/pdf",
            "size": stat.st_size,
            "modified_ns": stat.st_mtime_ns,
            "is_dir": False,
        }
    ]
    window.controller.store.save_evidence(
        Evidence(
            "item_scan",
            "pdf",
            "Locally cached OCR text",
            facts={"text": "Locally cached OCR text", "pages": ["Locally cached OCR text"]},
            content_classes=["extracted_text"],
        )
    )
    rename_page = window.stack.widget(6)
    rename_page.generate()
    rename_page.table.setCurrentIndex(rename_page.model.index(0, 0))

    assert rename_page.file_preview.text.toPlainText() == "Locally cached OCR text"


@pytest.mark.ui
def test_smoke_flag_starts_and_stops(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtWidgets import QApplication

    from ai_organizer.bootstrap.main import main

    application = QApplication.instance()
    assert application is not None
    monkeypatch.setattr("sys.argv", ["aiorganizer", "--smoke-test"])
    assert main() == 0
