from __future__ import annotations

import json
import os
import platform
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QModelIndex, QProcess, QSettings, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableView,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ai_organizer.adapters.email import GraphClient, MsalDeviceAuth, UrllibGraphTransport
from ai_organizer.adapters.extraction import default_registry
from ai_organizer.adapters.providers import (
    AnthropicProvider,
    CodexProvider,
    CodexRuntimeDetector,
    DeepSeekProvider,
    OpenAIProvider,
    OpenRouterProvider,
)
from ai_organizer.adapters.providers.base import detect_secret_kinds
from ai_organizer.adapters.secrets import SecretStore
from ai_organizer.application.email_service import EmailService
from ai_organizer.application.inventory_query import InventoryQueryService
from ai_organizer.application.update_research import PublicWebResearchClient
from ai_organizer.domain.actions import (
    ActionEngine,
    ActionFilter,
    ActionOutputMode,
    ActionPreset,
    ActionRun,
    FilterOperator,
    builtin_actions,
)
from ai_organizer.domain.email import (
    MAIL_WRITE_SCOPES,
    READ_SCOPES,
    RULE_WRITE_SCOPES,
    EmailProposal,
    EmailProposalKind,
)
from ai_organizer.domain.evidence import EvidenceClass
from ai_organizer.domain.hierarchy import HierarchyAction, HierarchyChange, UnionHierarchyPlanner
from ai_organizer.domain.models import (
    CategoryDefinition,
    CloudPolicy,
    FolderRole,
    ItemSnapshot,
    TagDefinition,
    TagFacet,
    new_id,
    utc_now,
)
from ai_organizer.domain.moves import MoveCandidate, ProjectedMoveValidator
from ai_organizer.domain.naming import NamingProfile, builtin_naming_profiles, disambiguate
from ai_organizer.domain.organization import FolderDepthPolicy, general_source_presets
from ai_organizer.domain.prompts import PromptCompiler, PromptLayerKind, PromptRevision
from ai_organizer.domain.recurrence import Cadence, GapStatus, recurrence_series_from_payload
from ai_organizer.domain.updates import (
    ReleaseChannel,
    UpdateAssessment,
    UpdatePageHint,
    extract_version_with_hint,
)

from .background_task import BackgroundTaskDialog
from .controller import WorkspaceController
from .guidance import GuidanceContextBar, GuidancePanel
from .inventory_scan import InventoryScanDialog
from .preferences import apply_runtime_preferences
from .preview import FilePreview
from .table_models import DictTableModel
from .table_views import (
    configure_data_table,
    configure_data_tree,
    install_table_context_menu,
    install_tree_context_menu,
)


def _archive_member_loader(
    store: Any, root_id: str, relative_path: str
) -> Callable[[int, int, str], dict[str, Any]]:
    def load(offset: int, limit: int, glob: str) -> dict[str, Any]:
        return store.list_archive_members(
            root_id,
            relative_path,
            offset=offset,
            limit=limit,
            glob=glob,
        )

    return load


def _run_inventory_scan(
    controller: WorkspaceController,
    parent: QWidget,
    *,
    revalidation: bool = False,
) -> None:
    if not controller.store:
        QMessageBox.warning(parent, "Workspace required", "Open a workspace first.")
        return
    if not controller.sources:
        QMessageBox.information(parent, "No sources", "Add at least one folder source first.")
        return
    sources = tuple(
        replace(
            source,
            roles=set(source.roles),
            category_ids=set(source.category_ids),
            tag_ids=set(source.tag_ids),
            exclusions=list(source.exclusions),
        )
        for source in controller.sources.values()
    )
    cached = controller.store.metadata_cache_records()
    dialog = InventoryScanDialog(sources, cached, controller.metadata_fingerprint_mode(), parent)
    dialog.start()
    if dialog.error_message:
        QMessageBox.critical(parent, "Inventory failed", dialog.error_message)
        return
    if dialog.result_value is None:
        return
    try:
        count = controller.apply_inventory_scan(dialog.result_value)
        title = "Metadata cache revalidated" if revalidation else "Inventory complete"
        QMessageBox.information(parent, title, f"Recorded {count:,} item(s).")
    except Exception as error:
        QMessageBox.critical(parent, "Inventory could not be saved", str(error))


class ReproposalDialog(QDialog):
    MAX_CHARS = 2_000

    def __init__(self, item_count: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI re-propose selected items")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        notice = QLabel(
            f"Explain what is wrong with the {item_count:,} selected proposal(s). "
            "This extra context applies only to this correction pass."
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText(
            "Example: These are client-deliverable PDFs, not build artifacts. Preserve the client code prefix."
        )
        self.prompt.textChanged.connect(self._limit_text)
        layout.addWidget(self.prompt)
        self.count = QLabel(f"0 / {self.MAX_CHARS}")
        layout.addWidget(self.count)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _limit_text(self) -> None:
        value = self.prompt.toPlainText()
        if len(value) > self.MAX_CHARS:
            cursor = self.prompt.textCursor()
            self.prompt.blockSignals(True)
            self.prompt.setPlainText(value[: self.MAX_CHARS])
            cursor.setPosition(self.MAX_CHARS)
            self.prompt.setTextCursor(cursor)
            self.prompt.blockSignals(False)
            value = value[: self.MAX_CHARS]
        self.count.setText(f"{len(value):,} / {self.MAX_CHARS:,}")

    def _accept_if_valid(self) -> None:
        if not self.prompt.toPlainText().strip():
            QMessageBox.information(
                self, "Correction needed", "Explain what the AI should reconsider."
            )
            return
        self.accept()

    def correction(self) -> str:
        return self.prompt.toPlainText().strip()


class OverviewPage(QWidget):
    def __init__(self, controller: WorkspaceController) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        title = QLabel("AIOrganizer")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        self.summary = QLabel("Create or open a workspace to begin.")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        safety = QLabel(
            "AI and MCP can revise proposals only. Every file change requires a fresh "
            "preflight and explicit desktop confirmation. Use copied data for this alpha."
        )
        safety.setWordWrap(True)
        safety.setObjectName("safetyBanner")
        layout.addWidget(safety)
        self.recovery = QPushButton("Recover interrupted operations…")
        self.recovery.clicked.connect(self.recover_interrupted)
        layout.addWidget(self.recovery)
        layout.addStretch()
        controller.workspace_changed.connect(self.refresh)
        controller.inventory_changed.connect(self.refresh)
        self.controller = controller

    def refresh(self) -> None:
        if not self.controller.store:
            self.summary.setText("Create or open a workspace to begin.")
            self.recovery.setEnabled(False)
            return
        incomplete = self.controller.store.incomplete_journals()
        self.recovery.setEnabled(bool(incomplete))
        self.recovery.setText(
            f"Recover {len(incomplete)} interrupted operation journal(s)…"
            if incomplete
            else "No interrupted operations"
        )
        self.summary.setText(
            f"Workspace: {self.controller.store.get_meta('name')}\n"
            f"Sources: {len(self.controller.sources)}\n"
            f"Inventory records: {len(self.controller.items)}"
        )

    def recover_interrupted(self) -> None:
        answer = QMessageBox.warning(
            self,
            "Recover interrupted operations",
            "AIOrganizer will inspect the persisted journal and observed filesystem state, then "
            "attempt a rollback to original paths. Changed or occupied paths stop recovery.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            count = self.controller.recover_incomplete_journals()
            QMessageBox.information(
                self, "Recovery complete", f"Recovered {count} interrupted journal(s)."
            )
            self.refresh()
        except Exception as error:
            QMessageBox.critical(self, "Recovery needs attention", str(error))


class SourcesCategoriesPage(QWidget):
    def __init__(self, controller: WorkspaceController) -> None:
        super().__init__()
        self.controller = controller
        layout = QVBoxLayout(self)
        title = QLabel("Sources & Categories")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        controls = QHBoxLayout()
        add_source = QPushButton("Add source…")
        add_source.clicked.connect(self.add_source)
        scan = QPushButton("Inventory all sources")
        scan.clicked.connect(self.scan)
        add_category = QPushButton("Add category…")
        add_category.clicked.connect(self.add_category)
        add_tag = QPushButton("Add tag…")
        add_tag.clicked.connect(self.add_tag)
        install_profile = QPushButton("Install/refresh general defaults…")
        install_profile.clicked.connect(self.install_general_profile)
        privacy = QPushButton("Edit selected source privacy…")
        privacy.clicked.connect(self.edit_source_privacy)
        assign = QPushButton("Assign selected folder policy…")
        assign.clicked.connect(self.assign_folder)
        controls.addWidget(add_source)
        controls.addWidget(scan)
        controls.addWidget(add_category)
        controls.addWidget(add_tag)
        controls.addWidget(install_profile)
        controls.addWidget(privacy)
        controls.addWidget(assign)
        controls.addStretch()
        layout.addLayout(controls)
        splitter = QSplitter()
        self.sources = QTreeWidget()
        self.sources.setHeaderLabels(
            ["Source/folder", "Categories", "Tags", "Roles", "Cloud", "Status"]
        )
        configure_data_tree(self.sources)
        self.categories = QTreeWidget()
        self.categories.setHeaderLabels(
            ["Category", "Default tags", "Folder template", "Sensitivity", "Cloud", "Max depth"]
        )
        configure_data_tree(self.categories)
        self.tags = QTreeWidget()
        self.tags.setHeaderLabels(["Tag", "Facet", "Description"])
        configure_data_tree(self.tags)
        splitter.addWidget(self.sources)
        splitter.addWidget(self.categories)
        splitter.addWidget(self.tags)
        self.sources.itemSelectionChanged.connect(self.record_source_selection)
        self.source_tabs = QTabWidget()
        folder_tab = QWidget()
        folder_layout = QVBoxLayout(folder_tab)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        folder_layout.addWidget(splitter)
        self.source_tabs.addTab(folder_tab, "Folders")

        email_tab = QWidget()
        email_layout = QVBoxLayout(email_tab)
        email_controls = QHBoxLayout()
        add_outlook = QPushButton("Register Outlook source…")
        add_outlook.clicked.connect(self.add_outlook_source)
        email_controls.addWidget(add_outlook)
        email_controls.addStretch()
        email_layout.addLayout(email_controls)
        self.email_sources = QTreeWidget()
        self.email_sources.setHeaderLabels(["Email source", "Kind", "Status"])
        configure_data_tree(self.email_sources)
        email_layout.addWidget(self.email_sources)
        email_layout.addWidget(
            QLabel(
                "Email sources have separate message/folder identities and will never be mixed "
                "with filesystem proposals. Registration does not grant access or store credentials."
            )
        )
        self.source_tabs.addTab(email_tab, "Email")

        software_tab = QWidget()
        software_layout = QVBoxLayout(software_tab)
        self.software_source_status = QLabel()
        self.software_source_status.setWordWrap(True)
        software_layout.addWidget(self.software_source_status)
        refresh_software = QPushButton("Refresh local software inventory")
        refresh_software.clicked.connect(self.refresh_software_inventory)
        software_layout.addWidget(refresh_software)
        software_layout.addStretch()
        self.source_tabs.addTab(software_tab, "Software")
        layout.addWidget(self.source_tabs, 1)
        note = QLabel(
            "Assignments inherit into descendants. AI suggestions remain inactive until approved. "
            "Overlapping roots are rejected to prevent duplicate identities."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        controller.workspace_changed.connect(self.refresh)
        controller.software_changed.connect(self.refresh)
        self.refresh()

    def add_outlook_source(self) -> None:
        if not self.controller.store:
            QMessageBox.warning(self, "Workspace required", "Open a workspace first.")
            return
        name, accepted = QInputDialog.getText(
            self, "Register Outlook source", "Account or source label"
        )
        if not accepted or not name.strip():
            return
        self.controller.store.save_connector_source(
            new_id("connector"),
            "outlook",
            name.strip(),
            {"authorization": "not_configured", "content_kind": "email"},
            False,
        )
        self.refresh()

    def refresh_software_inventory(self) -> None:
        try:
            count = self.controller.refresh_software_inventory()
            QMessageBox.information(
                self, "Software inventory refreshed", f"Recorded {count} installed application(s)."
            )
        except Exception as error:
            QMessageBox.critical(self, "Software inventory failed", str(error))

    def record_source_selection(self) -> None:
        roots: set[str] = set()
        folders: set[tuple[str, str]] = set()
        for item in self.sources.selectedItems():
            root_id = str(item.data(0, Qt.ItemDataRole.UserRole) or "")
            relative = str(item.data(0, Qt.ItemDataRole.UserRole + 1) or "")
            if not root_id:
                continue
            roots.add(root_id)
            if relative:
                folders.add((root_id, relative))
        self.controller.selected_root_ids = roots
        self.controller.selected_folders = folders

    def add_source(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Add source root")
        if not folder:
            return
        categories = self.controller.store.list_category_payloads() if self.controller.store else []
        tags = self.controller.store.list_tag_definition_payloads() if self.controller.store else []
        options = SourceOptionsDialog(categories, tags, self)
        if options.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.controller.add_source(
                Path(folder),
                options.roles(),
                options.cloud_policy(),
                options.category_ids(),
                tag_ids=options.tag_ids(),
                exclusions=options.exclusions(),
                max_hierarchy_depth=options.max_hierarchy_depth(),
            )
        except Exception as error:
            QMessageBox.critical(self, "Cannot add source", str(error))

    def scan(self) -> None:
        _run_inventory_scan(self.controller, self)

    def edit_source_privacy(self) -> None:
        selected = self.sources.currentItem()
        root_id = str(selected.data(0, Qt.ItemDataRole.UserRole) or "") if selected else ""
        source = self.controller.sources.get(root_id)
        if source is None:
            QMessageBox.information(self, "Select a source", "Select a source or its child folder.")
            return
        policies = [
            CloudPolicy.LOCAL_ONLY.value,
            CloudPolicy.METADATA_ONLY.value,
            CloudPolicy.CLOUD_TEXT.value,
            CloudPolicy.TEXT_AND_IMAGES.value,
        ]
        policy, accepted = QInputDialog.getItem(
            self,
            "Provider privacy policy",
            "Maximum content this source may send to a cloud provider",
            policies,
            max(0, policies.index(source.cloud_policy.value))
            if source.cloud_policy.value in policies
            else 0,
            False,
        )
        if not accepted:
            return
        try:
            self.controller.set_source_cloud_policy(root_id, CloudPolicy(policy))
            self.refresh()
        except Exception as error:
            QMessageBox.critical(self, "Privacy policy not saved", str(error))

    def add_category(self) -> None:
        if not self.controller.store:
            QMessageBox.warning(self, "Workspace required", "Open a workspace first.")
            return
        dialog = CategoryDialog(
            self.controller.store.list_category_payloads(),
            self.controller.store.list_tag_definition_payloads(),
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        category = dialog.category()
        self.controller.store.save_category(category)
        self.controller.store.mark_proposals_stale(
            {"folder", "move", "finding"}, "Category policy revision changed"
        )
        self.controller.store.activity("category.created", f"Created category {category.name}")
        self.refresh()

    def add_tag(self) -> None:
        if not self.controller.store:
            QMessageBox.warning(self, "Workspace required", "Open a workspace first.")
            return
        dialog = TagDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        tag = dialog.tag()
        if not tag.name:
            return
        self.controller.store.save_tag_definition(tag)
        self.controller.store.mark_proposals_stale(
            {"folder", "move", "finding"}, "Tag vocabulary revision changed"
        )
        self.controller.store.activity("tag.created", f"Created tag {tag.name}")
        self.refresh()

    def install_general_profile(self) -> None:
        if not self.controller.store:
            QMessageBox.warning(self, "Workspace required", "Open a workspace first.")
            return
        answer = QMessageBox.question(
            self,
            "Install general organization defaults",
            "Add missing general-purpose categories and facet tags, mark matching categories as "
            "folder templates, and set folder depth to preferred 2 / maximum 3?\n\n"
            "Existing user-created categories, tags, assignments, and files are not deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            categories, tags = self.controller.install_general_organization_profile()
            self.controller.workspace_changed.emit()
            QMessageBox.information(
                self,
                "Organization defaults ready",
                f"Added {categories} missing category definition(s) and {tags} missing tag definition(s).",
            )
            self.refresh()
        except Exception as error:
            QMessageBox.critical(self, "Defaults not installed", str(error))

    def assign_folder(self) -> None:
        if not self.controller.store:
            QMessageBox.warning(self, "Workspace required", "Open a workspace first.")
            return
        selected = self.sources.currentItem()
        if not selected:
            QMessageBox.information(self, "Select a folder", "Select a source or folder first.")
            return
        root_id = selected.data(0, Qt.ItemDataRole.UserRole)
        relative = selected.data(0, Qt.ItemDataRole.UserRole + 1) or ""
        source = self.controller.sources.get(str(root_id))
        if not source:
            return
        dialog = AssignmentDialog(
            self.controller.store.list_category_payloads(),
            self.controller.store.list_tag_definition_payloads(),
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.controller.assign_folder_policy(
            str(root_id),
            str(relative),
            dialog.category_ids(),
            dialog.roles(),
            dialog.tag_ids(),
            override_roles=dialog.override_roles.isChecked(),
        )
        self.refresh()

    def refresh(self) -> None:
        self.sources.clear()
        self.email_sources.clear()
        self.software_source_status.setText(
            f"Local {platform.system()} software inventory — "
            f"{len(self.controller.software_packages)} installed application(s). "
            "Package records are consumed only by Updates."
        )
        category_payloads = (
            self.controller.store.list_category_payloads() if self.controller.store else []
        )
        category_names = {value["id"]: value["name"] for value in category_payloads}
        tag_payloads = (
            self.controller.store.list_tag_definition_payloads() if self.controller.store else []
        )
        tag_names = {value["id"]: value["name"] for value in tag_payloads}
        category_default_tags = {
            value["id"]: set(value.get("default_tag_ids", [])) for value in category_payloads
        }
        assignments = (
            self.controller.store.list_assignment_payloads() if self.controller.store else []
        )
        for source in self.controller.sources.values():
            capabilities = source.capabilities
            source_tag_ids = set(source.tag_ids)
            for category_id in source.category_ids:
                source_tag_ids.update(category_default_tags.get(category_id, set()))
            root_item = QTreeWidgetItem(
                [
                    str(source.path),
                    ", ".join(category_names.get(value, value) for value in source.category_ids),
                    ", ".join(tag_names.get(value, value) for value in source_tag_ids),
                    ", ".join(sorted(role.value for role in source.roles)),
                    source.cloud_policy.value,
                    "Ready" if capabilities and capabilities.reachable else "Unavailable",
                ]
            )
            root_item.setData(0, Qt.ItemDataRole.UserRole, source.id)
            root_item.setData(0, Qt.ItemDataRole.UserRole + 1, "")
            self.sources.addTopLevelItem(root_item)
            nodes = {"": root_item}
            folders = sorted(
                (
                    item
                    for item in self.controller.items
                    if item["root_id"] == source.id and item.get("is_dir")
                ),
                key=lambda value: (len(Path(value["relative_path"]).parts), value["relative_path"]),
            )
            for folder in folders:
                relative = str(folder["relative_path"])
                parent_relative = Path(relative).parent.as_posix()
                if parent_relative == ".":
                    parent_relative = ""
                category_ids = set(source.category_ids)
                tag_ids = set(source_tag_ids)
                roles = set(source.roles)
                resolved = (source.path / relative).resolve(strict=False)
                for assignment in assignments:
                    assignment_path = Path(assignment["path"]).resolve(strict=False)
                    if assignment_path == resolved or assignment_path in resolved.parents:
                        category_ids.update(assignment.get("category_ids", []))
                        tag_ids.update(assignment.get("tag_ids", []))
                        for category_id in assignment.get("category_ids", []):
                            tag_ids.update(category_default_tags.get(category_id, set()))
                        assigned_roles = {
                            FolderRole(value) for value in assignment.get("roles", [])
                        }
                        if assignment.get("override_roles"):
                            roles = assigned_roles
                        else:
                            roles.update(assigned_roles)
                node = QTreeWidgetItem(
                    [
                        Path(relative).name,
                        ", ".join(category_names.get(value, value) for value in category_ids),
                        ", ".join(tag_names.get(value, value) for value in tag_ids),
                        ", ".join(sorted(role.value for role in roles)),
                        source.cloud_policy.value,
                        "Project bundle" if folder.get("is_project_root") else "Inherited",
                    ]
                )
                node.setData(0, Qt.ItemDataRole.UserRole, source.id)
                node.setData(0, Qt.ItemDataRole.UserRole + 1, relative)
                nodes.get(parent_relative, root_item).addChild(node)
                nodes[relative] = node
            root_item.setExpanded(True)
        self.categories.clear()
        if not self.controller.store:
            return
        payloads = category_payloads
        nodes: dict[str, QTreeWidgetItem] = {}
        for payload in payloads:
            nodes[payload["id"]] = QTreeWidgetItem(
                [
                    payload["name"],
                    ", ".join(
                        tag_names.get(value, value) for value in payload.get("default_tag_ids", [])
                    ),
                    "Yes" if payload.get("suggest_as_folder") else "No",
                    payload["sensitivity"],
                    payload["cloud_policy"],
                    str(payload["max_hierarchy_depth"]),
                ]
            )
        for payload in payloads:
            node = nodes[payload["id"]]
            parent = nodes.get(payload.get("parent_id"))
            if parent:
                parent.addChild(node)
            else:
                self.categories.addTopLevelItem(node)
        self.categories.expandAll()
        self.tags.clear()
        facet_nodes: dict[str, QTreeWidgetItem] = {}
        for payload in tag_payloads:
            facet = str(payload["facet"])
            parent = facet_nodes.get(facet)
            if parent is None:
                parent = QTreeWidgetItem([facet.title(), facet, ""])
                facet_nodes[facet] = parent
                self.tags.addTopLevelItem(parent)
            parent.addChild(
                QTreeWidgetItem([payload["name"], facet, str(payload.get("description", ""))])
            )
        self.tags.expandAll()
        connectors = self.controller.store.list_connector_sources()
        for connector in connectors:
            self.email_sources.addTopLevelItem(
                QTreeWidgetItem(
                    [
                        connector["display_name"],
                        connector["kind"],
                        "Enabled" if connector["enabled"] else "Registered; authorization pending",
                    ]
                )
            )


class SourceOptionsDialog(QDialog):
    def __init__(
        self,
        categories: list[dict[str, Any]],
        tags: list[dict[str, Any]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Source policy")
        form = QFormLayout(self)
        self.presets = QComboBox()
        self.presets.addItem("Custom", None)
        for preset in general_source_presets():
            self.presets.addItem(preset.name, preset)
        self.preset_note = QLabel("Choose a reusable preset or configure the policy directly.")
        self.preset_note.setWordWrap(True)
        self.role_checks: dict[str, QCheckBox] = {}
        role_box = QWidget()
        role_layout = QVBoxLayout(role_box)
        for role in ["inbox", "downloads", "destination", "archive", "protected", "excluded"]:
            check = QCheckBox(role.title())
            check.setChecked(role == "inbox")
            self.role_checks[role] = check
            role_layout.addWidget(check)
        self.cloud = QComboBox()
        self.cloud.addItems(["local_only", "metadata_only", "cloud_text", "text_and_images"])
        self.categories = QListWidget()
        self.categories.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        for category in categories:
            item = QListWidgetItem(category["name"])
            item.setData(Qt.ItemDataRole.UserRole, category["id"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.categories.addItem(item)
        self.tags = QListWidget()
        for tag in tags:
            item = QListWidgetItem(f"{tag['name']}  [{tag['facet']}]")
            item.setData(Qt.ItemDataRole.UserRole, tag["id"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.tags.addItem(item)
        self.depth = QSpinBox()
        self.depth.setRange(0, 12)
        self.depth.setSpecialValueText("Use workspace default")
        self.exclusion_patterns = QLineEdit()
        self.exclusion_patterns.setPlaceholderText("e.g. nested-root/**; cache/**")
        form.addRow("Source profile", self.presets)
        form.addRow(self.preset_note)
        form.addRow("Operational roles", role_box)
        form.addRow("Categories", self.categories)
        form.addRow("Inherited tags", self.tags)
        form.addRow("Folder depth override", self.depth)
        form.addRow("Cloud processing", self.cloud)
        form.addRow("Excluded patterns", self.exclusion_patterns)
        warning = QLabel(
            "Cloud use is disabled by default. Category policy may further restrict this source."
        )
        warning.setWordWrap(True)
        form.addRow(warning)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self._category_ids_by_key = {
            str(value.get("semantic_key", "")): str(value["id"]) for value in categories
        }
        self._tag_ids_by_key = {str(value.get("key", "")): str(value["id"]) for value in tags}
        self.presets.currentIndexChanged.connect(self.apply_preset)

    def apply_preset(self) -> None:
        preset = self.presets.currentData()
        if preset is None:
            self.preset_note.setText("Choose a reusable preset or configure the policy directly.")
            return
        for name, check in self.role_checks.items():
            check.setChecked(FolderRole(name) in preset.roles)
        wanted_categories = {
            self._category_ids_by_key[key]
            for key in preset.category_keys
            if key in self._category_ids_by_key
        }
        for index in range(self.categories.count()):
            item = self.categories.item(index)
            item.setCheckState(
                Qt.CheckState.Checked
                if str(item.data(Qt.ItemDataRole.UserRole)) in wanted_categories
                else Qt.CheckState.Unchecked
            )
        wanted_tags = {
            self._tag_ids_by_key[key] for key in preset.tag_keys if key in self._tag_ids_by_key
        }
        for index in range(self.tags.count()):
            item = self.tags.item(index)
            item.setCheckState(
                Qt.CheckState.Checked
                if str(item.data(Qt.ItemDataRole.UserRole)) in wanted_tags
                else Qt.CheckState.Unchecked
            )
        cloud_index = self.cloud.findText(preset.cloud_policy.value)
        self.cloud.setCurrentIndex(max(0, cloud_index))
        self.depth.setValue(preset.max_hierarchy_depth or 0)
        self.exclusion_patterns.setText("; ".join(preset.exclusions))
        self.preset_note.setText(preset.description)

    def roles(self) -> set[FolderRole]:
        return {FolderRole(name) for name, check in self.role_checks.items() if check.isChecked()}

    def cloud_policy(self) -> CloudPolicy:
        return CloudPolicy(self.cloud.currentText())

    def category_ids(self) -> set[str]:
        return {
            str(self.categories.item(index).data(Qt.ItemDataRole.UserRole))
            for index in range(self.categories.count())
            if self.categories.item(index).checkState() == Qt.CheckState.Checked
        }

    def exclusions(self) -> list[str]:
        return [
            value.strip() for value in self.exclusion_patterns.text().split(";") if value.strip()
        ]

    def tag_ids(self) -> set[str]:
        return {
            str(self.tags.item(index).data(Qt.ItemDataRole.UserRole))
            for index in range(self.tags.count())
            if self.tags.item(index).checkState() == Qt.CheckState.Checked
        }

    def max_hierarchy_depth(self) -> int | None:
        return self.depth.value() or None


class AssignmentDialog(QDialog):
    def __init__(
        self,
        categories: list[dict[str, Any]],
        tags: list[dict[str, Any]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Approved folder assignment")
        form = QFormLayout(self)
        self.categories = QListWidget()
        self.categories.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        for category in categories:
            item = QListWidgetItem(category["name"])
            item.setData(Qt.ItemDataRole.UserRole, category["id"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.categories.addItem(item)
        self.tags = QListWidget()
        for tag in tags:
            item = QListWidgetItem(f"{tag['name']}  [{tag['facet']}]")
            item.setData(Qt.ItemDataRole.UserRole, tag["id"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.tags.addItem(item)
        role_box = QWidget()
        role_layout = QVBoxLayout(role_box)
        self.role_checks: dict[str, QCheckBox] = {}
        for role in ["inbox", "downloads", "destination", "archive", "protected", "excluded"]:
            check = QCheckBox(role.title())
            self.role_checks[role] = check
            role_layout.addWidget(check)
        self.override_roles = QCheckBox("Replace inherited routing roles")
        form.addRow("Add categories", self.categories)
        form.addRow("Add inherited tags", self.tags)
        form.addRow("Routing roles", role_box)
        form.addRow(self.override_roles)
        note = QLabel(
            "This is an explicit user-approved assignment. AI suggestions use a separate inactive state."
        )
        note.setWordWrap(True)
        form.addRow(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def category_ids(self) -> set[str]:
        return {
            str(self.categories.item(index).data(Qt.ItemDataRole.UserRole))
            for index in range(self.categories.count())
            if self.categories.item(index).checkState() == Qt.CheckState.Checked
        }

    def roles(self) -> set[FolderRole]:
        return {FolderRole(name) for name, check in self.role_checks.items() if check.isChecked()}

    def tag_ids(self) -> set[str]:
        return {
            str(self.tags.item(index).data(Qt.ItemDataRole.UserRole))
            for index in range(self.tags.count())
            if self.tags.item(index).checkState() == Qt.CheckState.Checked
        }


class CategoryDialog(QDialog):
    def __init__(
        self,
        categories: list[dict[str, Any]],
        tags: list[dict[str, Any]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("New category")
        layout = QFormLayout(self)
        self.name = QLineEdit()
        self.semantic_key = QLineEdit()
        self.semantic_key.setPlaceholderText("stable-key, e.g. legal.client-records")
        self.description = QLineEdit()
        self.guidance = QLineEdit()
        self.parent_category = QComboBox()
        self.parent_category.addItem("(top level)", None)
        for category in categories:
            self.parent_category.addItem(category["name"], category["id"])
        self.sensitivity = QComboBox()
        self.sensitivity.addItems(["normal", "confidential", "restricted"])
        self.cloud = QComboBox()
        self.cloud.addItems(
            ["inherit", "local_only", "metadata_only", "cloud_text", "text_and_images"]
        )
        self.depth = QSpinBox()
        self.depth.setRange(1, 12)
        self.depth.setValue(3)
        self.tags = QListWidget()
        for tag in tags:
            item = QListWidgetItem(f"{tag['name']}  [{tag['facet']}]")
            item.setData(Qt.ItemDataRole.UserRole, tag["id"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.tags.addItem(item)
        self.folder_template = QCheckBox(
            "Offer this category as a physical folder under an assigned parent category"
        )
        layout.addRow("Name", self.name)
        layout.addRow("Stable semantic key", self.semantic_key)
        layout.addRow("Description", self.description)
        layout.addRow("AI guidance", self.guidance)
        layout.addRow("Parent category", self.parent_category)
        layout.addRow("Sensitivity", self.sensitivity)
        layout.addRow("Cloud policy", self.cloud)
        layout.addRow("Maximum hierarchy depth", self.depth)
        layout.addRow("Default tags", self.tags)
        layout.addRow(self.folder_template)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def category(self) -> CategoryDefinition:
        from ai_organizer.domain.models import Sensitivity

        return CategoryDefinition(
            name=self.name.text().strip(),
            semantic_key=(
                self.semantic_key.text().strip().casefold().replace(" ", "-")
                or self.name.text().strip().casefold().replace(" ", "-")
            ),
            description=self.description.text().strip(),
            guidance=self.guidance.text().strip(),
            parent_id=self.parent_category.currentData(),
            sensitivity=Sensitivity(self.sensitivity.currentText()),
            cloud_policy=CloudPolicy(self.cloud.currentText()),
            max_hierarchy_depth=self.depth.value(),
            default_tag_ids={
                str(self.tags.item(index).data(Qt.ItemDataRole.UserRole))
                for index in range(self.tags.count())
                if self.tags.item(index).checkState() == Qt.CheckState.Checked
            },
            suggest_as_folder=self.folder_template.isChecked(),
        )


class TagDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New tag")
        form = QFormLayout(self)
        self.name = QLineEdit()
        self.key = QLineEdit()
        self.key.setPlaceholderText("stable-key, e.g. legal-review")
        self.facet = QComboBox()
        for facet in TagFacet:
            self.facet.addItem(facet.value.title(), facet.value)
        self.description = QLineEdit()
        self.guidance = QLineEdit()
        form.addRow("Name", self.name)
        form.addRow("Stable key", self.key)
        form.addRow("Facet", self.facet)
        form.addRow("Description", self.description)
        form.addRow("AI guidance", self.guidance)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def tag(self) -> TagDefinition:
        name = self.name.text().strip()
        key = self.key.text().strip().casefold().replace(" ", "-")
        return TagDefinition(
            name=name,
            key=key or name.casefold().replace(" ", "-"),
            facet=TagFacet(str(self.facet.currentData())),
            description=self.description.text().strip(),
            guidance=self.guidance.text().strip(),
        )


class TagPickerDialog(QDialog):
    def __init__(
        self,
        title: str,
        tags: list[dict[str, Any]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        self.tags = QListWidget()
        for tag in tags:
            item = QListWidgetItem(f"{tag['name']}  [{tag['facet']}]")
            item.setData(Qt.ItemDataRole.UserRole, tag["id"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.tags.addItem(item)
        layout.addWidget(self.tags)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_tag_ids(self) -> set[str]:
        return {
            str(self.tags.item(index).data(Qt.ItemDataRole.UserRole))
            for index in range(self.tags.count())
            if self.tags.item(index).checkState() == Qt.CheckState.Checked
        }


class NamingWizardDialog(QDialog):
    def __init__(self, original: NamingProfile, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.original = original
        self.setWindowTitle(f"Naming wizard — {original.name}")
        form = QFormLayout(self)
        self.template = QLineEdit(original.template)
        self.separator = QLineEdit(original.separator)
        self.date_format = QLineEdit(original.date_format)
        self.case_style = QComboBox()
        self.case_style.addItems(["preserve", "lower", "upper", "title", "snake", "kebab"])
        self.case_style.setCurrentText(original.case_style)
        self.unicode_form = QComboBox()
        self.unicode_form.addItems(["NFC", "NFKC", "NFD", "NFKD"])
        self.unicode_form.setCurrentText(original.unicode_form)
        self.maximum = QSpinBox()
        self.maximum.setRange(32, 240)
        self.maximum.setValue(original.max_component_length)
        self.aliases = QLineEdit(
            "; ".join(f"{key}={value}" for key, value in original.aliases.items())
        )
        self.abbreviations = QLineEdit(
            "; ".join(f"{key}={value}" for key, value in original.abbreviations.items())
        )
        self.collision = QLineEdit(original.collision_suffix)
        form.addRow("Token order/template", self.template)
        form.addRow("Separator", self.separator)
        form.addRow("Date format", self.date_format)
        form.addRow("Case", self.case_style)
        form.addRow("Unicode normalization", self.unicode_form)
        form.addRow("Maximum component length", self.maximum)
        form.addRow("Aliases (from=to; …)", self.aliases)
        form.addRow("Abbreviations (from=to; …)", self.abbreviations)
        form.addRow("Collision suffix", self.collision)
        note = QLabel(
            "Unknown or optional tokens are omitted. Values are never invented to fill a template."
        )
        note.setWordWrap(True)
        form.addRow(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def profile(self) -> NamingProfile:
        return replace(
            self.original,
            template=self.template.text(),
            separator=self.separator.text(),
            date_format=self.date_format.text(),
            case_style=self.case_style.currentText(),
            unicode_form=self.unicode_form.currentText(),
            max_component_length=self.maximum.value(),
            aliases=_parse_mapping(self.aliases.text()),
            abbreviations=_parse_mapping(self.abbreviations.text()),
            collision_suffix=self.collision.text(),
            revision=self.original.revision + 1,
        )


class InventoryPage(QWidget):
    def __init__(self, controller: WorkspaceController) -> None:
        super().__init__()
        self.controller = controller
        layout = QVBoxLayout(self)
        title = QLabel("Inventory")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        controls = QHBoxLayout()
        hydrate = QPushButton("Hydrate selected cloud files…")
        hydrate.clicked.connect(self.hydrate_selected)
        rebuild = QPushButton("Revalidate metadata cache")
        rebuild.clicked.connect(self.revalidate_metadata_cache)
        scope = QPushButton("Create MCP evidence scope")
        scope.clicked.connect(self.create_mcp_scope)
        tag_selected = QPushButton("Tag selected…")
        tag_selected.clicked.connect(self.tag_selected)
        controls.addWidget(hydrate)
        controls.addWidget(rebuild)
        controls.addWidget(scope)
        controls.addWidget(tag_selected)
        self.cache_status = QLabel()
        controls.addWidget(self.cache_status)
        controls.addStretch()
        layout.addLayout(controls)
        splitter = QSplitter()
        self.model = DictTableModel(
            [
                ("relative_path", "Path"),
                ("mime_type", "Type"),
                ("size", "Bytes"),
                ("is_placeholder", "Placeholder"),
                ("is_project_root", "Project bundle"),
                ("tags", "Tags"),
            ]
        )
        self.table = QTableView()
        self.table.setModel(self.model)
        configure_data_table(self.table)
        self.table.selectionModel().currentChanged.connect(self.preview)
        self.table.selectionModel().selectionChanged.connect(self.record_selection)
        install_table_context_menu(self.table, self._inventory_context_actions)
        splitter.addWidget(self.table)
        self.file_preview = FilePreview()
        splitter.addWidget(self.file_preview)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)
        controller.inventory_changed.connect(self.refresh)

    def refresh(self) -> None:
        tag_payloads = (
            self.controller.store.list_tag_definition_payloads() if self.controller.store else []
        )
        tag_names = {str(value["id"]): str(value["name"]) for value in tag_payloads}
        assignments = (
            self.controller.store.list_tag_assignment_payloads("inventory")
            if self.controller.store
            else []
        )
        tags_by_item: dict[str, list[str]] = {}
        for assignment in assignments:
            tags_by_item.setdefault(str(assignment["entity_key"]), []).append(
                tag_names.get(str(assignment["tag_id"]), str(assignment["tag_id"]))
            )
        self.model.set_rows(
            [
                {
                    **value,
                    "tags": ", ".join(
                        sorted(tags_by_item.get(self.controller.inventory_tag_key(value), []))
                    ),
                }
                for value in self.controller.items
            ]
        )
        stats = self.controller.metadata_cache_stats()
        self.cache_status.setText(
            f"Metadata store: {stats['records']} records validated by size/modified time; "
            f"{stats.get('archive_members', 0):,} archive members; "
        )

    def revalidate_metadata_cache(self) -> None:
        _run_inventory_scan(self.controller, self, revalidation=True)

    def _inventory_context_actions(self, rows: list[dict[str, Any]]) -> list[tuple[str, Any]]:
        return [
            (f"Assign tags to {len(rows):,} selected item(s)…", self.tag_selected),
            (f"Remove tags from {len(rows):,} selected item(s)…", self.remove_tags_selected),
        ]

    def tag_selected(self) -> None:
        if not self.controller.store or not self.controller.selected_item_ids:
            QMessageBox.information(self, "Select inventory", "Select one or more inventory rows.")
            return
        dialog = TagPickerDialog(
            "Assign approved tags",
            self.controller.store.list_tag_definition_payloads(),
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        tag_ids = dialog.selected_tag_ids()
        if tag_ids:
            self.controller.assign_item_tags(set(self.controller.selected_item_ids), tag_ids)

    def remove_tags_selected(self) -> None:
        if not self.controller.store or not self.controller.selected_item_ids:
            QMessageBox.information(self, "Select inventory", "Select one or more inventory rows.")
            return
        selected_keys = {
            self.controller.inventory_tag_key(item)
            for item in self.controller.items
            if str(item["id"]) in self.controller.selected_item_ids
        }
        assigned_ids = {
            str(value["tag_id"])
            for value in self.controller.store.list_tag_assignment_payloads("inventory")
            if str(value["entity_key"]) in selected_keys
        }
        tags = [
            value
            for value in self.controller.store.list_tag_definition_payloads()
            if str(value["id"]) in assigned_ids
        ]
        if not tags:
            QMessageBox.information(self, "No tags", "The selected rows have no direct tags.")
            return
        dialog = TagPickerDialog("Remove approved tags", tags, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        tag_ids = dialog.selected_tag_ids()
        if tag_ids:
            self.controller.remove_item_tags(set(self.controller.selected_item_ids), tag_ids)

    def record_selection(self) -> None:
        self.controller.selected_item_ids = {
            str(row["id"])
            for index in self.table.selectionModel().selectedRows()
            if (row := self.model.row(index))
        }

    def hydrate_selected(self) -> None:
        item_ids = {
            str(row["id"])
            for index in self.table.selectionModel().selectedRows()
            if (row := self.model.row(index))
        }
        if not item_ids:
            QMessageBox.information(self, "Nothing selected", "Select placeholder files first.")
            return
        answer = QMessageBox.warning(
            self,
            "Explicit cloud hydration",
            "Download the selected cloud-only files through their synchronized provider?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            count = self.controller.hydrate_selected(item_ids)
            QMessageBox.information(self, "Hydration complete", f"Hydrated {count} file(s).")
        except Exception as error:
            QMessageBox.critical(self, "Hydration failed", str(error))

    def create_mcp_scope(self) -> None:
        item_ids = [
            str(row["id"])
            for index in self.table.selectionModel().selectedRows()
            if (row := self.model.row(index)) and not row.get("is_dir")
        ][:250]
        if not item_ids:
            QMessageBox.information(
                self, "Nothing selected", "Select up to 250 inventory files first."
            )
            return
        try:
            scope = self.controller.create_selection_scope(item_ids)
            QMessageBox.information(
                self,
                "MCP evidence scope ready",
                f"Scope {scope.id} contains {len(scope.item_ids):,} opaque item IDs and expires "
                f"at {scope.expires_at}. The AI can now request cached or on-demand bounded "
                "text/OCR evidence and individual PDF page images for these items only.",
            )
        except Exception as error:
            QMessageBox.critical(self, "MCP scope not created", str(error))

    def preview(self, current: QModelIndex, previous: QModelIndex) -> None:
        row = self.model.row(current)
        if not row:
            return
        source = self.controller.sources.get(row["root_id"])
        if not source:
            return
        path = source.path / row["relative_path"]
        metadata = row.get("metadata", {})
        if self.controller.store and metadata.get("archive_format"):
            store = self.controller.store
            root_id = str(row["root_id"])
            relative_path = str(row["relative_path"])
            members = self.controller.store.list_archive_members(
                root_id, relative_path, limit=1_000
            )
            self.file_preview.show_archive(
                path,
                metadata,
                members,
                member_loader=_archive_member_loader(store, root_id, relative_path),
                record=row,
            )
            return
        self.file_preview.show_path(
            path,
            placeholder=bool(row.get("is_placeholder")),
            metadata=metadata,
            record=row,
        )


class AuditPage(QWidget):
    def __init__(self, controller: WorkspaceController) -> None:
        super().__init__()
        self.controller = controller
        layout = QVBoxLayout(self)
        title = QLabel("Audit")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        description = QLabel(
            "Review patterns already present in the inventory and turn accepted findings into "
            "saved AI guidance. Audit reads inventory metadata only and never changes files."
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Source context"))
        self.source = QComboBox()
        controls.addWidget(self.source)
        analyze = QPushButton("Run AI metadata audit")
        analyze.clicked.connect(self.run_audit)
        controls.addWidget(analyze)
        controls.addStretch()
        layout.addLayout(controls)
        self.guidance = GuidanceContextBar(
            "audit",
            controller.compile_prompt,
            load_context=controller.ai_context,
            save_context=controller.set_ai_context,
        )
        if os.getenv("DEEPSEEK_API_KEY"):
            self.guidance.provider.setCurrentText("deepseek")
        layout.addWidget(self.guidance)

        self.model = DictTableModel(
            [
                ("selected", "Apply"),
                ("target_label", "Guidance"),
                ("pattern", "Observed pattern"),
                ("confidence", "Confidence"),
                ("evidence", "Evidence"),
            ]
        )
        self.table = QTableView()
        self.table.setModel(self.model)
        configure_data_table(self.table)
        self.table.selectionModel().currentChanged.connect(self.preview_guidance)
        install_table_context_menu(self.table, self._audit_context_actions)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("Select a finding to preview its proposed guidance.")
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(self.preview)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        actions = QHBoxLayout()
        apply_selected = QPushButton("Save selected guidance revisions")
        apply_selected.clicked.connect(self.apply_selected)
        actions.addWidget(apply_selected)
        self.status = QLabel("Run the audit after inventorying one or more sources.")
        self.status.setWordWrap(True)
        actions.addWidget(self.status, 1)
        layout.addLayout(actions)

        controller.workspace_changed.connect(self.refresh_sources)
        controller.workspace_changed.connect(self.guidance.refresh_context)
        controller.inventory_changed.connect(self.inventory_updated)
        self.refresh_sources()

    def refresh_sources(self) -> None:
        current = self.source.currentData()
        self.source.clear()
        self.source.addItem("All sources", None)
        for source in self.controller.sources.values():
            self.source.addItem(f"{source.name} — {source.path}", source.id)
        if current:
            index = self.source.findData(current)
            if index >= 0:
                self.source.setCurrentIndex(index)

    def inventory_updated(self) -> None:
        self.model.set_rows([])
        self.preview.clear()
        self.status.setText("Inventory changed. Run the audit to review current patterns.")

    def run_audit(self) -> None:
        if self.guidance.content_kind.currentText() != "Files & folders":
            QMessageBox.information(
                self,
                "Email source required",
                "Email auditing uses a separate message/folder inventory. Register and authorize "
                "an email source before using this context.",
            )
            return
        root_id = self.source.currentData()
        scope = {str(root_id)} if root_id else None
        if not self.controller.store:
            QMessageBox.warning(self, "Workspace required", "Open a workspace first.")
            return
        provider_name = self.guidance.provider.currentText()
        try:
            preview = self.controller.provider_request_preview(
                scope or set(self.controller.sources),
                (),
                provider_name,
                self.guidance.model.currentText(),
                (EvidenceClass.METADATA,),
                0,
                0,
            )
            if not preview.allowed:
                raise PermissionError("; ".join(preview.blocked_reasons))
            provider = _provider_for(
                self.controller, provider_name, self.guidance.model.currentText()
            )
            if not callable(getattr(provider, "audit_inventory", None)):
                raise RuntimeError("Selected provider does not support inventory discovery tools")
            query = InventoryQueryService(
                self.controller.inventory_items_with_tags(),
                self.controller.store.list_source_payloads(),
                self.controller.metadata_cache_stats(),
                self.controller.folder_planning_context(scope or set(self.controller.sources)),
            )
            task = BackgroundTaskDialog(
                "Auditing inventory metadata",
                "AI is probing bounded metadata summaries and patterns…",
                lambda: provider.audit_inventory(
                    query,
                    scope,
                    self.controller.latest_prompt_text("view:audit"),
                ),
                self,
            )
            if task.run() != QDialog.DialogCode.Accepted:
                raise RuntimeError(task.error_message or "AI audit did not finish")
            proposals = task.result_value
        except Exception as error:
            QMessageBox.critical(self, "AI audit failed safely", str(error))
            return
        labels = {
            "workspace": "General",
            "rename": "Rename",
            "cleanup": "Cleanup",
            "move": "Move",
            "folder": "Folder Plan",
            "action": "Focused Actions",
        }
        rows = [
            {
                "selected": True,
                "target": proposal.target,
                "target_label": labels.get(proposal.target, proposal.target.title()),
                "pattern": proposal.pattern,
                "confidence": f"{proposal.confidence:.0%}",
                "evidence": proposal.evidence,
                "guidance": proposal.guidance,
            }
            for proposal in proposals
        ]
        self.model.set_rows(rows)
        self.preview.clear()
        if rows:
            self.table.selectRow(0)
            self.preview.setPlainText(str(rows[0]["guidance"]))
            self.status.setText(f"Found {len(rows)} guidance proposal(s). Review before saving.")
        else:
            self.status.setText("No strong reusable patterns were found in this source context.")

    def _audit_context_actions(self, rows: list[dict[str, Any]]) -> list[tuple[str, Any]]:
        row_ids = {id(row) for row in rows}
        return [
            (
                f"Remove {len(rows):,} selected proposal(s)",
                lambda: self.model.remove_rows(
                    [index for index, row in enumerate(self.model.rows) if id(row) in row_ids]
                ),
            ),
            (
                f"AI re-propose {len(rows):,} selected item(s)…",
                lambda: self._repropose_audit(row_ids),
            ),
        ]

    def _repropose_audit(self, row_ids: set[int]) -> None:
        rows = [row for row in self.model.rows if id(row) in row_ids]
        if not rows:
            return
        dialog = ReproposalDialog(len(rows), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if self.guidance.provider.currentText() != "deepseek":
            QMessageBox.information(
                self,
                "DeepSeek audit available",
                "Select DeepSeek for metadata-tool audit corrections.",
            )
            return
        try:
            provider = _provider_for(self.controller, "deepseek", self.guidance.model.currentText())
            if not isinstance(provider, DeepSeekProvider) or not self.controller.store:
                raise RuntimeError("Inventory discovery provider is unavailable")
            root_id = self.source.currentData()
            scope = {str(root_id)} if root_id else None
            query = InventoryQueryService(
                self.controller.inventory_items_with_tags(),
                self.controller.store.list_source_payloads(),
                self.controller.metadata_cache_stats(),
                self.controller.folder_planning_context(scope or set(self.controller.sources)),
            )
            correction_context = json.dumps(
                {
                    "task": "Re-propose only these audit guidance findings using the correction.",
                    "correction": dialog.correction(),
                    "selected_proposals": rows,
                },
                ensure_ascii=False,
                default=str,
            )
            task = BackgroundTaskDialog(
                "AI correcting audit guidance",
                f"Reconsidering {len(rows):,} selected guidance proposal(s)…",
                lambda: provider.audit_inventory(query, scope, correction_context),
                self,
            )
            if task.run() != QDialog.DialogCode.Accepted:
                raise RuntimeError(task.error_message or "AI correction did not finish")
            labels = {
                "workspace": "General",
                "rename": "Rename",
                "cleanup": "Cleanup",
                "move": "Move",
                "folder": "Folder Plan",
                "action": "Focused Actions",
            }
            replacement = [
                {
                    "selected": False,
                    "target": proposal.target,
                    "target_label": labels.get(proposal.target, proposal.target.title()),
                    "pattern": proposal.pattern,
                    "confidence": f"{proposal.confidence:.0%}",
                    "evidence": proposal.evidence,
                    "guidance": proposal.guidance,
                }
                for proposal in task.result_value
            ]
            retained = [row for row in self.model.rows if id(row) not in row_ids]
            self.model.set_rows([*retained, *replacement])
            self.status.setText(
                f"Replaced {len(rows):,} selected finding(s) with {len(replacement):,} "
                "unchecked AI correction(s)."
            )
        except Exception as error:
            QMessageBox.critical(self, "Audit correction failed safely", str(error))

    def preview_guidance(self, current: QModelIndex, previous: QModelIndex) -> None:
        row = self.model.row(current)
        self.preview.setPlainText(str(row.get("guidance", "")) if row else "")

    def apply_selected(self) -> None:
        selected = [row for row in self.model.rows if row.get("selected")]
        if not selected:
            QMessageBox.information(self, "Nothing selected", "Select one or more findings first.")
            return
        if not self.controller.store:
            QMessageBox.warning(self, "Workspace required", "Open a workspace first.")
            return
        grouped: dict[str, list[str]] = {}
        for row in selected:
            grouped.setdefault(str(row["target"]), []).append(str(row["guidance"]))
        saved = 0
        try:
            for target, additions in grouped.items():
                profile_id = "workspace:general" if target == "workspace" else f"view:{target}"
                kind = PromptLayerKind.WORKSPACE if target == "workspace" else PromptLayerKind.VIEW
                existing = self.controller.latest_prompt_text(profile_id).strip()
                unique = [text for text in additions if text not in existing]
                if not unique:
                    continue
                combined = "\n\n".join([value for value in [existing, *unique] if value])
                PromptCompiler().validate_editable(combined)
                self.controller.save_prompt_revision(PromptRevision(profile_id, kind, combined))
                saved += 1
            self.status.setText(
                f"Saved {saved} guidance revision(s). Review them under Settings > AI Guidance."
            )
        except Exception as error:
            QMessageBox.critical(self, "Guidance not saved", str(error))


class UpdatesPage(QWidget):
    def __init__(self, controller: WorkspaceController) -> None:
        super().__init__()
        self.controller = controller
        layout = QVBoxLayout(self)
        title = QLabel("Updates")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        description = QLabel(
            "Review installed software separately from Download category files. AI update research "
            "uses strict structured results, and verified application URLs are preserved across versions."
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        self.guidance = GuidanceContextBar(
            "updates",
            controller.compile_prompt,
            load_context=controller.ai_context,
            save_context=controller.set_ai_context,
        )
        layout.addWidget(self.guidance)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Show"))
        self.scope = QComboBox()
        self.scope.addItems(["Software Inventory", "Download category items"])
        self.scope.currentTextChanged.connect(self._mode_changed)
        controls.addWidget(self.scope)
        controls.addWidget(QLabel("Release channel"))
        self.channel = QComboBox()
        self.channel.addItem("Full releases only", ReleaseChannel.FULL_RELEASE.value)
        self.channel.addItem("Include pre-releases", ReleaseChannel.PRE_RELEASE.value)
        self.channel.addItem("Include beta", ReleaseChannel.BETA.value)
        self.channel.addItem("Include alpha", ReleaseChannel.ALPHA.value)
        self.channel.currentIndexChanged.connect(self._save_release_policy)
        controls.addWidget(self.channel)
        refresh = QPushButton("Refresh software inventory")
        refresh.clicked.connect(self.refresh_software)
        controls.addWidget(refresh)
        self.refresh_software_button = refresh
        self.defender_button = QPushButton("Refresh Defender history")
        self.defender_button.clicked.connect(self.refresh_defender)
        controls.addWidget(self.defender_button)
        self.research_button = QPushButton("Run AI research on selected…")
        self.research_button.clicked.connect(self.run_research)
        controls.addWidget(self.research_button)
        self.recheck_button = QPushButton("Recheck saved hints")
        self.recheck_button.clicked.connect(self.recheck_hints)
        controls.addWidget(self.recheck_button)
        self.open_page_button = QPushButton("Open update page")
        self.open_page_button.clicked.connect(self.open_update_page)
        controls.addWidget(self.open_page_button)
        controls.addStretch()
        layout.addLayout(controls)
        self.model = DictTableModel([])
        self.table = QTableView()
        self.table.setModel(self.model)
        configure_data_table(self.table)
        self.table.selectionModel().currentChanged.connect(self.preview_selected)
        install_table_context_menu(
            self.table,
            lambda _rows: [
                ("AI re-audit selected…", self.run_research),
                ("Recheck saved version hints", self.recheck_hints),
                ("Open preferred update page", self.open_update_page),
            ],
        )
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.table)
        self.details = FilePreview()
        splitter.addWidget(self.details)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)
        self.status = QLabel(
            "AI research is initiated by an MCP-capable host. It searches with the selected release "
            "policy, returns a JSON-Schema-validated latest version, and preserves the preferred web page."
        )
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        controller.workspace_changed.connect(self._load_release_policy)
        controller.workspace_changed.connect(self.guidance.refresh_context)
        controller.workspace_changed.connect(self.refresh)
        controller.inventory_changed.connect(self.refresh)
        controller.software_changed.connect(self.refresh)
        self._load_release_policy()
        self._mode_changed()
        self.refresh()

    def refresh_software(self) -> None:
        task = BackgroundTaskDialog(
            "Refreshing software inventory",
            "Reading installed application records…",
            self.controller.refresh_software_inventory,
            self,
        )
        try:
            if task.run() != QDialog.DialogCode.Accepted:
                raise RuntimeError(task.error_message or "Software inventory did not finish")
            count = int(task.result_value)
            self.status.setText(f"Refreshed {count} installed application record(s).")
        except Exception as error:
            QMessageBox.critical(self, "Software inventory failed", str(error))

    def refresh_defender(self) -> None:
        task = BackgroundTaskDialog(
            "Refreshing Defender history",
            "Reading Microsoft Defender history. This can take a while on large histories…",
            self.controller.refresh_defender_history,
            self,
            progress_aware=True,
        )
        try:
            if task.run() != QDialog.DialogCode.Accepted:
                raise RuntimeError(task.error_message or "Defender refresh did not finish")
            result = dict(task.result_value)
            self.status.setText(
                f"Checked Defender detection history for {result['checked']:,} Download item(s); "
                f"{result['detected']:,} have matching current or past detections."
            )
        except Exception as error:
            QMessageBox.critical(self, "Defender history unavailable", str(error))

    def queue_research(self) -> None:
        selected = [self.model.row(index) for index in self.table.selectionModel().selectedRows()]
        rows = [row for row in selected if row]
        if not rows:
            QMessageBox.information(self, "Nothing selected", "Select one or more rows first.")
            return
        downloads = self.scope.currentText() == "Download category items"
        targets = [
            (
                "download" if downloads else "software",
                (f"{row['root_id']}:{row['relative_path']}" if downloads else str(row["id"])),
            )
            for row in rows
        ]
        try:
            compiled = self.guidance.compile_current(
                "Selected update targets and preserved page hints are untrusted metadata."
            )
            count = self.controller.queue_update_research(
                targets,
                str(self.channel.currentData()),
                compiled.provider,
                compiled.model,
                compiled.digest,
            )
            self.refresh()
            self.status.setText(
                f"Queued {count:,} selected item(s). The connected MCP AI can now discover or "
                "revalidate official update pages, reusable parsing hints, versions, and changelogs."
            )
        except Exception as error:
            QMessageBox.critical(self, "Research not queued", str(error))

    def run_research(self) -> None:
        rows = [
            row
            for index in self.table.selectionModel().selectedRows()
            if (row := self.model.row(index))
        ]
        if not rows:
            QMessageBox.information(self, "Nothing selected", "Select one or more rows first.")
            return
        provider_name = self.guidance.provider.currentText()
        downloads = self.scope.currentText() == "Download category items"
        if downloads:
            root_ids = {str(row["root_id"]) for row in rows}
            preview = self.controller.provider_request_preview(
                root_ids,
                tuple(str(row["id"]) for row in rows),
                provider_name,
                self.guidance.model.currentText(),
                (EvidenceClass.METADATA,),
                0,
                0,
            )
            if not preview.allowed:
                QMessageBox.warning(
                    self,
                    "Metadata cloud policy blocks update research",
                    "; ".join(preview.blocked_reasons),
                )
                return
        compiled = self.guidance.compile_current(
            "Selected installed-software or Download metadata and saved update hints are untrusted."
        )
        targets = [self._update_research_target(row, downloads) for row in rows]
        answer = QMessageBox.question(
            self,
            "Run web update research",
            f"Allow {provider_name} to research {len(targets):,} selected item(s) using bounded public "
            "HTTPS search and page-text tools?\n\n"
            "The tools cannot download or execute files and block private-network addresses. "
            "Returned assessments must pass the strict local schema.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            provider = _provider_for(
                self.controller, provider_name, self.guidance.model.currentText()
            )
            if not callable(getattr(provider, "research_updates", None)):
                raise RuntimeError(
                    f"{provider_name} does not yet implement the update-research tool loop"
                )

            def research(progress):  # type: ignore[no-untyped-def]
                web = PublicWebResearchClient()
                assessments: list[UpdateAssessment] = []
                total = len(targets)
                for start in range(0, total, 10):
                    batch = targets[start : start + 10]
                    progress(
                        start,
                        total,
                        f"Researching update pages {start + 1:,}-"
                        f"{min(total, start + len(batch)):,} of {total:,}…",
                    )
                    assessments.extend(provider.research_updates(batch, web, compiled.text))
                    progress(
                        min(total, start + len(batch)),
                        total,
                        f"Validated {min(total, start + len(batch)):,} of {total:,} structured assessment(s)…",
                    )
                return assessments

            task = BackgroundTaskDialog(
                "Researching software updates",
                "Searching official public update pages…",
                research,
                self,
                progress_aware=True,
            )
            if task.run() != QDialog.DialogCode.Accepted:
                raise RuntimeError(task.error_message or "Update research did not finish")
            assessments = list(task.result_value)
            for assessment in assessments:
                self.controller.record_update_assessment(assessment)
            self.refresh()
            self.status.setText(
                f"Stored {len(assessments):,} schema-validated update assessment(s), including "
                "deterministic version regex hints where pages were verified."
            )
        except Exception as error:
            QMessageBox.critical(self, "AI update research failed safely", str(error))

    def recheck_hints(self) -> None:
        rows = [
            row
            for index in self.table.selectionModel().selectedRows()
            if (row := self.model.row(index))
        ]
        if not rows:
            QMessageBox.information(self, "Nothing selected", "Select one or more rows first.")
            return
        downloads = self.scope.currentText() == "Download category items"

        def check(progress):  # type: ignore[no-untyped-def]
            web = PublicWebResearchClient()
            results: list[tuple[UpdateAssessment | None, str, str, str]] = []
            total = len(rows)
            for index, row in enumerate(rows, start=1):
                entity_kind = "download" if downloads else "software"
                entity_key = (
                    f"{row['root_id']}:{row['relative_path']}" if downloads else str(row["id"])
                )
                progress(index - 1, total, f"Checking saved hint {index:,} of {total:,}…")
                try:
                    assessment_record = row.get(
                        "update_assessment" if downloads else "assessment", {}
                    )
                    previous = UpdateAssessment.model_validate(assessment_record.get("facts", {}))
                    hint_record = row.get("update_hint", {})
                    hint_facts = hint_record.get("facts", {})
                    hint_value = previous.update_page_hint or UpdatePageHint.model_validate(
                        hint_facts.get("update_page_hint", {})
                    )
                    page = web.fetch(str(hint_value.url))
                    latest = extract_version_with_hint(str(page["text"]), hint_value)
                    current = previous.current_version
                    assessment = previous.model_copy(
                        update={
                            "latest_version": latest,
                            "update_available": (
                                latest != current if current else previous.update_available
                            ),
                            "result_status": "verified" if latest != current else "no_update",
                            "checked_at": utc_now(),
                            "next_check_strategy": "reuse_hint",
                        }
                    )
                    results.append((assessment, entity_kind, entity_key, ""))
                except Exception as error:
                    results.append((None, entity_kind, entity_key, str(error)))
                progress(index, total, f"Checked saved hint {index:,} of {total:,}…")
            return results

        task = BackgroundTaskDialog(
            "Rechecking saved update hints",
            "Fetching saved pages and applying safe version regexes…",
            check,
            self,
            progress_aware=True,
        )
        try:
            if task.run() != QDialog.DialogCode.Accepted:
                raise RuntimeError(task.error_message or "Saved-hint recheck did not finish")
            successful = 0
            failed = 0
            for assessment, entity_kind, entity_key, error in task.result_value:
                if assessment is not None:
                    self.controller.record_update_assessment(assessment)
                    successful += 1
                else:
                    self.controller.record_update_hint_failure(entity_kind, entity_key, error)
                    failed += 1
            self.refresh()
            self.status.setText(
                f"Rechecked {successful + failed:,} saved hint(s): {successful:,} succeeded; "
                f"{failed:,} need AI re-audit."
            )
        except Exception as error:
            QMessageBox.critical(self, "Saved-hint recheck failed safely", str(error))

    def _update_research_target(self, row: dict[str, Any], downloads: bool) -> dict[str, Any]:
        assessment = row.get("update_assessment" if downloads else "assessment", {})
        hint = row.get("update_hint", {})
        metadata = dict(row.get("metadata", {}))
        bounded_metadata = {
            key: value
            for key, value in metadata.items()
            if key
            in {
                "ProductName",
                "ProductVersion",
                "FileVersion",
                "CompanyName",
                "FileDescription",
                "OriginalFilename",
                "InternalName",
                "copyright",
                "title",
                "archive_format",
            }
        }
        return {
            "entity_kind": "download" if downloads else "software",
            "entity_key": (
                f"{row['root_id']}:{row['relative_path']}" if downloads else str(row["id"])
            ),
            "application_name": (
                bounded_metadata.get("ProductName")
                or bounded_metadata.get("FileDescription")
                or row.get("name")
                or row.get("relative_path")
            ),
            "publisher": row.get("publisher") or bounded_metadata.get("CompanyName", ""),
            "current_version": (
                row.get("version") if not downloads else row.get("product_version", "")
            ),
            "filename": row.get("relative_path", "") if downloads else "",
            "metadata": bounded_metadata,
            "previous_assessment": assessment,
            "previous_hint": hint,
            "release_channel_policy": str(self.channel.currentData()),
        }

    def _mode_changed(self) -> None:
        downloads = self.scope.currentText() == "Download category items"
        self.refresh_software_button.setVisible(not downloads)
        self.defender_button.setVisible(downloads and platform.system() == "Windows")
        self.open_page_button.setVisible(True)
        self.refresh()

    def _load_release_policy(self) -> None:
        if not self.controller.store:
            return
        value = (
            self.controller.store.get_meta("updates_release_channel")
            or ReleaseChannel.FULL_RELEASE.value
        )
        index = self.channel.findData(value)
        self.channel.setCurrentIndex(max(0, index))

    def _save_release_policy(self) -> None:
        if self.controller.store:
            self.controller.store.set_meta(
                "updates_release_channel", str(self.channel.currentData())
            )

    def _software_rows(self) -> list[dict[str, Any]]:
        assert self.controller.store is not None
        hints = {
            record["entity_key"]: record
            for record in self.controller.store.list_semantic_records("software", "update_hint")
        }
        assessments = {
            record["entity_key"]: record
            for record in self.controller.store.list_semantic_records(
                "software", "update_assessment"
            )
        }
        errors = {
            record["entity_key"]: record
            for record in self.controller.store.list_semantic_records(
                "software", "update_hint_error"
            )
            if record.get("status") == "error"
        }
        requests = {
            record["entity_key"]: record
            for record in self.controller.store.list_semantic_records(
                "software", "update_research_request"
            )
            if record.get("status") == "current"
        }
        rows = []
        for package in self.controller.software_packages:
            hint = hints.get(str(package["id"]), {})
            assessment = assessments.get(str(package["id"]), {})
            facts = assessment.get("facts", {})
            hint_facts = hint.get("facts", {})
            page_hint = (
                facts.get("update_page_hint") or hint_facts.get("update_page_hint", {}) or {}
            )
            changelog = facts.get("changelog_hint") or hint_facts.get("changelog_hint", {}) or {}
            page_url = facts.get("official_page_url") or hint_facts.get(
                "official_page_url", hint_facts.get("official_url", "")
            )
            available = facts.get("update_available")
            rows.append(
                {
                    **package,
                    "latest_version": facts.get("latest_version", "Research needed"),
                    "update_available_display": (
                        "Yes" if available is True else "No" if available is False else "Unknown"
                    ),
                    "release_channel": facts.get("latest_release_channel", ""),
                    "official_page_url": page_url,
                    "direct_download_url": facts.get("direct_download_url", ""),
                    "knowledge_status": (
                        "AI research queued"
                        if str(package["id"]) in requests
                        else assessment.get("status", "research needed")
                    ),
                    "hint_status": page_hint.get("status", ""),
                    "hint_check": errors.get(str(package["id"]), {})
                    .get("facts", {})
                    .get(
                        "message",
                        "Ready" if page_hint.get("version_regex") else "AI research needed",
                    ),
                    "changelog_url": changelog.get("url", ""),
                    "assessment": assessment,
                    "update_hint": hint,
                }
            )
        return rows

    def _download_rows(self) -> list[dict[str, Any]]:
        assert self.controller.store is not None
        defender = {
            record["entity_key"]: record
            for record in self.controller.store.list_semantic_records("file", "windows_defender")
        }
        assessments = {
            record["entity_key"]: record
            for record in self.controller.store.list_semantic_records(
                "download", "update_assessment"
            )
        }
        hints = {
            record["entity_key"]: record
            for record in self.controller.store.list_semantic_records("download", "update_hint")
        }
        errors = {
            record["entity_key"]: record
            for record in self.controller.store.list_semantic_records(
                "download", "update_hint_error"
            )
            if record.get("status") == "error"
        }
        requests = {
            record["entity_key"]: record
            for record in self.controller.store.list_semantic_records(
                "download", "update_research_request"
            )
            if record.get("status") == "current"
        }
        rows = []
        for item in self.controller.download_items():
            key = f"{item['root_id']}:{item['relative_path']}"
            record = defender.get(key, {})
            facts = record.get("facts", {})
            detections = facts.get("detections", [])
            names = sorted(
                {
                    str(value.get("threat_name", "")).strip()
                    for value in detections
                    if value.get("threat_name")
                }
            )
            if detections:
                defender_status = "Detected: " + (
                    ", ".join(names) or f"{len(detections)} record(s)"
                )
            elif facts.get("status") == "no_matching_detection_history":
                defender_status = "No matching detection history"
            else:
                defender_status = "Not checked"
            metadata = dict(item.get("metadata", {}))
            assessment = assessments.get(key, {})
            update_facts = assessment.get("facts", {})
            hint_facts = hints.get(key, {}).get("facts", {})
            page_hint = (
                update_facts.get("update_page_hint") or hint_facts.get("update_page_hint", {}) or {}
            )
            changelog = (
                update_facts.get("changelog_hint") or hint_facts.get("changelog_hint", {}) or {}
            )
            available = update_facts.get("update_available")
            rows.append(
                {
                    **item,
                    "modified_display": _display_timestamp_ns(item.get("modified_ns")),
                    "created_display": _display_timestamp_ns(item.get("created_ns")),
                    "product_version": metadata.get(
                        "ProductVersion", metadata.get("FileVersion", "")
                    ),
                    "defender_status": defender_status,
                    "defender_detection_count": len(detections),
                    "defender": record,
                    "latest_version": update_facts.get("latest_version", ""),
                    "update_available_display": (
                        "Yes" if available is True else "No" if available is False else "Unknown"
                    ),
                    "official_page_url": update_facts.get("official_page_url")
                    or hint_facts.get("official_page_url", ""),
                    "direct_download_url": update_facts.get("direct_download_url")
                    or hint_facts.get("direct_download_url", ""),
                    "update_assessment": assessment,
                    "update_hint": hints.get(key, {}),
                    "research_status": ("AI research queued" if key in requests else ""),
                    "hint_status": page_hint.get("status", ""),
                    "hint_check": errors.get(key, {})
                    .get("facts", {})
                    .get(
                        "message",
                        "Ready" if page_hint.get("version_regex") else "AI research needed",
                    ),
                    "changelog_url": changelog.get("url", ""),
                }
            )
        return rows

    def refresh(self) -> None:
        if not self.controller.store:
            self.model.set_columns_and_rows([], [])
            return
        self.details.clear()
        if self.scope.currentText() == "Download category items":
            rows = self._download_rows()
            columns = [
                ("relative_path", "Path"),
                ("extension", "Type"),
                ("size", "Bytes"),
                ("modified_display", "Modified"),
                ("created_display", "Created"),
                ("product_version", "File/product version"),
                ("latest_version", "Latest version"),
                ("update_available_display", "Update"),
                ("research_status", "Research status"),
                ("official_page_url", "Preferred update page"),
                ("hint_status", "Page hint"),
                ("hint_check", "Hint check"),
                ("changelog_url", "Changelog"),
                ("defender_detection_count", "Defender detections"),
                ("defender_status", "Defender history"),
            ]
            self.status.setText(
                f"{len(rows):,} file(s) are in sources or assignments marked Downloads. "
                "Select a row to see every extracted metadata field. Defender status is historical "
                "correlation, not a claim that an unmatched file is clean."
            )
        else:
            rows = self._software_rows()
            columns = [
                ("name", "Application"),
                ("publisher", "Publisher"),
                ("version", "Installed version"),
                ("latest_version", "Latest version"),
                ("update_available_display", "Update"),
                ("release_channel", "Channel"),
                ("official_page_url", "Preferred update page"),
                ("hint_status", "Page hint"),
                ("hint_check", "Hint check"),
                ("changelog_url", "Changelog"),
                ("knowledge_status", "Knowledge status"),
            ]
        self.model.set_columns_and_rows(columns, rows)

    def preview_selected(self) -> None:
        row = self.model.row(self.table.currentIndex())
        if not row:
            self.details.clear()
            return
        metadata = dict(row.get("metadata", {}))
        root_id = str(row.get("root_id", ""))
        relative_path = str(row.get("relative_path", ""))
        source = self.controller.sources.get(root_id)
        if source and relative_path:
            members = None
            member_loader = None
            if self.controller.store and metadata.get("archive_format"):
                store = self.controller.store
                members = store.list_archive_members(root_id, relative_path, limit=1_000)
                member_loader = _archive_member_loader(store, root_id, relative_path)
            self.details.show_path(
                source.path / relative_path,
                placeholder=bool(row.get("is_placeholder")),
                metadata=metadata,
                member_page=members,
                member_loader=member_loader,
                record=row,
            )
            return
        self.details.show_record(
            dict(row),
            caption=str(row.get("name") or row.get("relative_path") or "Indexed record"),
        )

    def open_update_page(self) -> None:
        row = self.model.row(self.table.currentIndex())
        if not row:
            QMessageBox.information(self, "Select software", "Select one application first.")
            return
        url = str(row.get("official_page_url") or row.get("direct_download_url") or "")
        if not url.startswith("https://"):
            QMessageBox.information(
                self,
                "Research needed",
                "No verified update page is stored yet. Ask the connected AI host to research updates.",
            )
            return
        QDesktopServices.openUrl(QUrl(url))


class ReviewPage(QWidget):
    def __init__(
        self,
        title_text: str,
        view_key: str,
        columns: list[tuple[str, str]],
        controller: WorkspaceController,
    ) -> None:
        super().__init__()
        self.controller = controller
        self.view_key = view_key
        layout = QVBoxLayout(self)
        title = QLabel(title_text)
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        self.guidance = GuidanceContextBar(
            view_key,
            controller.compile_prompt,
            load_context=controller.ai_context,
            save_context=controller.set_ai_context,
        )
        layout.addWidget(self.guidance)
        self.context_notice = QLabel()
        self.context_notice.setWordWrap(True)
        layout.addWidget(self.context_notice)
        self.model = DictTableModel(columns)
        self.table = QTableView()
        self.table.setModel(self.model)
        configure_data_table(self.table)
        self.file_preview = FilePreview()
        splitter = QSplitter()
        splitter.addWidget(self.table)
        splitter.addWidget(self.file_preview)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)
        self.table.selectionModel().currentChanged.connect(self.preview_selection)
        self.guidance.content_kind.currentTextChanged.connect(self._content_kind_changed)
        controller.workspace_changed.connect(self.guidance.refresh_context)
        install_table_context_menu(self.table, self._review_context_actions)

    def _review_context_actions(self, rows: list[dict[str, Any]]) -> list[tuple[str, Any]]:
        row_ids = {id(row) for row in rows}
        actions: list[tuple[str, Any]] = [
            (
                f"Remove {len(rows):,} selected proposal(s)",
                lambda: self._remove_proposals(row_ids),
            )
        ]
        if self._proposal_field(rows):
            actions.append(
                (
                    f"AI re-propose {len(rows):,} selected item(s)…",
                    lambda: self._ai_repropose(row_ids),
                )
            )
        return actions

    def _remove_proposals(self, row_ids: set[int]) -> None:
        indexes = [index for index, row in enumerate(self.model.rows) if id(row) in row_ids]
        removed = self.model.remove_rows(indexes)
        self.context_notice.setText(f"Removed {removed:,} proposal(s) from this review batch.")

    def _proposal_field(self, rows: list[dict[str, Any]]) -> str:
        key = {
            "rename": "proposed",
            "folder": "projected",
            "move": "destination",
            "action": "rationale",
        }.get(self.view_key, "")
        return key if key and rows and all(key in row for row in rows) else ""

    def _ai_repropose(self, row_ids: set[int]) -> None:
        rows = [row for row in self.model.rows if id(row) in row_ids]
        proposal_field = self._proposal_field(rows)
        if not rows or not proposal_field:
            return
        provider_name = self.guidance.provider.currentText()
        model_name = self.guidance.model.currentText()
        if provider_name == "local":
            QMessageBox.information(
                self,
                "Choose an AI provider",
                "Select a configured AI provider in the context bar first.",
            )
            return
        dialog = ReproposalDialog(len(rows), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        correction = dialog.correction()
        identifiers: dict[str, dict[str, Any]] = {}
        evidence_rows = []
        for index, row in enumerate(rows):
            identifier = str(row.get("item_id") or row.get("id") or f"review-row-{index}")
            identifiers[identifier] = row
            evidence_rows.append(
                {
                    "item_id": identifier,
                    "current_proposal": row.get(proposal_field, ""),
                    "row": {
                        key: value
                        for key, value in row.items()
                        if not key.startswith("_") and key not in {"assessment", "update_hint"}
                    },
                }
            )
        evidence = {
            "task": f"Re-propose only the '{proposal_field}' value for every selected row.",
            "user_correction": correction,
            "requirements": [
                "Return exactly one finding for each supplied item_id.",
                "Put the replacement proposal in suggestion.",
                "Do not change or omit item identifiers.",
                "Do not propose filesystem actions.",
            ],
            "selected_items": evidence_rows,
        }
        try:
            compiled = self.guidance.compile_current(
                json.dumps(evidence, ensure_ascii=False, default=str)
            )
            answer = QMessageBox.question(
                self,
                "Send correction context",
                f"Send {len(rows):,} selected proposal(s) and your correction to "
                f"{provider_name} ({model_name})? The response can revise proposals only.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            provider = _provider_for(self.controller, provider_name, model_name)
            task = BackgroundTaskDialog(
                "AI re-proposing selected items",
                f"Waiting for corrected structured proposals for {len(rows):,} item(s)…",
                lambda: provider.analyze(compiled),
                self,
            )
            if task.run() != QDialog.DialogCode.Accepted:
                raise RuntimeError(task.error_message or "AI correction did not finish")
            updated = 0
            for finding in task.result_value.findings:
                row = identifiers.get(str(finding.get("item_id", "")))
                suggestion = str(finding.get("suggestion", "")).strip()
                if row is None or not self._valid_reproposal(proposal_field, suggestion):
                    continue
                row[proposal_field] = suggestion
                row["selected"] = False
                row["status"] = "needs_review"
                row["confidence"] = float(finding.get("confidence", 0.0))
                if "reason" in row:
                    row["reason"] = str(finding.get("rationale", "AI correction"))
                updated += 1
            self.model.set_rows(self.model.rows)
            if isinstance(self, MovePage):
                self._revalidate_rows(self.model.rows, initialize=False)
            elif isinstance(self, FolderPlanPage):
                self._refresh_edited_rows()
            self.context_notice.setText(
                f"AI revised {updated:,} of {len(rows):,} selected proposal(s). "
                "Corrections are unchecked and require review."
            )
        except Exception as error:
            QMessageBox.critical(self, "AI re-proposal failed safely", str(error))

    @staticmethod
    def _valid_reproposal(field: str, suggestion: str) -> bool:
        if not suggestion or len(suggestion) > 500 or "\x00" in suggestion:
            return False
        if field == "proposed":
            return Path(suggestion).name == suggestion
        if field in {"projected", "destination"}:
            candidate = Path(suggestion)
            return not candidate.is_absolute() and ".." not in candidate.parts
        return True

    def _content_kind_changed(self, content_kind: str) -> None:
        files = content_kind == "Files & folders"
        self.table.setEnabled(files)
        self.file_preview.setEnabled(files)
        for button in self.findChildren(QPushButton):
            button.setEnabled(files)
        if not files:
            self.model.set_rows([])
            self.context_notice.setText(
                "Email mode is isolated from filesystem operations. Register and authorize an "
                "email source to enable message or email-folder proposals here."
            )
        else:
            self.context_notice.clear()

    def preview_selection(self, current: QModelIndex, previous: QModelIndex) -> None:
        row = self.model.row(current)
        if not row or "root_id" not in row or "relative_path" not in row:
            return
        source = self.controller.sources.get(str(row["root_id"]))
        if source:
            item = next(
                (
                    value
                    for value in self.controller.items
                    if str(value.get("root_id")) == str(row["root_id"])
                    and str(value.get("relative_path")) == str(row["relative_path"])
                ),
                {},
            )
            metadata = dict(item.get("metadata", row.get("metadata", {})))
            members = None
            member_loader = None
            if self.controller.store and metadata.get("archive_format"):
                store = self.controller.store
                root_id = str(row["root_id"])
                relative_path = str(row["relative_path"])
                members = store.list_archive_members(root_id, relative_path, limit=1_000)
                member_loader = _archive_member_loader(store, root_id, relative_path)
            self.file_preview.show_path(
                source.path / str(row["relative_path"]),
                placeholder=bool(item.get("is_placeholder", row.get("is_placeholder"))),
                metadata=metadata,
                member_page=members,
                member_loader=member_loader,
                record={"proposal": row, "inventory": item},
            )


class RenamePage(ReviewPage):
    def __init__(self, controller: WorkspaceController) -> None:
        super().__init__(
            "Rename",
            "rename",
            [
                ("selected", "Apply"),
                ("status", "Review"),
                ("current", "Current name"),
                ("proposed", "Proposed name"),
                ("confidence", "Confidence"),
                ("reason", "Reason"),
            ],
            controller,
        )
        controls = QHBoxLayout()
        self.naming_profiles = builtin_naming_profiles()
        self.naming_profile = QComboBox()
        self.naming_profile.addItems([profile.name for profile in self.naming_profiles])
        self.naming_profile.setCurrentIndex(4)
        configure = QPushButton("Naming wizard…")
        configure.clicked.connect(self.configure_naming)
        controls.addWidget(self.naming_profile)
        controls.addWidget(configure)
        propose = QPushButton("Generate deterministic proposals")
        propose.clicked.connect(self.generate)
        analyze = QPushButton("Analyze with selected AI provider…")
        analyze.clicked.connect(self.analyze_with_ai)
        share = QPushButton("Create MCP selection scope")
        share.clicked.connect(self.create_mcp_scope)
        commit = QPushButton("Freeze, preflight & commit selected…")
        commit.clicked.connect(self.commit_selected)
        controls.addWidget(propose)
        controls.addWidget(analyze)
        controls.addWidget(share)
        controls.addWidget(commit)
        controls.addStretch()
        self.layout().addLayout(controls)

    def generate(self) -> None:
        rows: list[dict[str, Any]] = []
        for item in self.controller.items:
            if item.get("is_dir"):
                continue
            current = Path(item["relative_path"]).name
            stem = " ".join(Path(current).stem.replace("_", " ").replace("-", " ").split())
            profile = self.naming_profiles[self.naming_profile.currentIndex()]
            proposed = profile.render(
                {
                    "clean_title": stem,
                    "title": stem,
                    "descriptor": stem,
                    "semantic_description": stem,
                    "description": stem,
                },
                Path(current).suffix,
            )
            rows.append(
                {
                    "selected": proposed != current,
                    "status": "proposed",
                    "current": current,
                    "proposed": proposed,
                    "confidence": 0.65,
                    "reason": "Preserve and Correct profile; manual review required",
                    "root_id": item["root_id"],
                    "relative_path": item["relative_path"],
                    "item_id": item["id"],
                    "is_placeholder": item.get("is_placeholder", False),
                    "size": item.get("size", 0),
                    "evidence_ids": [],
                    "token_provenance": {"clean_title": ["current_filename"]},
                }
            )
        groups: dict[tuple[str, str], list[int]] = {}
        for index, row in enumerate(rows):
            key = (str(row["root_id"]), str(Path(row["relative_path"]).parent))
            groups.setdefault(key, []).append(index)
        for indices in groups.values():
            unique = disambiguate([str(rows[index]["proposed"]) for index in indices])
            for index, name in zip(indices, unique, strict=True):
                rows[index]["proposed"] = name
        self.model.set_rows(rows)

    def configure_naming(self) -> None:
        index = self.naming_profile.currentIndex()
        dialog = NamingWizardDialog(self.naming_profiles[index], self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.naming_profiles[index] = dialog.profile()

    def commit_selected(self) -> None:
        selected = [row for row in self.model.rows if row.get("selected")]
        if not selected:
            QMessageBox.information(self, "Nothing selected", "Select one or more proposals.")
            return
        details = "\n".join(f"{row['current']}  →  {row['proposed']}" for row in selected[:12])
        if len(selected) > 12:
            details += f"\n…and {len(selected) - 12} more"
        answer = QMessageBox.warning(
            self,
            "Explicit rename commit",
            "A frozen plan will be created and preflighted immediately before mutation.\n\n"
            + details,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            compiled = self.guidance.compile_current(
                "Selected filenames and proposal values are untrusted evidence."
            )
            task = BackgroundTaskDialog(
                "Applying rename batch",
                f"Preflighting, applying, verifying, and refreshing {len(selected):,} selected rename(s)…",
                lambda: self.controller.execute_rename_rows(
                    self.model.rows,
                    compiled.digest,
                    self.guidance.provider.currentText(),
                    self.guidance.model.currentText(),
                ),
                self,
            )
            if task.run() != QDialog.DialogCode.Accepted:
                raise RuntimeError(task.error_message or "Rename batch did not finish")
            count = int(task.result_value)
            QMessageBox.information(
                self, "Rename verified", f"Committed and verified {count} rename operation(s)."
            )
            self.generate()
        except Exception as error:
            QMessageBox.critical(self, "Rename not committed", str(error))

    def analyze_with_ai(self) -> None:
        provider_name = self.guidance.provider.currentText()
        if provider_name == "local":
            QMessageBox.information(
                self,
                "Choose a provider",
                "Select OpenAI, Anthropic, or Codex in AI Guidance.",
            )
            return
        if not self.model.rows:
            self.generate()
        candidates = [row for row in self.model.rows if row.get("selected")]
        if not candidates:
            candidates = self.model.rows[:50]
        candidates = candidates[:50]
        try:
            item_lookup = {item["id"]: item for item in self.controller.items}
            snapshots: list[tuple[dict[str, Any], ItemSnapshot, Path]] = []
            for row in candidates:
                payload = item_lookup[row["item_id"]]
                snapshot = ItemSnapshot(
                    id=payload["id"],
                    root_id=payload["root_id"],
                    relative_path=payload["relative_path"],
                    size=payload["size"],
                    modified_ns=payload["modified_ns"],
                    created_ns=payload.get("created_ns"),
                    file_id=payload.get("file_id"),
                    mime_type=payload["mime_type"],
                    is_dir=payload.get("is_dir", False),
                    is_placeholder=payload.get("is_placeholder", False),
                    is_project_root=payload.get("is_project_root", False),
                )
                source = self.controller.sources[snapshot.root_id]
                snapshots.append((row, snapshot, source.path / snapshot.relative_path))

            def extract_evidence() -> list[tuple[str, Any]]:
                registry = default_registry()
                return [
                    (snapshot.id, registry.extract(path, snapshot))
                    for _, snapshot, path in snapshots
                ]

            extraction = BackgroundTaskDialog(
                "Extracting local evidence",
                "Embedded text and only the pages needing OCR are being analyzed locally.",
                extract_evidence,
                self,
            )
            if extraction.run() != QDialog.DialogCode.Accepted:
                raise RuntimeError(extraction.error_message or "Evidence extraction did not finish")
            extracted = dict(extraction.result_value)
            evidence_records: list[dict[str, Any]] = []
            content_classes: set[EvidenceClass] = set()
            redaction_count = 0
            for row, snapshot, _ in snapshots:
                evidence = extracted[snapshot.id]
                if self.controller.store:
                    self.controller.store.save_evidence(evidence)
                row["evidence_ids"] = [evidence.id]
                row["token_provenance"] = {"suggested_name": [evidence.id, "compiled_prompt"]}
                evidence_records.append(
                    {
                        "item_id": snapshot.id,
                        "filename": Path(snapshot.relative_path).name,
                        "mime_type": snapshot.mime_type,
                        "summary": evidence.summary,
                        "language_candidates": evidence.language_candidates,
                        "confidence": evidence.confidence,
                        "confidence_route": evidence.confidence_route,
                    }
                )
                for value in evidence.content_classes:
                    content_classes.add(EvidenceClass(value))
                redaction_count += len(detect_secret_kinds(evidence.summary))

            compiled = self.guidance.compile_current(
                json.dumps(evidence_records, ensure_ascii=False)
            )
            preview = self.controller.provider_request_preview(
                {str(row["root_id"]) for row in candidates},
                tuple(str(row["item_id"]) for row in candidates),
                compiled.provider,
                compiled.model,
                tuple(sorted(content_classes, key=lambda value: value.value)),
                redaction_count,
                compiled.evidence_bytes,
            )
            if not preview.allowed:
                raise PermissionError("; ".join(preview.blocked_reasons))
            answer = QMessageBox.warning(
                self,
                "Confirm cloud analysis",
                f"Provider: {compiled.provider}\nModel: {compiled.model}\n"
                f"Items: {len(candidates)}\nRedacted evidence: {compiled.evidence_bytes:,} bytes\n"
                f"Content classes: {', '.join(value.value for value in preview.content_classes)}\n"
                f"Source policies: {', '.join(preview.source_policies.values())}\n"
                f"Redactions: {preview.redaction_count}\n"
                f"Estimated input: {preview.estimated_tokens:,} tokens\n\n"
                "Secret-like values and long account identifiers are masked before sending.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            provider = self._provider(compiled.provider, compiled.model)
            analysis = BackgroundTaskDialog(
                "Waiting for structured provider response",
                "The provider can return proposal data only; it has no commit authority.",
                lambda: provider.analyze(compiled),
                self,
            )
            if analysis.run() != QDialog.DialogCode.Accepted:
                raise RuntimeError(analysis.error_message or "Provider analysis did not finish")
            result = analysis.result_value
            rows = {row["item_id"]: row for row in self.model.rows}
            for finding in result.findings:
                row = rows.get(str(finding.get("item_id", "")))
                suggestion = str(finding.get("suggestion", "")).strip()
                if not row or not suggestion or Path(suggestion).name != suggestion:
                    continue
                row["proposed"] = suggestion
                row["selected"] = False
                row["status"] = "needs_review"
                row["confidence"] = float(finding.get("confidence", 0.0))
                row["reason"] = str(finding.get("rationale", "AI proposal"))
            self.model.set_rows(self.model.rows)
        except Exception as error:
            QMessageBox.critical(self, "AI analysis failed safely", str(error))

    def create_mcp_scope(self) -> None:
        candidates = [row for row in self.model.rows if row.get("selected")]
        if not candidates:
            QMessageBox.information(self, "Nothing selected", "Select proposal rows first.")
            return
        try:
            scope = self.controller.create_selection_scope(
                [str(row["item_id"]) for row in candidates[:250]]
            )
            self.context_notice.setText(
                f"Active MCP scope {scope.id} contains {len(scope.item_ids)} opaque item IDs and "
                f"expires at {scope.expires_at}. The MCP host cannot expand it."
            )
        except Exception as error:
            QMessageBox.critical(self, "Scope not created", str(error))

    def _provider(self, name: str, model: str) -> Any:
        return _provider_for(self.controller, name, model)


class FolderPlanPage(ReviewPage):
    def __init__(self, controller: WorkspaceController) -> None:
        super().__init__(
            "Folder Plan",
            "folder",
            [
                ("selected", "Apply"),
                ("node", "Proposal"),
                ("current", "Current folder"),
                ("projected", "Projected folder"),
                ("action", "Action"),
                ("status", "Status"),
                ("confidence", "Confidence"),
                ("issues", "Issues"),
            ],
            controller,
        )
        note = QLabel(
            "Folder Plan creates folders or renames them in place; it never moves files implicitly."
        )
        note.setWordWrap(True)
        self.layout().insertWidget(2, note)
        controls = QHBoxLayout()
        propose = QPushButton("Build folder proposals")
        propose.clicked.connect(self.generate)
        ai_propose = QPushButton("Propose with selected AI…")
        ai_propose.clicked.connect(self.generate_with_ai)
        commit = QPushButton("Freeze, preflight & commit selected…")
        commit.clicked.connect(self.commit_selected)
        controls.addWidget(propose)
        controls.addWidget(ai_propose)
        controls.addWidget(commit)
        controls.addStretch()
        self.layout().addLayout(controls)
        self.model.dataChanged.connect(self._refresh_edited_rows)

    def generate(self) -> None:
        rows: list[dict[str, Any]] = []
        if not self.controller.store:
            self.model.set_rows(rows)
            return
        categories = self.controller.store.list_category_payloads()
        for source in self.controller.sources.values():
            if not source.roles.intersection({FolderRole.DESTINATION, FolderRole.ARCHIVE}):
                continue
            current_folders = {
                str(item["relative_path"])
                for item in self.controller.items
                if item["root_id"] == source.id and item.get("is_dir")
            }
            changes: list[HierarchyChange] = []
            for category in categories:
                if not category.get("suggest_as_folder"):
                    continue
                if category.get("parent_id") not in source.category_ids:
                    continue
                projected = category["name"].strip()
                if projected in current_folders or not projected:
                    continue
                changes.append(HierarchyChange(projected, category_id=str(category["id"])))
            projection = UnionHierarchyPlanner().project(
                source.id,
                current_folders,
                changes,
                case_sensitive=bool(source.capabilities and source.capabilities.case_sensitive),
                windows_rules=platform.system() == "Windows",
            )
            folder_items = {
                str(item["relative_path"]): item
                for item in self.controller.items
                if item["root_id"] == source.id and item.get("is_dir")
            }
            for projected_row in projection.rows:
                if projected_row.action not in {
                    HierarchyAction.CREATE,
                    HierarchyAction.RENAME,
                }:
                    continue
                current = projected_row.current_path or ""
                projected = projected_row.projected_path or ""
                item = folder_items.get(projected_row.current_path or "", {})
                protected = bool(
                    item.get("is_project_root") or item.get("inside_protected_project")
                )
                issues = list(projected_row.issues)
                if protected:
                    issues.append("Protected project boundary")
                if len(Path(projected).parts) > self.controller.folder_depth_limit(
                    source.id, str(projected_row.category_id or "")
                ):
                    issues.append("Exceeds active folder-depth policy")
                node_path = projected_row.projected_path or projected_row.current_path or ""
                rows.append(
                    {
                        "selected": projected_row.action == HierarchyAction.CREATE and not issues,
                        "node": (
                            f"New folder: {Path(node_path).name}"
                            if projected_row.action == HierarchyAction.CREATE
                            else f"Rename folder: {Path(node_path).name}"
                        ),
                        "current": current,
                        "projected": projected,
                        "action": projected_row.action.value,
                        "status": "blocked" if issues else projected_row.status,
                        "confidence": 1.0
                        if projected_row.action == HierarchyAction.UNCHANGED
                        else 0.7,
                        "issues": "; ".join(issues),
                        "root_id": source.id,
                        "category_id": projected_row.category_id or "",
                        "reason": "Top-level category folder in an eligible destination root"
                        if projected_row.action == HierarchyAction.CREATE
                        else "Current and projected hierarchy are row-aligned",
                    }
                )
        self.model.set_rows(rows)

    def generate_with_ai(self) -> None:
        if not self.controller.store:
            QMessageBox.warning(self, "Workspace required", "Open a workspace first.")
            return
        if self.guidance.content_kind.currentText() != "Files & folders":
            QMessageBox.information(
                self,
                "Choose files and folders",
                "Folder Plan cannot mix email and file hierarchies.",
            )
            return
        provider_name = self.guidance.provider.currentText()
        destination_ids = {
            source.id
            for source in self.controller.sources.values()
            if source.roles.intersection({FolderRole.DESTINATION, FolderRole.ARCHIVE})
        }
        if not destination_ids:
            QMessageBox.information(
                self,
                "Destination required",
                "Assign Destination or Archive role to at least one source first.",
            )
            return
        query_root_ids = set(self.controller.sources)
        planning_context = self.controller.folder_planning_context(destination_ids)
        compiled = self.guidance.compile_current(
            "The provider may query bounded cached metadata, hierarchy, extension and MIME summaries.\n"
            "Approved organization taxonomy and folder-depth policy:\n"
            + json.dumps(planning_context, ensure_ascii=False)
        )
        preview = self.controller.provider_request_preview(
            query_root_ids,
            (),
            compiled.provider,
            compiled.model,
            (EvidenceClass.METADATA,),
            0,
            0,
        )
        if not preview.allowed:
            QMessageBox.warning(
                self,
                "Metadata cloud policy blocks AI Folder Plan",
                "; ".join(preview.blocked_reasons)
                + "\n\nSet each participating source to Metadata only (or a broader policy) "
                "under Sources & Categories > Edit selected source privacy.",
            )
            return
        answer = QMessageBox.question(
            self,
            "Run tool-driven Folder Plan",
            f"Allow {provider_name} to query cached metadata for {len(query_root_ids):,} source(s) "
            f"and {len(self.controller.items):,} inventory record(s)?\n\n"
            "It can return folder proposals only. Nothing is created until you select proposals "
            "and explicitly commit them.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            provider = _provider_for(
                self.controller, provider_name, self.guidance.model.currentText()
            )
            if not callable(getattr(provider, "plan_folders", None)):
                raise RuntimeError("Selected provider has no inventory tool loop")
            query = InventoryQueryService(
                self.controller.inventory_items_with_tags(),
                self.controller.store.list_source_payloads(),
                self.controller.metadata_cache_stats(),
                planning_context,
            )
            task = BackgroundTaskDialog(
                "AI exploring inventory metadata",
                f"{provider_name} is querying summaries and the current hierarchy before proposing folders…",
                lambda: provider.plan_folders(
                    query,
                    destination_ids,
                    query_root_ids,
                    compiled.text,
                    {
                        root_id: int(value["maximum_depth"])
                        for root_id, value in planning_context["roots"].items()
                    },
                ),
                self,
            )
            if task.run() != QDialog.DialogCode.Accepted:
                raise RuntimeError(task.error_message or "AI Folder Plan did not finish")
            self._show_ai_folder_proposals(list(task.result_value))
        except Exception as error:
            QMessageBox.critical(self, "AI Folder Plan failed safely", str(error))

    def _show_ai_folder_proposals(self, proposals: list[dict[str, Any]]) -> None:
        if not proposals:
            self.model.set_rows([])
            self.context_notice.setText(
                "AI found no evidence-grounded folder additions. No changes were proposed."
            )
            return
        rows: list[dict[str, Any]] = []
        grouped: dict[str, list[dict[str, Any]]] = {}
        for proposal in proposals:
            grouped.setdefault(str(proposal["root_id"]), []).append(proposal)
        for root_id, root_proposals in grouped.items():
            source = self.controller.sources[root_id]
            current_folders = {
                str(item["relative_path"])
                for item in self.controller.items
                if item["root_id"] == root_id and item.get("is_dir")
            }
            proposal_by_path = {str(value["projected"]): value for value in root_proposals}
            projection = UnionHierarchyPlanner().project(
                root_id,
                current_folders,
                [HierarchyChange(path) for path in proposal_by_path],
                case_sensitive=bool(source.capabilities and source.capabilities.case_sensitive),
                windows_rules=platform.system() == "Windows",
            )
            for projected_row in projection.rows:
                if projected_row.action not in {
                    HierarchyAction.CREATE,
                    HierarchyAction.RENAME,
                }:
                    continue
                path = projected_row.projected_path or projected_row.current_path or ""
                proposal = proposal_by_path.get(path, {})
                issues = list(projected_row.issues)
                if len(Path(path).parts) > self.controller.folder_depth_limit(root_id):
                    issues.append("Exceeds active folder-depth policy")
                rows.append(
                    {
                        "selected": False,
                        "node": (
                            f"New folder: {Path(path).name}"
                            if projected_row.action == HierarchyAction.CREATE
                            else f"Rename folder: {Path(path).name}"
                        ),
                        "current": projected_row.current_path or "",
                        "projected": projected_row.projected_path or "",
                        "action": projected_row.action.value,
                        "status": "blocked" if issues else "AI proposal; review",
                        "confidence": float(proposal.get("confidence", 0.0)),
                        "issues": "; ".join(issues),
                        "root_id": root_id,
                        "category_id": "",
                        "reason": str(proposal.get("rationale", "AI metadata proposal")),
                        "evidence": str(proposal.get("evidence", "Inventory metadata")),
                    }
                )
        self.model.set_rows(rows)
        self.context_notice.setText(
            f"AI returned {len(proposals):,} evidence-grounded folder suggestion(s). "
            "All are unchecked and require review."
        )

    def _refresh_edited_rows(self, *_args: Any) -> None:
        """Keep the visible action label honest after a projected path is edited."""
        for row in self.model.rows:
            current = str(row.get("current", ""))
            if not current:
                continue
            protected = "Protected project boundary" in str(row.get("issues", ""))
            if current == str(row.get("projected", "")):
                row["action"] = HierarchyAction.UNCHANGED.value
                row["status"] = "blocked" if protected else "aligned"
                row["issues"] = "Protected project boundary" if protected else ""
            else:
                row["action"] = HierarchyAction.RENAME.value
                row["status"] = "blocked" if protected else "needs preflight"
                row["issues"] = (
                    "Protected project boundary"
                    if protected
                    else "Will be fully validated when the plan is frozen"
                )
        self.model.set_rows(self.model.rows)

    def commit_selected(self) -> None:
        count = sum(bool(row.get("selected")) for row in self.model.rows)
        if not count:
            QMessageBox.information(self, "Nothing selected", "Select one or more folders.")
            return
        answer = QMessageBox.warning(
            self,
            "Explicit folder commit",
            f"Apply {count} selected folder operation(s)? "
            "This plan cannot delete or reparent anything.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            compiled = self.guidance.compile_current("Projected hierarchy is untrusted evidence.")
            task = BackgroundTaskDialog(
                "Applying folder batch",
                f"Preflighting and verifying {count:,} selected folder operation(s)…",
                lambda: self.controller.execute_folder_rows(self.model.rows, compiled.digest),
                self,
            )
            if task.run() != QDialog.DialogCode.Accepted:
                raise RuntimeError(task.error_message or "Folder batch did not finish")
            completed = int(task.result_value)
            QMessageBox.information(
                self, "Folders verified", f"Verified {completed} folder operation(s)."
            )
            self.generate()
        except Exception as error:
            QMessageBox.critical(self, "Folder plan not committed", str(error))


class MovePage(ReviewPage):
    def __init__(self, controller: WorkspaceController) -> None:
        super().__init__(
            "Move",
            "move",
            [
                ("selected", "Move"),
                ("status", "Review"),
                ("source", "Current folder"),
                ("destination", "Proposed folder"),
                ("filename", "Filename"),
                ("projected", "Projected target"),
                ("conflicts", "Conflicts"),
                ("reason", "Reason"),
            ],
            controller,
        )
        note = QLabel(
            "Move preserves filenames. Cross-volume moves use copy, verify, and finalize; "
            "originals then remain in quarantine."
        )
        note.setWordWrap(True)
        self.layout().insertWidget(2, note)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Destination root"))
        self.destination = QComboBox()
        controls.addWidget(self.destination)
        propose = QPushButton("Propose Inbox moves")
        propose.clicked.connect(self.generate)
        commit = QPushButton("Freeze, preflight & commit selected…")
        commit.clicked.connect(self.commit_selected)
        controls.addWidget(propose)
        controls.addWidget(commit)
        controls.addStretch()
        self.layout().addLayout(controls)
        controller.workspace_changed.connect(self.refresh_destinations)
        self.model.dataChanged.connect(self._revalidate_edited_rows)
        self.refresh_destinations()

    def refresh_destinations(self) -> None:
        current = self.destination.currentData()
        self.destination.clear()
        for source in self.controller.sources.values():
            if source.roles.intersection({FolderRole.DESTINATION, FolderRole.ARCHIVE}):
                self.destination.addItem(f"{source.name} — {source.path}", source.id)
        if current:
            index = self.destination.findData(current)
            if index >= 0:
                self.destination.setCurrentIndex(index)

    def generate(self) -> None:
        destination_id = self.destination.currentData()
        if not destination_id:
            QMessageBox.warning(
                self,
                "Destination required",
                "Configure a reachable source with Destination or Archive role first.",
            )
            return
        rows: list[dict[str, Any]] = []
        for item in self.controller.items:
            source = self.controller.sources.get(item["root_id"])
            if not source or FolderRole.INBOX not in source.roles or source.id == destination_id:
                continue
            if item.get("is_dir") and not item.get("is_project_root"):
                continue
            filename = Path(item["relative_path"]).name
            rows.append(
                {
                    "selected": True,
                    "status": "proposed",
                    "source": str((source.path / item["relative_path"]).parent),
                    "destination": "",
                    "filename": filename,
                    "projected": filename,
                    "conflicts": "",
                    "reason": "Inbox item to eligible destination",
                    "root_id": source.id,
                    "destination_root_id": destination_id,
                    "relative_path": item["relative_path"],
                    "item_id": item["id"],
                    "is_dir": item.get("is_dir", False),
                    "is_project_root": item.get("is_project_root", False),
                    "inside_protected_project": item.get("inside_protected_project", False),
                    "is_placeholder": item.get("is_placeholder", False),
                    "size": item.get("size", 0),
                }
            )
        self._revalidate_rows(rows, initialize=True)

    def _revalidate_edited_rows(self, *_args: Any) -> None:
        self._revalidate_rows(self.model.rows, initialize=False)

    def _revalidate_rows(self, rows: list[dict[str, Any]], *, initialize: bool) -> None:
        existing = {
            str(root_id): {
                str(item["relative_path"])
                for item in self.controller.items
                if item["root_id"] == root_id
            }
            for root_id in self.controller.sources
        }
        folders = {
            str(root_id): {
                "",
                *(
                    str(item["relative_path"])
                    for item in self.controller.items
                    if item["root_id"] == root_id and item.get("is_dir")
                ),
            }
            for root_id in self.controller.sources
        }
        protected = {
            str(root_id): {
                str(item["relative_path"])
                for item in self.controller.items
                if item["root_id"] == root_id and item.get("is_project_root")
            }
            for root_id in self.controller.sources
        }
        for root_id in self.controller.sources:
            if any(
                item["root_id"] == root_id
                and item.get("inside_protected_project")
                and not item.get("protected_project_path")
                for item in self.controller.items
            ):
                protected[str(root_id)].add("")
        case_sensitive = {
            root_id: bool(source.capabilities and source.capabilities.case_sensitive)
            for root_id, source in self.controller.sources.items()
        }
        candidates = [
            MoveCandidate(
                str(row["item_id"]),
                str(row["root_id"]),
                str(row["destination_root_id"]),
                str(row["relative_path"]),
                str(row.get("destination", "")),
                str(row["filename"]),
                bool(row.get("is_dir")),
                bool(row.get("is_project_root")),
                bool(row.get("inside_protected_project")),
            )
            for row in rows
            if row.get("selected")
        ]
        projections = ProjectedMoveValidator().validate(
            candidates, existing, folders, protected, case_sensitive
        )
        for row in rows:
            item_id = str(row["item_id"])
            projection = projections.get(item_id)
            if projection is None:
                candidate = MoveCandidate(
                    item_id,
                    str(row["root_id"]),
                    str(row["destination_root_id"]),
                    str(row["relative_path"]),
                    str(row.get("destination", "")),
                    str(row["filename"]),
                    bool(row.get("is_dir")),
                    bool(row.get("is_project_root")),
                    bool(row.get("inside_protected_project")),
                )
                projection = ProjectedMoveValidator().validate(
                    [candidate], existing, folders, protected, case_sensitive
                )[item_id]
            row["projected"] = projection.target_relative_path
            row["conflicts"] = "; ".join(projection.issues)
            row["status"] = "blocked" if projection.issues else "proposed"
            if initialize:
                row["selected"] = not projection.issues
        self.model.set_rows(rows)

    def commit_selected(self) -> None:
        count = sum(bool(row.get("selected")) for row in self.model.rows)
        if not count:
            QMessageBox.information(self, "Nothing selected", "Select one or more moves.")
            return
        duplicate_bytes = 0
        for row in self.model.rows:
            if not row.get("selected"):
                continue
            source = self.controller.sources[str(row["root_id"])]
            destination = self.controller.sources[str(row["destination_root_id"])]
            if (
                source.capabilities
                and destination.capabilities
                and source.capabilities.volume_id != destination.capabilities.volume_id
            ):
                duplicate_bytes += int(row.get("size", 0))
        answer = QMessageBox.warning(
            self,
            "Explicit move commit",
            f"Commit {count} selected move(s)? Cross-volume items use copy, verify, and finalize; "
            "originals enter indefinite quarantine.\n"
            f"Required cross-volume duplicate space: {duplicate_bytes:,} bytes.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            compiled = self.guidance.compile_current("Selected move rows are untrusted evidence.")
            task = BackgroundTaskDialog(
                "Applying move batch",
                f"Preflighting, copying where needed, and verifying {count:,} selected move(s)…",
                lambda: self.controller.execute_move_rows(self.model.rows, compiled.digest),
                self,
            )
            if task.run() != QDialog.DialogCode.Accepted:
                raise RuntimeError(task.error_message or "Move batch did not finish")
            completed = int(task.result_value)
            QMessageBox.information(self, "Moves verified", f"Verified {completed} move(s).")
            self.generate()
        except Exception as error:
            QMessageBox.critical(self, "Move plan not committed", str(error))


class CleanupPage(ReviewPage):
    def __init__(self, controller: WorkspaceController) -> None:
        super().__init__(
            "Cleanup",
            "cleanup",
            [
                ("selected", "Quarantine"),
                ("status", "Status"),
                ("kind", "Category"),
                ("path", "Current path"),
                ("size", "Total bytes"),
                ("items", "Items"),
                ("derivation", "Derivation"),
                ("regeneration", "Regeneration evidence"),
                ("exclusions", "Exclusions"),
                ("destination", "Destination"),
            ],
            controller,
        )
        note = QLabel(
            "Cleanup never permanently deletes. Selected items move to an AIOrganizer quarantine "
            "inside their source root and remain restorable. Build artifacts are never selected "
            "automatically."
        )
        note.setWordWrap(True)
        self.layout().insertWidget(2, note)
        controls = QHBoxLayout()
        analyze = QPushButton("Analyze cleanup evidence")
        analyze.clicked.connect(self.generate)
        commit = QPushButton("Freeze, confirm & quarantine selected…")
        commit.clicked.connect(self.commit_selected)
        restore = QPushButton("Restore latest cleanup quarantine…")
        restore.clicked.connect(self.restore_latest)
        controls.addWidget(analyze)
        controls.addWidget(commit)
        controls.addWidget(restore)
        controls.addStretch()
        self.layout().addLayout(controls)

    def generate(self) -> None:
        task = BackgroundTaskDialog(
            "Analyzing cleanup evidence",
            "Reviewing project boundaries, regeneration evidence, exclusions, and completed moves…",
            self.controller.cleanup_candidates,
            self,
        )
        try:
            if task.run() != QDialog.DialogCode.Accepted:
                raise RuntimeError(task.error_message or "Cleanup analysis did not finish")
            self.model.set_rows(list(task.result_value))
        except Exception as error:
            QMessageBox.critical(self, "Cleanup analysis failed safely", str(error))

    def commit_selected(self) -> None:
        selected = [row for row in self.model.rows if row.get("selected")]
        if not selected:
            QMessageBox.information(self, "Nothing selected", "Select cleanup candidates first.")
            return
        total_size = sum(int(row.get("size", 0)) for row in selected)
        total_items = sum(int(row.get("items", 0)) for row in selected)
        phrase, accepted = QInputDialog.getText(
            self,
            "Cleanup quarantine confirmation",
            f"{len(selected)} candidate(s), {total_items:,} item(s), {total_size:,} bytes will "
            "move to restorable quarantine. Nothing is permanently deleted.\n\n"
            "Type QUARANTINE to continue:",
        )
        if not accepted or phrase.strip() != "QUARANTINE":
            return
        try:
            compiled = self.guidance.compile_current(
                "Cleanup evidence and selected paths are untrusted until local preflight."
            )
            task = BackgroundTaskDialog(
                "Applying cleanup quarantine batch",
                f"Revalidating and moving {len(selected):,} candidate(s) to restorable quarantine…",
                lambda: self.controller.execute_cleanup_rows(self.model.rows, compiled.digest),
                self,
            )
            if task.run() != QDialog.DialogCode.Accepted:
                raise RuntimeError(task.error_message or "Cleanup batch did not finish")
            count = int(task.result_value)
            QMessageBox.information(
                self,
                "Cleanup quarantined",
                f"Verified {count} cleanup candidate(s) in restorable quarantine.",
            )
            self.generate()
        except Exception as error:
            QMessageBox.critical(self, "Cleanup not applied", str(error))

    def restore_latest(self) -> None:
        answer = QMessageBox.question(
            self,
            "Restore cleanup quarantine",
            "Restore the latest completed cleanup batch to its original paths? Restore stops "
            "safely if any original path is now occupied.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            task = BackgroundTaskDialog(
                "Restoring cleanup quarantine",
                "Revalidating original paths and restoring the latest cleanup batch…",
                self.controller.restore_last_cleanup,
                self,
            )
            if task.run() != QDialog.DialogCode.Accepted:
                raise RuntimeError(task.error_message or "Cleanup restore did not finish")
            count = int(task.result_value)
            QMessageBox.information(
                self, "Cleanup restored", f"Restored {count} cleanup candidate(s)."
            )
            self.generate()
        except Exception as error:
            QMessageBox.critical(self, "Cleanup not restored", str(error))


class SeriesReviewDialog(QDialog):
    def __init__(self, payload: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review recurring series")
        self.resize(560, 420)
        layout = QVBoxLayout(self)
        notice = QLabel(
            "Confirm or correct every field before tracking. Account identifiers must remain "
            "masked; membership evidence is preserved with the series."
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        form = QFormLayout()
        self.name = QLineEdit(str(payload.get("name", "")))
        self.issuer = QLineEdit(str(payload.get("issuer", "")))
        self.document_type = QLineEdit(str(payload.get("document_type", "")))
        self.account = QLineEdit(str(payload.get("masked_account_id", payload.get("account", ""))))
        self.cadence = QComboBox()
        for value in Cadence:
            self.cadence.addItem(value.value.title(), value.value)
        cadence_index = self.cadence.findData(str(payload.get("cadence", "monthly")))
        self.cadence.setCurrentIndex(max(0, cadence_index))
        observations = payload.get("observations", [])
        periods = sorted(str(value["period_start"]) for value in observations)
        self.start = QLineEdit(str(payload.get("start_period") or (periods[0] if periods else "")))
        self.end = QLineEdit(str(payload.get("end_period") or ""))
        self.grace = QSpinBox()
        self.grace.setRange(0, 180)
        self.grace.setValue(int(payload.get("grace_days", 14)))
        form.addRow("Series name", self.name)
        form.addRow("Issuer/entity", self.issuer)
        form.addRow("Document type", self.document_type)
        form.addRow("Masked account suffix", self.account)
        form.addRow("Cadence", self.cadence)
        form.addRow("Start period (YYYY-MM-DD)", self.start)
        form.addRow("Optional end period", self.end)
        form.addRow("Grace days", self.grace)
        layout.addLayout(form)
        self.members = QTreeWidget()
        self.members.setHeaderLabels(["Use", "Period", "Document", "Membership evidence"])
        configure_data_tree(self.members)
        self.members.setMaximumHeight(150)
        for value in observations:
            item = QTreeWidgetItem(
                [
                    "",
                    str(value["period_start"]),
                    str(value.get("relative_path") or value["item_id"]),
                    "; ".join(value.get("evidence", [])),
                ]
            )
            item.setCheckState(0, Qt.CheckState.Checked)
            item.setData(0, Qt.ItemDataRole.UserRole, value)
            self.members.addTopLevelItem(item)
        layout.addWidget(QLabel("Reviewed membership — uncheck false groupings"))
        layout.addWidget(self.members)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> dict[str, Any]:
        return {
            "name": self.name.text().strip(),
            "issuer": self.issuer.text().strip(),
            "document_type": self.document_type.text().strip(),
            "masked_account_id": self.account.text().strip(),
            "cadence": str(self.cadence.currentData()),
            "start_period": self.start.text().strip(),
            "end_period": self.end.text().strip() or None,
            "grace_days": self.grace.value(),
        }

    def observations(self) -> list[dict[str, Any]]:
        return [
            self.members.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole)
            for index in range(self.members.topLevelItemCount())
            if self.members.topLevelItem(index).checkState(0) == Qt.CheckState.Checked
        ]


class RecurrencesPage(QWidget):
    def __init__(self, controller: WorkspaceController) -> None:
        super().__init__()
        self.controller = controller
        layout = QVBoxLayout(self)
        title = QLabel("Recurrences")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        notice = QLabel(
            "Series candidates are suggestions only. Tracking starts after review. Every expected "
            "period has an explanation and can be dismissed individually. Attachment matching is "
            "metadata-only; this page cannot download anything."
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)

        candidate_controls = QHBoxLayout()
        discover = QPushButton("Discover series candidates")
        discover.clicked.connect(self.discover)
        review = QPushButton("Review & track selected candidate…")
        review.clicked.connect(self.review_candidate)
        candidate_controls.addWidget(discover)
        candidate_controls.addWidget(review)
        candidate_controls.addStretch()
        layout.addLayout(candidate_controls)
        self.candidate_model = DictTableModel(
            [
                ("name", "Candidate"),
                ("issuer", "Issuer"),
                ("document_type", "Type"),
                ("account", "Masked account"),
                ("cadence", "Detected cadence"),
                ("confidence", "Cadence confidence"),
                ("documents", "Documents"),
                ("periods", "Periods"),
                ("rationale", "Why grouped"),
            ]
        )
        self.candidate_table = QTableView()
        self.candidate_table.setModel(self.candidate_model)
        configure_data_table(self.candidate_table)

        matrix_widget = QWidget()
        matrix_layout = QVBoxLayout(matrix_widget)
        series_controls = QHBoxLayout()
        series_controls.addWidget(QLabel("Reviewed series"))
        self.series = QComboBox()
        self.series.currentIndexChanged.connect(self.refresh_matrix)
        edit = QPushButton("Edit series…")
        edit.clicked.connect(self.edit_series)
        series_controls.addWidget(self.series, 1)
        series_controls.addWidget(edit)
        matrix_layout.addLayout(series_controls)
        self.gap_model = DictTableModel(
            [
                ("period_label", "Period"),
                ("status", "Coverage"),
                ("due_date", "Grace deadline"),
                ("item_count", "Documents"),
                ("explanation", "Explanation"),
            ]
        )
        self.gap_table = QTableView()
        self.gap_table.setModel(self.gap_model)
        configure_data_table(self.gap_table)
        matrix_layout.addWidget(self.gap_table, 1)
        gap_controls = QHBoxLayout()
        ignore = QPushButton("Ignore selected gap…")
        ignore.clicked.connect(lambda: self.dismiss_gap(GapStatus.IGNORED))
        skip = QPushButton("Mark not applicable…")
        skip.clicked.connect(lambda: self.dismiss_gap(GapStatus.SKIPPED))
        clear = QPushButton("Clear selected exception")
        clear.clicked.connect(self.clear_exception)
        gap_controls.addWidget(ignore)
        gap_controls.addWidget(skip)
        gap_controls.addWidget(clear)
        gap_controls.addStretch()
        matrix_layout.addLayout(gap_controls)

        splitter = QSplitter()
        candidate_widget = QWidget()
        candidate_layout = QVBoxLayout(candidate_widget)
        candidate_layout.setContentsMargins(0, 0, 0, 0)
        candidate_layout.addWidget(QLabel("Unreviewed candidates"))
        candidate_layout.addWidget(self.candidate_table)
        splitter.addWidget(candidate_widget)
        splitter.addWidget(matrix_widget)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)
        controller.workspace_changed.connect(self.refresh_series)
        controller.recurrence_changed.connect(self.refresh_series)
        self.refresh_series()

    def discover(self) -> None:
        task = BackgroundTaskDialog(
            "Discovering recurring series",
            "Grouping period-bearing metadata and calculating cadence confidence…",
            self.controller.discover_recurrence_candidates,
            self,
        )
        try:
            if task.run() != QDialog.DialogCode.Accepted:
                raise RuntimeError(task.error_message or "Series discovery did not finish")
            rows = list(task.result_value)
            self.candidate_model.set_rows(rows)
            if rows:
                self.candidate_table.selectRow(0)
            else:
                QMessageBox.information(
                    self,
                    "No candidates",
                    "No repeated, period-bearing document pattern has enough evidence yet. "
                    "Names such as 'Issuer Statement 2026-01.pdf' can be discovered; extracted "
                    "reviewed period evidence is preferred when available.",
                )
        except Exception as error:
            QMessageBox.critical(self, "Series discovery failed", str(error))

    def review_candidate(self) -> None:
        row = self.candidate_model.row(self.candidate_table.currentIndex())
        if not row:
            QMessageBox.information(self, "Select a candidate", "Select one candidate first.")
            return
        dialog = SeriesReviewDialog(row, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            reviewed = {**row, "observations": dialog.observations()}
            self.controller.save_reviewed_series(reviewed, **dialog.values())
            self.refresh_series()
        except Exception as error:
            QMessageBox.critical(self, "Series not saved", str(error))

    def refresh_series(self) -> None:
        current = self.series.currentData()
        self.series.blockSignals(True)
        self.series.clear()
        for payload in self.controller.recurrence_series():
            self.series.addItem(str(payload["name"]), str(payload["id"]))
        if current:
            index = self.series.findData(current)
            if index >= 0:
                self.series.setCurrentIndex(index)
        self.series.blockSignals(False)
        self.refresh_matrix()

    def refresh_matrix(self) -> None:
        series_id = self.series.currentData()
        if not series_id:
            self.gap_model.set_rows([])
            return
        try:
            rows = self.controller.recurrence_gap_rows(str(series_id))
            for row in rows:
                row["status"] = str(row["status"])
                row["item_count"] = len(row.get("item_ids", []))
            self.gap_model.set_rows(rows)
        except Exception as error:
            QMessageBox.critical(self, "Gap matrix unavailable", str(error))

    def edit_series(self) -> None:
        series_id = self.series.currentData()
        payload = next(
            (
                value
                for value in self.controller.recurrence_series()
                if str(value["id"]) == str(series_id)
            ),
            None,
        )
        if not payload:
            return
        dialog = SeriesReviewDialog(payload, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            reviewed = {**payload, "observations": dialog.observations()}
            self.controller.save_reviewed_series(
                reviewed,
                **dialog.values(),
                series_id=str(payload["id"]),
                revision=int(payload.get("revision", 1)) + 1,
            )
        except Exception as error:
            QMessageBox.critical(self, "Series not updated", str(error))

    def dismiss_gap(self, status: GapStatus) -> None:
        row = self.gap_model.row(self.gap_table.currentIndex())
        series_id = self.series.currentData()
        if not row or not series_id:
            QMessageBox.information(self, "Select a period", "Select one gap period first.")
            return
        if row.get("item_ids"):
            QMessageBox.information(
                self, "Period has documents", "Only uncovered periods can be dismissed."
            )
            return
        reason, accepted = QInputDialog.getText(
            self,
            "Explain recurrence exception",
            "Reason this period should be ignored or treated as not applicable:",
        )
        if not accepted:
            return
        try:
            self.controller.set_recurrence_exception(
                str(series_id), str(row["period_start"]), status.value, reason
            )
            self.refresh_matrix()
        except Exception as error:
            QMessageBox.critical(self, "Exception not saved", str(error))

    def clear_exception(self) -> None:
        row = self.gap_model.row(self.gap_table.currentIndex())
        series_id = self.series.currentData()
        if not row or not series_id:
            return
        try:
            self.controller.clear_recurrence_exception(str(series_id), str(row["period_start"]))
            self.refresh_matrix()
        except Exception as error:
            QMessageBox.critical(self, "Exception not cleared", str(error))


class FocusedActionsPage(ReviewPage):
    def __init__(self, controller: WorkspaceController) -> None:
        super().__init__(
            "Focused Actions",
            "action",
            [
                ("title", "Finding"),
                ("item_id", "Item"),
                ("severity", "Severity"),
                ("confidence", "Confidence"),
                ("rationale", "Rationale"),
            ],
            controller,
        )
        controls = QHBoxLayout()
        self.actions = QComboBox()
        self._presets = builtin_actions()
        self.actions.addItems([preset.name for preset in self._presets])
        run = QPushButton("Run…")
        run.clicked.connect(self.run_action)
        create = QPushButton("New custom action…")
        create.clicked.connect(self.create_action)
        controls.addWidget(self.actions)
        controls.addWidget(run)
        controls.addWidget(create)
        controls.addStretch()
        self.layout().addLayout(controls)
        controller.workspace_changed.connect(self.load_actions)

    def load_actions(self) -> None:
        if not self.controller.store:
            return
        presets: list[ActionPreset] = []
        for payload in self.controller.store.list_action_payloads():
            presets.append(
                ActionPreset(
                    payload["name"],
                    payload["description"],
                    [
                        ActionFilter(
                            condition["field"],
                            FilterOperator(condition["operator"]),
                            condition.get("value"),
                        )
                        for condition in payload.get("filters", [])
                    ],
                    payload.get("guidance", ""),
                    id=payload["id"],
                    security_oriented=payload.get("security_oriented", False),
                    default_output=ActionOutputMode(payload.get("default_output", "findings")),
                    max_results=int(payload.get("max_results", 500)),
                    allowed_destination_category_ids=set(
                        payload.get("allowed_destination_category_ids", [])
                    ),
                    builtin=payload.get("builtin", False),
                    revision=int(payload.get("revision", 1)),
                )
            )
        self._presets = presets or builtin_actions()
        self.actions.clear()
        self.actions.addItems([preset.name for preset in self._presets])

    def create_action(self) -> None:
        if not self.controller.store:
            QMessageBox.warning(self, "Workspace required", "Open a workspace first.")
            return
        dialog = CustomActionDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            preset = dialog.preset()
            ActionEngine().validate(preset)
            self.controller.store.save_action(preset)
            self._presets.append(preset)
            self.actions.addItem(preset.name)
            self.actions.setCurrentIndex(len(self._presets) - 1)
        except Exception as error:
            QMessageBox.critical(self, "Invalid action", str(error))

    def run_action(self) -> None:
        preset = self._presets[self.actions.currentIndex()]
        category_payloads = (
            self.controller.store.list_category_payloads() if self.controller.store else []
        )
        dialog = ActionRunDialog(preset.security_oriented, category_payloads, preset.revision, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        run = ActionRun(
            preset.id,
            preset.revision,
            dialog.output_mode(),
            dialog.scope.currentText(),
            dialog.limit.value(),
        )
        normalized = []
        category_names = {value["id"]: value["name"] for value in category_payloads}
        for item in self.controller.items:
            record = dict(item)
            source = self.controller.sources.get(record["root_id"])
            record["category_ids"] = (
                [
                    value
                    for category_id in source.category_ids
                    for value in (category_id, category_names.get(category_id, category_id))
                ]
                if source
                else []
            )
            record["roles"] = [role.value for role in source.roles] if source else []
            folded_path = str(record["relative_path"]).casefold()
            sensitive_name = any(
                token in folded_path
                for token in ("passport", "identity", "health", "bank", "tax", "payroll")
            )
            record["sensitivity"] = "confidential" if sensitive_name else "normal"
            record["secret_like"] = self._has_secret_evidence(record, source)
            record.setdefault("confidence", 0.5)
            record["extension"] = Path(record["relative_path"]).suffix.casefold()
            record["project_status"] = (
                "project_root" if record.get("is_project_root") else "regular"
            )
            normalized.append(record)
        scope = dialog.scope.currentText()
        if scope == "current selection":
            normalized = [
                item for item in normalized if str(item["id"]) in self.controller.selected_item_ids
            ]
        elif scope == "selected roots":
            normalized = [
                item
                for item in normalized
                if str(item["root_id"]) in self.controller.selected_root_ids
            ]
        elif scope == "selected folders":
            normalized = [
                item
                for item in normalized
                if any(
                    str(item["root_id"]) == root_id
                    and (
                        str(item["relative_path"]) == folder
                        or str(item["relative_path"]).startswith(folder.rstrip("/") + "/")
                    )
                    for root_id, folder in self.controller.selected_folders
                )
            ]
        selected_category = dialog.category.currentData()
        selected_sensitivity = dialog.sensitivity.currentData()
        if selected_category:
            normalized = [item for item in normalized if selected_category in item["category_ids"]]
        if selected_sensitivity:
            normalized = [
                item for item in normalized if item["sensitivity"] == selected_sensitivity
            ]
        evaluation = BackgroundTaskDialog(
            "Evaluating focused action",
            f"Evaluating {len(normalized):,} in-scope inventory record(s)…",
            lambda: ActionEngine().evaluate(preset, normalized, run),
            self,
        )
        if evaluation.run() != QDialog.DialogCode.Accepted:
            QMessageBox.critical(
                self,
                "Focused action failed safely",
                evaluation.error_message or "Evaluation did not finish",
            )
            return
        findings = evaluation.result_value
        provider_name = dialog.provider.currentText().casefold().replace(" only", "")
        if provider_name != "local" and findings.findings:
            finding_ids = {finding.item_id for finding in findings.findings}
            scoped = [item for item in normalized if str(item["id"]) in finding_ids]
            for root_id in {str(item["root_id"]) for item in scoped}:
                allowed, reason = self.controller.cloud_allowed(root_id)
                if not allowed:
                    QMessageBox.critical(self, "Cloud action blocked by policy", reason)
                    return
            model = {
                "openai": "gpt-5.6-terra",
                "anthropic": "claude-sonnet-5",
                "codex": "user-default",
            }[provider_name]
            import json

            compiled = PromptCompiler().compile(
                provider=provider_name,
                model=model,
                action=PromptRevision(
                    f"action:{preset.id}", PromptLayerKind.ACTION, preset.guidance
                ),
                evidence=json.dumps(
                    [
                        {
                            "item_id": item["id"],
                            "relative_path": item["relative_path"],
                            "mime_type": item["mime_type"],
                            "sensitivity": item["sensitivity"],
                        }
                        for item in scoped
                    ],
                    ensure_ascii=False,
                ),
            )
            answer = QMessageBox.warning(
                self,
                "Confirm focused cloud analysis",
                f"Provider: {provider_name}\nModel: {model}\nItems: {len(scoped)}\n"
                f"Redacted evidence: {compiled.evidence_bytes:,} bytes\n"
                f"Estimated input: {len(compiled.text) // 4:,} tokens",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            try:
                task = BackgroundTaskDialog(
                    "Refining focused findings",
                    f"Waiting for structured AI review of {len(scoped):,} finding(s)…",
                    lambda: _provider_for(self.controller, provider_name, model).analyze(compiled),
                    self,
                )
                if task.run() != QDialog.DialogCode.Accepted:
                    raise RuntimeError(task.error_message or "AI refinement did not finish")
                result = task.result_value
            except Exception as error:
                QMessageBox.critical(self, "Cloud action failed safely", str(error))
                return
            enrichment = {
                str(value.get("item_id")): value
                for value in result.findings
                if str(value.get("item_id")) in finding_ids
            }
            findings = replace(
                findings,
                findings=tuple(
                    replace(
                        finding,
                        rationale=str(
                            enrichment.get(finding.item_id, {}).get("rationale", finding.rationale)
                        ),
                        confidence=float(
                            enrichment.get(finding.item_id, {}).get(
                                "confidence", finding.confidence
                            )
                        ),
                    )
                    for finding in findings.findings
                ),
            )
            run = replace(
                run,
                provider=provider_name,
                model=model,
                prompt_hash=compiled.digest,
            )
        proposal_id = self.controller.save_action_result(run, findings)
        self.model.set_rows(
            [
                {
                    "title": finding.title,
                    "item_id": finding.item_id,
                    "severity": finding.severity,
                    "confidence": finding.confidence,
                    "rationale": finding.rationale,
                }
                for finding in findings.findings
            ]
        )
        if proposal_id:
            QMessageBox.information(
                self,
                "Move proposal created",
                f"Created proposal set {proposal_id}. It remains unaccepted and requires normal Move review.",
            )

    @staticmethod
    def _has_secret_evidence(record: dict[str, Any], source: Any) -> bool:
        name = Path(record["relative_path"]).name.casefold()
        if any(
            token in name
            for token in ("id_rsa", ".pem", ".key", ".env", "credential", "secret", "token")
        ):
            return True
        if not source or record.get("is_dir") or record.get("is_placeholder"):
            return False
        path = source.path / record["relative_path"]
        if path.suffix.casefold() not in {
            ".txt",
            ".env",
            ".ini",
            ".cfg",
            ".conf",
            ".json",
            ".yaml",
            ".yml",
            ".toml",
            ".pem",
            ".key",
        }:
            return False
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                return bool(detect_secret_kinds(stream.read(256_000)))
        except OSError:
            return False


class ActionRunDialog(QDialog):
    def __init__(
        self,
        security_oriented: bool,
        categories: list[dict[str, Any]],
        action_revision: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Run focused action")
        form = QFormLayout(self)
        self.scope = QComboBox()
        self.scope.addItems(
            ["workspace", "selected roots", "selected folders", "current selection"]
        )
        self.output = QComboBox()
        self.output.addItems(["findings", "move proposals"])
        if security_oriented:
            self.output.setCurrentText("findings")
        self.provider = QComboBox()
        self.provider.addItems(["local only", "OpenAI", "Anthropic", "Codex"])
        self.category = QComboBox()
        self.category.addItem("Any category", None)
        for category in categories:
            self.category.addItem(category["name"], category["id"])
        self.sensitivity = QComboBox()
        self.sensitivity.addItem("Any sensitivity", None)
        for value in ("normal", "confidential", "restricted"):
            self.sensitivity.addItem(value.title(), value)
        self.limit = QSpinBox()
        self.limit.setRange(1, 5_000)
        self.limit.setValue(500)
        form.addRow("Scope", self.scope)
        form.addRow("Output", self.output)
        form.addRow("Analysis", self.provider)
        form.addRow("Category filter", self.category)
        form.addRow("Sensitivity filter", self.sensitivity)
        form.addRow("Maximum results", self.limit)
        form.addRow("Action revision", QLabel(str(action_revision)))
        warning = QLabel(
            "Move proposals still require normal review, freeze, preflight, and commit."
        )
        warning.setWordWrap(True)
        form.addRow(warning)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def output_mode(self) -> ActionOutputMode:
        return (
            ActionOutputMode.FINDINGS
            if self.output.currentIndex() == 0
            else ActionOutputMode.MOVE_PROPOSALS
        )


class CustomActionDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New structured focused action")
        form = QFormLayout(self)
        self.name = QLineEdit()
        self.description = QLineEdit()
        self.guidance = QLineEdit()
        self.field = QComboBox()
        self.field.addItems(sorted(ActionEngine.ALLOWED_FIELDS))
        self.operator = QComboBox()
        self.operator.addItems([operator.value for operator in FilterOperator])
        self.value = QLineEdit()
        self.value.setPlaceholderText("JSON scalar/list, or plain text")
        self.security = QCheckBox("Security-oriented; default to findings only")
        form.addRow("Name", self.name)
        form.addRow("Description", self.description)
        form.addRow("Guidance", self.guidance)
        form.addRow("Predicate field", self.field)
        form.addRow("Operator", self.operator)
        form.addRow("Value", self.value)
        form.addRow(self.security)
        note = QLabel(
            "Predicates are data only. Scripts, callables, and filesystem expressions are not accepted."
        )
        note.setWordWrap(True)
        form.addRow(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def preset(self) -> ActionPreset:
        import json

        raw = self.value.text().strip()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        return ActionPreset(
            self.name.text().strip(),
            self.description.text().strip(),
            [
                ActionFilter(
                    self.field.currentText(), FilterOperator(self.operator.currentText()), value
                )
            ],
            self.guidance.text().strip(),
            security_oriented=self.security.isChecked(),
        )


class ActivityPage(QWidget):
    def __init__(self, controller: WorkspaceController) -> None:
        super().__init__()
        self.controller = controller
        layout = QVBoxLayout(self)
        title = QLabel("Activity")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        undo = QPushButton("Undo last verified filesystem commit…")
        undo.clicked.connect(self.undo_last)
        layout.addWidget(undo)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        layout.addWidget(self.list)
        controller.activity_changed.connect(self.refresh)
        controller.workspace_changed.connect(self.refresh)

    def refresh(self) -> None:
        self.list.clear()
        if not self.controller.store:
            return
        for row in self.controller.store.list_activity():
            self.list.addItem(f"{row['occurred_at']}  {row['kind']}  {row['summary']}")

    def undo_last(self) -> None:
        answer = QMessageBox.warning(
            self,
            "Explicit undo",
            "Undo the latest verified filesystem commit? Current paths and hashes are checked first.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            task = BackgroundTaskDialog(
                "Undoing verified filesystem batch",
                "Revalidating current paths, applying the inverse journal, and refreshing inventory…",
                self.controller.undo_last_commit,
                self,
            )
            if task.run() != QDialog.DialogCode.Accepted:
                raise RuntimeError(task.error_message or "Undo did not finish")
            count = int(task.result_value)
            QMessageBox.information(self, "Undo verified", f"Undid {count} operation(s).")
        except Exception as error:
            QMessageBox.critical(self, "Undo stopped safely", str(error))


class SettingsPage(QWidget):
    def __init__(self, controller: WorkspaceController) -> None:
        super().__init__()
        self.controller = controller
        layout = QVBoxLayout(self)
        title = QLabel("Settings")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        intro = QLabel(
            "Configure reusable AI guidance here. Working pages only select their runtime context, "
            "leaving their main area available for lists and previews."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)

        general_tab = QWidget()
        general_layout = QVBoxLayout(general_tab)
        workspace_group = QGroupBox("Workspace AI guidance")
        workspace_layout = QVBoxLayout(workspace_group)
        self.workspace_guidance = QPlainTextEdit()
        self.workspace_guidance.setPlaceholderText(
            "General organization vocabulary, hierarchy, and ambiguity preferences…"
        )
        workspace_layout.addWidget(self.workspace_guidance)
        save_workspace = QPushButton("Save workspace guidance revision")
        save_workspace.clicked.connect(self.save_workspace_guidance)
        workspace_layout.addWidget(save_workspace)
        general_layout.addWidget(workspace_group, 1)
        metadata_group = QGroupBox("Metadata validation")
        metadata_layout = QFormLayout(metadata_group)
        self.metadata_fingerprint = QComboBox()
        self.metadata_fingerprint.addItem(
            "Disabled — trust size and modified time (recommended)", "none"
        )
        self.metadata_fingerprint.addItem("CRC32 — reread every file during validation", "crc32")
        self.metadata_fingerprint.addItem(
            "SHA-256 — strongest, reread every file during validation", "sha256"
        )
        self.metadata_fingerprint.currentIndexChanged.connect(self.save_metadata_fingerprint_policy)
        metadata_layout.addRow("Optional content fingerprint", self.metadata_fingerprint)
        metadata_note = QLabel(
            "Metadata has no age-based expiry. Normal scans reuse it when size and modification "
            "time match. CRC32/SHA-256 are opt-in because validating them requires reading every file."
        )
        metadata_note.setWordWrap(True)
        metadata_layout.addRow(metadata_note)
        general_layout.addWidget(metadata_group)
        self.tabs.addTab(general_tab, "General")

        accessibility_tab = QWidget()
        accessibility_layout = QFormLayout(accessibility_tab)
        self.interface_locale = QComboBox()
        self.interface_locale.addItem("System language", "system")
        self.interface_locale.addItem("English", "en_US")
        self.text_scale = QSpinBox()
        self.text_scale.setRange(90, 180)
        self.text_scale.setSuffix("%")
        self.high_contrast = QCheckBox("Use a high-contrast application palette")
        accessibility_layout.addRow("Interface language", self.interface_locale)
        accessibility_layout.addRow("Text scale", self.text_scale)
        accessibility_layout.addRow(self.high_contrast)
        accessibility_note = QLabel(
            "English is the fallback language. Additional human-reviewed Qt translation catalogs "
            "can be installed without changing application logic. Text scaling and contrast apply "
            "immediately; translated interface strings apply fully after restart."
        )
        accessibility_note.setWordWrap(True)
        accessibility_layout.addRow(accessibility_note)
        save_accessibility = QPushButton("Apply accessibility and language preferences")
        save_accessibility.clicked.connect(self.save_accessibility_preferences)
        accessibility_layout.addRow(save_accessibility)
        self.tabs.addTab(accessibility_tab, "Accessibility & Language")

        self.guidance_panels: dict[str, GuidancePanel] = {}
        for view_key, label in (
            ("audit", "Audit"),
            ("rename", "Rename"),
            ("folder", "Folder Plan"),
            ("move", "Move"),
            ("action", "Focused Actions"),
            ("cleanup", "Cleanup"),
            ("recurrence", "Recurrences"),
            ("updates", "Updates"),
        ):
            tab = QWidget()
            tab_layout = QVBoxLayout(tab)
            if view_key == "folder":
                depth_group = QGroupBox("Folder hierarchy depth policy")
                depth_layout = QFormLayout(depth_group)
                self.folder_preferred_depth = QSpinBox()
                self.folder_preferred_depth.setRange(1, 12)
                self.folder_maximum_depth = QSpinBox()
                self.folder_maximum_depth.setRange(1, 12)
                self.folder_preferred_depth.valueChanged.connect(
                    self.folder_maximum_depth.setMinimum
                )
                self.folder_adaptive_depth = QCheckBox(
                    "Let AI recommend a shallower depth from source size and distribution"
                )
                save_depth = QPushButton("Save folder-depth policy")
                save_depth.clicked.connect(self.save_folder_depth_policy)
                depth_layout.addRow("Preferred depth", self.folder_preferred_depth)
                depth_layout.addRow("Hard maximum depth", self.folder_maximum_depth)
                depth_layout.addRow(self.folder_adaptive_depth)
                depth_layout.addRow(
                    QLabel(
                        "The maximum is a ceiling. Source and category overrides may be more restrictive; "
                        "AI may always choose fewer levels to avoid over-organization."
                    )
                )
                depth_layout.addRow(save_depth)
                tab_layout.addWidget(depth_group)
            panel = GuidancePanel(
                view_key,
                controller.save_prompt_revision,
                controller.compile_prompt,
                load_text=controller.latest_prompt_text,
                load_context=controller.ai_context,
                save_context=controller.set_ai_context,
            )
            tab_layout.addWidget(panel, 1)
            self.guidance_panels[view_key] = panel
            self.tabs.addTab(tab, label)

        provider_tab = QWidget()
        provider_layout = QVBoxLayout(provider_tab)
        provider = QLabel(
            "Provider credentials are stored in the operating-system credential store. "
            "For development, ignored .env values are used only when no stored credential exists. "
            "Cloud analysis remains disabled per source until explicitly enabled."
        )
        provider.setWordWrap(True)
        provider_layout.addWidget(provider)
        credentials = QFormLayout()
        self.openai_key = QLineEdit()
        self.openai_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.deepseek_key = QLineEdit()
        self.deepseek_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.openrouter_key = QLineEdit()
        self.openrouter_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.anthropic_key = QLineEdit()
        self.anthropic_key.setEchoMode(QLineEdit.EchoMode.Password)
        credentials.addRow("DeepSeek API key", self.deepseek_key)
        credentials.addRow("OpenRouter API key", self.openrouter_key)
        credentials.addRow("OpenAI API key", self.openai_key)
        credentials.addRow("Anthropic API key", self.anthropic_key)
        provider_layout.addLayout(credentials)
        controls = QHBoxLayout()
        save = QPushButton("Save keys to credential store")
        save.clicked.connect(self.save_keys)
        clear = QPushButton("Remove stored keys")
        clear.clicked.connect(self.clear_keys)
        detect = QPushButton("Detect Codex subscription runtime")
        detect.clicked.connect(self.detect_codex)
        login = QPushButton("Start Codex browser login…")
        login.clicked.connect(self.login_codex)
        controls.addWidget(save)
        controls.addWidget(clear)
        controls.addWidget(detect)
        controls.addWidget(login)
        controls.addStretch()
        provider_layout.addLayout(controls)
        provider_layout.addStretch()
        self.tabs.addTab(provider_tab, "Providers")
        layout.addWidget(self.tabs, 1)

        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        controller.workspace_changed.connect(self.refresh_guidance)
        controller.workspace_changed.connect(self.refresh_metadata_settings)
        controller.workspace_changed.connect(self.refresh_folder_depth_policy)
        controller.prompt_changed.connect(self.refresh_prompt)
        self.refresh_guidance()
        self.refresh_metadata_settings()
        self.refresh_folder_depth_policy()
        self.refresh_accessibility_preferences()

    def refresh_guidance(self) -> None:
        self.workspace_guidance.setPlainText(
            self.controller.latest_prompt_text("workspace:general")
        )
        for panel in self.guidance_panels.values():
            panel.refresh_saved()

    def refresh_prompt(self, profile_id: str) -> None:
        if profile_id == "workspace:general":
            self.workspace_guidance.setPlainText(
                self.controller.latest_prompt_text("workspace:general")
            )
            return
        view_key = profile_id.removeprefix("view:")
        panel = self.guidance_panels.get(view_key)
        if panel:
            panel.refresh_saved()

    def refresh_metadata_settings(self) -> None:
        mode = self.controller.metadata_fingerprint_mode()
        index = self.metadata_fingerprint.findData(mode)
        self.metadata_fingerprint.blockSignals(True)
        self.metadata_fingerprint.setCurrentIndex(max(0, index))
        self.metadata_fingerprint.blockSignals(False)

    def save_metadata_fingerprint_policy(self) -> None:
        if not self.controller.store:
            return
        try:
            self.controller.set_metadata_fingerprint_mode(
                str(self.metadata_fingerprint.currentData())
            )
            self.status.setText(
                "Metadata validation policy saved. Revalidate Inventory to populate new fingerprints."
            )
        except Exception as error:
            QMessageBox.critical(self, "Metadata setting not saved", str(error))

    def refresh_folder_depth_policy(self) -> None:
        if not hasattr(self, "folder_preferred_depth"):
            return
        policy = self.controller.folder_depth_policy()
        self.folder_preferred_depth.setValue(policy.preferred_depth)
        self.folder_maximum_depth.setMinimum(policy.preferred_depth)
        self.folder_maximum_depth.setValue(policy.maximum_depth)
        self.folder_adaptive_depth.setChecked(policy.adaptive)

    def save_folder_depth_policy(self) -> None:
        try:
            policy = FolderDepthPolicy(
                self.folder_preferred_depth.value(),
                self.folder_maximum_depth.value(),
                self.folder_adaptive_depth.isChecked(),
            ).validated()
            self.controller.set_folder_depth_policy(policy)
            self.status.setText(
                f"Folder depth saved: preferred {policy.preferred_depth}, hard maximum {policy.maximum_depth}."
            )
        except Exception as error:
            QMessageBox.critical(self, "Folder depth not saved", str(error))

    def refresh_accessibility_preferences(self) -> None:
        settings = QSettings("AIOrganizer", "AIOrganizer")
        locale = str(settings.value("accessibility/locale", "system"))
        index = self.interface_locale.findData(locale)
        self.interface_locale.setCurrentIndex(max(0, index))
        try:
            scale = int(settings.value("accessibility/textScale", 100))
        except (TypeError, ValueError):
            scale = 100
        self.text_scale.setValue(scale)
        self.high_contrast.setChecked(settings.value("accessibility/highContrast", False, bool))

    def save_accessibility_preferences(self) -> None:
        settings = QSettings("AIOrganizer", "AIOrganizer")
        settings.setValue("accessibility/locale", self.interface_locale.currentData())
        settings.setValue("accessibility/textScale", self.text_scale.value())
        settings.setValue("accessibility/highContrast", self.high_contrast.isChecked())
        application = QApplication.instance()
        if application:
            apply_runtime_preferences(application, settings)
        self.status.setText(
            "Accessibility preferences applied. Restart to apply a changed translation catalog everywhere."
        )

    def save_workspace_guidance(self) -> None:
        text = self.workspace_guidance.toPlainText().strip()
        try:
            PromptCompiler().validate_editable(text)
            self.controller.save_prompt_revision(
                PromptRevision("workspace:general", PromptLayerKind.WORKSPACE, text)
            )
            self.status.setText("Workspace guidance revision saved; dependent proposals are stale.")
        except Exception as error:
            QMessageBox.critical(self, "Guidance not saved", str(error))

    def save_keys(self) -> None:
        from ai_organizer.adapters.secrets import SecretStore

        store = SecretStore()
        if self.deepseek_key.text().strip():
            store.set("deepseek_api_key", self.deepseek_key.text().strip())
        if self.openai_key.text().strip():
            store.set("openai_api_key", self.openai_key.text().strip())
        if self.openrouter_key.text().strip():
            store.set("openrouter_api_key", self.openrouter_key.text().strip())
        if self.anthropic_key.text().strip():
            store.set("anthropic_api_key", self.anthropic_key.text().strip())
        self.deepseek_key.clear()
        self.openai_key.clear()
        self.openrouter_key.clear()
        self.anthropic_key.clear()
        self.status.setText("Credentials saved without writing them to the workspace or logs.")

    def clear_keys(self) -> None:
        from ai_organizer.adapters.secrets import SecretStore

        store = SecretStore()
        store.delete("deepseek_api_key")
        store.delete("openai_api_key")
        store.delete("openrouter_api_key")
        store.delete("anthropic_api_key")
        self.status.setText("Stored API credentials removed.")

    def detect_codex(self) -> None:
        from ai_organizer.adapters.providers import CodexRuntimeDetector

        runtime = CodexRuntimeDetector().detect()
        if runtime:
            self.status.setText(
                f"Codex runtime: {runtime.source}; version {runtime.version}; "
                "browser/device login is handled by the supported runtime."
            )
        else:
            self.status.setText("No compatible installed or bundled Codex runtime was detected.")

    def login_codex(self) -> None:
        from ai_organizer.adapters.providers import CodexRuntimeDetector

        runtime = CodexRuntimeDetector().detect()
        if not runtime or runtime.source != "installed":
            self.status.setText(
                "Install a compatible Codex CLI to start its supported browser/device login flow."
            )
            return
        started, _process_id = QProcess.startDetached(runtime.command[0], ["login"])
        self.status.setText(
            "Codex login started in its supported runtime. Complete the browser or device flow."
            if started
            else "Codex login could not be started."
        )


class EmailPage(QWidget):
    def __init__(self, controller: WorkspaceController) -> None:
        super().__init__()
        self.controller = controller
        layout = QVBoxLayout(self)
        title = QLabel("Email")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        self.banner = QLabel()
        self.banner.setObjectName("safetyBanner")
        self.banner.setWordWrap(True)
        layout.addWidget(self.banner)
        controls = QHBoxLayout()
        sign_in = QPushButton("Sign in read-only…")
        sign_in.clicked.connect(self.sign_in)
        sync = QPushButton("Refresh selected mailbox metadata")
        sync.clicked.connect(self.sync)
        folder = QPushButton("Propose folder…")
        folder.clicked.connect(self.propose_folder)
        move = QPushButton("Propose selected message move…")
        move.clicked.connect(self.propose_move)
        categorize = QPushButton("Propose categories…")
        categorize.clicked.connect(self.propose_categories)
        rule = QPushButton("Propose sender rule…")
        rule.clicked.connect(self.propose_rule)
        review = QPushButton("Review permissions for selected…")
        review.clicked.connect(self.review_permissions)
        apply = QPushButton("Apply selected…")
        apply.clicked.connect(self.apply_selected)
        for button in (sign_in, sync, folder, move, categorize, rule, review, apply):
            controls.addWidget(button)
        controls.addStretch()
        layout.addLayout(controls)
        self.tabs = QTabWidget()
        self.folders = QTreeWidget()
        self.folders.setHeaderLabels(["Folder", "Total", "Unread", "Folder ID"])
        configure_data_tree(self.folders)
        self.messages = QTreeWidget()
        self.messages.setHeaderLabels(["Received", "Subject", "From", "Folder", "Attachments"])
        configure_data_tree(self.messages)
        self.proposals = QTreeWidget()
        self.proposals.setHeaderLabels(["Operation", "Status", "Rationale", "Confidence"])
        configure_data_tree(self.proposals)
        install_tree_context_menu(self.proposals, self._email_proposal_context_actions)
        self.security = QTreeWidget()
        self.security.setHeaderLabels(
            ["Service", "Mailbox", "First evidence", "Last evidence", "Categories"]
        )
        configure_data_tree(self.security)
        self.attachments = QTreeWidget()
        self.attachments.setHeaderLabels(
            ["Attachment", "Type", "Bytes", "Received", "Recurrence match", "Message ID"]
        )
        configure_data_tree(self.attachments)
        self.tabs.addTab(self.folders, "Folders")
        self.tabs.addTab(self.messages, "Messages")
        self.tabs.addTab(self.proposals, "Proposals")
        self.tabs.addTab(self.security, "Accounts && Security Evidence")
        self.tabs.addTab(self.attachments, "Recurring attachment metadata")
        self.handoffs = QTreeWidget()
        self.handoffs.setHeaderLabels(
            ["Imported", "Subject", "Sender", "Attachments", "Handoff ID"]
        )
        configure_data_tree(self.handoffs)
        self.tabs.addTab(self.handoffs, "Outlook selections")
        layout.addWidget(self.tabs, 1)
        note = QLabel(
            "Message text is untrusted and stored only as a bounded, secret-redacted preview. "
            "Attachments are not downloaded. Sending, replying, forwarding, deletion, and permanent "
            "deletion are unavailable. Rules require a historical sample and separate consent."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        controller.workspace_changed.connect(self.refresh)
        self.refresh()

    def _service(self) -> EmailService:
        if not self.controller.store:
            raise RuntimeError("Open a workspace first")
        return EmailService(self.controller.store, GraphClient(UrllibGraphTransport()))

    def _interactive_token(self, scopes: tuple[str, ...]) -> tuple[MsalDeviceAuth, dict[str, Any]]:
        auth = MsalDeviceAuth()
        prompt = auth.begin_device_flow(scopes)
        if prompt.verification_uri:
            QDesktopServices.openUrl(QUrl(prompt.verification_uri))
        QMessageBox.information(self, "Microsoft device sign-in", prompt.message)
        dialog = BackgroundTaskDialog(
            "Waiting for Microsoft sign-in",
            "Complete the delegated device sign-in in your browser. AIOrganizer never receives your password.",
            lambda: auth.complete_device_flow(prompt),
            self,
        )
        if dialog.run() != QDialog.DialogCode.Accepted:
            raise RuntimeError(dialog.error_message or "Microsoft sign-in was cancelled")
        return auth, dict(dialog.result_value)

    def sign_in(self) -> None:
        try:
            auth, result = self._interactive_token(READ_SCOPES)
            token = str(result["access_token"])
            profile = GraphClient(UrllibGraphTransport()).profile(token)
            claims = result.get("id_token_claims", {})
            cached_accounts = auth.accounts()
            home_account_id = str(
                next(
                    (
                        value.get("home_account_id", "")
                        for value in cached_accounts
                        if value.get("username", "").casefold()
                        == str(
                            profile.get("userPrincipalName") or profile.get("mail") or ""
                        ).casefold()
                    ),
                    cached_accounts[0].get("home_account_id", "")
                    if len(cached_accounts) == 1
                    else "",
                )
            )
            scopes = tuple(str(result.get("scope", "")).split()) or READ_SCOPES
            account = self._service().register_active_account(
                profile,
                granted_scopes=scopes,
                home_account_id=home_account_id,
                tenant_id=str(claims.get("tid", "")),
            )
            if self.controller.store:
                self.controller.store.save_connector_source(
                    f"graph:{account.id}",
                    "microsoft_graph",
                    account.username,
                    {"account_id": account.id, "content_kind": "email", "auth": "delegated"},
                    True,
                )
            del auth
            self.refresh()
        except Exception as error:
            QMessageBox.critical(self, "Outlook sign-in failed", str(error))

    def sync(self) -> None:
        try:
            service = self._service()
            account = service.active_account()
            if not account:
                raise RuntimeError("Sign in to one mailbox first")
            auth = MsalDeviceAuth()
            token = auth.acquire_silent(READ_SCOPES, account.home_account_id)
            if not token:
                raise RuntimeError("The read token is unavailable; use Sign in read-only again")
            selected_ids = tuple(
                str(item.data(0, Qt.ItemDataRole.UserRole))
                for item in self.folders.selectedItems()
                if item.data(0, Qt.ItemDataRole.UserRole)
            )
            dialog = BackgroundTaskDialog(
                "Refreshing Outlook metadata",
                "Using per-folder delta cursors. Only bounded message and attachment metadata is read.",
                lambda: service.sync_read_only(account, token, selected_ids),
                self,
            )
            if dialog.run() != QDialog.DialogCode.Accepted:
                raise RuntimeError(dialog.error_message or "Mailbox refresh failed")
            self.refresh()
            QMessageBox.information(
                self, "Mailbox refreshed", json.dumps(dialog.result_value, indent=2)
            )
        except Exception as error:
            QMessageBox.critical(self, "Mailbox refresh failed", str(error))

    def propose_folder(self) -> None:
        try:
            account = self._active_account()
            folders = (
                self.controller.store.list_mail_folders(account.id) if self.controller.store else []
            )
            if not folders:
                raise RuntimeError("Refresh mailbox folders first")
            labels = [f"{value['display_name']} — {value['id']}" for value in folders]
            parent, accepted = QInputDialog.getItem(
                self, "Parent folder", "Create beneath", labels, 0, False
            )
            if not accepted:
                return
            name, accepted = QInputDialog.getText(self, "New mail folder", "Display name")
            if not accepted or not name.strip():
                return
            parent_id = str(folders[labels.index(parent)]["id"])
            self._service().propose(
                EmailProposal(
                    account.id,
                    EmailProposalKind.FOLDER_CREATE,
                    {"parent_folder_id": parent_id, "display_name": name.strip()},
                    {"parent_folder_id": parent_id},
                    "User-staged folder; creation remains separate from message moves.",
                    1.0,
                )
            )
            self.refresh()
        except Exception as error:
            QMessageBox.critical(self, "Folder proposal failed", str(error))

    def propose_move(self) -> None:
        try:
            account = self._active_account()
            item = self.messages.currentItem()
            message = item.data(0, Qt.ItemDataRole.UserRole) if item else None
            if not isinstance(message, dict):
                raise RuntimeError("Select a message first")
            folders = (
                self.controller.store.list_mail_folders(account.id) if self.controller.store else []
            )
            labels = [f"{value['display_name']} — {value['id']}" for value in folders]
            target, accepted = QInputDialog.getItem(
                self, "Move destination", "Destination", labels, 0, False
            )
            if not accepted:
                return
            destination = str(folders[labels.index(target)]["id"])
            self._service().propose(
                EmailProposal(
                    account.id,
                    EmailProposalKind.MESSAGE_MOVE,
                    {"message_id": message["id"], "destination_folder_id": destination},
                    {
                        "folder_id": message["folder_id"],
                        "change_key": message["change_key"],
                        "etag": message.get("etag", ""),
                    },
                    "User-staged item-level message move.",
                    1.0,
                )
            )
            self.refresh()
        except Exception as error:
            QMessageBox.critical(self, "Move proposal failed", str(error))

    def propose_rule(self) -> None:
        try:
            account = self._active_account()
            item = self.messages.currentItem()
            message = item.data(0, Qt.ItemDataRole.UserRole) if item else None
            if not isinstance(message, dict):
                raise RuntimeError("Select a historical sample message first")
            folders = (
                self.controller.store.list_mail_folders(account.id) if self.controller.store else []
            )
            labels = [f"{value['display_name']} — {value['id']}" for value in folders]
            target, accepted = QInputDialog.getItem(
                self, "Rule destination", "Move matching mail to", labels, 0, False
            )
            if not accepted:
                return
            destination = str(folders[labels.index(target)]["id"])
            sender = str(message.get("sender_address", ""))
            proposal = EmailProposal(
                account.id,
                EmailProposalKind.RULE_CREATE,
                {
                    "display_name": f"AIOrganizer: {sender}"[:255],
                    "conditions": {"senderContains": [sender]},
                    "exceptions": {},
                    "actions": {"moveToFolder": destination, "stopProcessingRules": True},
                    "priority": 1,
                    "sample_message_ids": [message["id"]],
                },
                {},
                "Rule derived from the explicitly selected historical message sample.",
                0.8,
            )
            self._service().propose(proposal)
            self.refresh()
        except Exception as error:
            QMessageBox.critical(self, "Rule proposal failed", str(error))

    def propose_categories(self) -> None:
        try:
            account = self._active_account()
            item = self.messages.currentItem()
            message = item.data(0, Qt.ItemDataRole.UserRole) if item else None
            if not isinstance(message, dict):
                raise RuntimeError("Select a message first")
            value, accepted = QInputDialog.getText(
                self, "Message categories", "Comma-separated reviewed categories"
            )
            categories = [part.strip() for part in value.split(",") if part.strip()]
            if not accepted or not categories:
                return
            self._service().propose(
                EmailProposal(
                    account.id,
                    EmailProposalKind.MESSAGE_CATEGORIZE,
                    {"message_id": message["id"], "categories": categories},
                    {
                        "folder_id": message["folder_id"],
                        "change_key": message["change_key"],
                        "etag": message.get("etag", ""),
                    },
                    "User-staged message category assignment.",
                    1.0,
                )
            )
            self.refresh()
        except Exception as error:
            QMessageBox.critical(self, "Category proposal failed", str(error))

    def review_permissions(self) -> None:
        try:
            review = self._service().permission_review(self._selected_proposal_ids())
            QMessageBox.information(
                self,
                "Exact delegated permission review",
                "Actions:\n- "
                + "\n- ".join(review.actions)
                + "\n\nAdditional scopes: "
                + (", ".join(review.additional_scopes) or "None")
                + "\n\nSending: unavailable\nPermanent deletion: unavailable",
            )
        except Exception as error:
            QMessageBox.critical(self, "Permission review failed", str(error))

    def apply_selected(self) -> None:
        try:
            service = self._service()
            account = self._active_account()
            selected = self._selected_proposal_ids()
            review = service.permission_review(selected)
            granted = set(account.granted_scopes)
            token = None
            if review.additional_scopes:
                scopes = tuple(sorted(set(READ_SCOPES) | set(review.additional_scopes)))
                _auth, result = self._interactive_token(scopes)
                token = str(result["access_token"])
                granted.update(str(result.get("scope", "")).split())
                account.granted_scopes = tuple(sorted(granted))
                account.revision += 1
                if self.controller.store:
                    self.controller.store.save_email_account(account)
            else:
                auth = MsalDeviceAuth()
                required = tuple(
                    sorted(set(READ_SCOPES) | set(MAIL_WRITE_SCOPES) | set(RULE_WRITE_SCOPES))
                )
                token = auth.acquire_silent(required, account.home_account_id)
                if not token:
                    token = auth.acquire_silent(tuple(granted), account.home_account_id)
            if not token:
                raise RuntimeError(
                    "A delegated write token is unavailable; review permissions again"
                )
            confirmation, accepted = QInputDialog.getText(
                self,
                "Apply reviewed email changes",
                "Type APPLY EMAIL CHANGES",
            )
            if not accepted:
                return
            service.apply(selected, token, tuple(granted), confirmation=confirmation)
            self.refresh()
        except Exception as error:
            QMessageBox.critical(self, "Email apply failed", str(error))

    def _active_account(self):  # type: ignore[no-untyped-def]
        account = self._service().active_account()
        if not account:
            raise RuntimeError("No active delegated mailbox")
        return account

    def _selected_proposal_ids(self) -> set[str]:
        return {
            str(item.data(0, Qt.ItemDataRole.UserRole))
            for item in self.proposals.selectedItems()
            if item.data(0, Qt.ItemDataRole.UserRole)
        }

    def _email_proposal_context_actions(
        self, _items: list[QTreeWidgetItem]
    ) -> list[tuple[str, Any]]:
        return [
            (
                f"Remove {len(self._selected_proposal_ids()):,} selected proposal(s)",
                self._remove_email_proposals,
            )
        ]

    def _remove_email_proposals(self) -> None:
        if not self.controller.store:
            return
        removed = self.controller.store.delete_email_proposals(self._selected_proposal_ids())
        self.controller.store.activity(
            "email.proposal_removed", f"Removed {removed} uncommitted email proposal(s)"
        )
        self.controller.activity_changed.emit()
        self.refresh()

    def refresh(self) -> None:
        for tree in (
            self.folders,
            self.messages,
            self.proposals,
            self.security,
            self.attachments,
            self.handoffs,
        ):
            tree.clear()
        if not self.controller.store:
            self.banner.setText(
                "Open a workspace to configure one active delegated Outlook account."
            )
            return
        self._refresh_handoffs()
        service = self._service()
        account = service.active_account()
        if not account:
            configured = bool(os.getenv("AIORGANIZER_GRAPH_CLIENT_ID"))
            self.banner.setText(
                "No active Outlook account. Set AIORGANIZER_GRAPH_CLIENT_ID and use read-only sign-in."
                if configured
                else "Set AIORGANIZER_GRAPH_CLIENT_ID in .env, restart, then use read-only sign-in."
            )
            return
        self.banner.setText(
            f"Active mailbox: {account.display_name} <{account.username}>. Delegated scopes: "
            + ", ".join(account.granted_scopes)
        )
        folders = self.controller.store.list_mail_folders(account.id)
        for value in folders:
            item = QTreeWidgetItem(
                [
                    str(value["display_name"]),
                    str(value["total_item_count"]),
                    str(value["unread_item_count"]),
                    str(value["id"]),
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, value["id"])
            self.folders.addTopLevelItem(item)
        for value in self.controller.store.list_mail_messages(account.id):
            item = QTreeWidgetItem(
                [
                    str(value.get("received_at", "")),
                    str(value.get("subject", "")),
                    str(value.get("sender_address", "")),
                    str(value.get("folder_id", "")),
                    "Yes" if value.get("has_attachments") else "No",
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, value)
            self.messages.addTopLevelItem(item)
        for value in self.controller.store.list_email_proposals(account.id):
            item = QTreeWidgetItem(
                [
                    str(value["kind"]),
                    str(value["status"]),
                    str(value["rationale"]),
                    f"{float(value['confidence']):.0%}",
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, value["id"])
            self.proposals.addTopLevelItem(item)
        for value in self.controller.store.list_account_security_evidence(account.id):
            self.security.addTopLevelItem(
                QTreeWidgetItem(
                    [
                        str(value["display_name"]),
                        str(value["mailbox"]),
                        str(value["first_evidence_at"]),
                        str(value["last_evidence_at"]),
                        ", ".join(value["categories"]),
                    ]
                )
            )
        attachment_matches: dict[str, list[str]] = {}
        for payload in self.controller.recurrence_series():
            missing = {
                str(row["period_start"])
                for row in self.controller.recurrence_gap_rows(str(payload["id"]))
                if str(row["status"]) == GapStatus.MISSING.value
            }
            if not missing:
                continue
            series = recurrence_series_from_payload(payload)
            for match in service.recurring_attachment_matches(account.id, series, missing):
                attachment_matches.setdefault(match.attachment_id, []).append(
                    f"{series.name}: {match.period_start} ({match.confidence:.0%})"
                )
        for value in self.controller.store.list_mail_attachments(account.id):
            self.attachments.addTopLevelItem(
                QTreeWidgetItem(
                    [
                        str(value["filename"]),
                        str(value["mime_type"]),
                        str(value["size"]),
                        str(value.get("received_at", "")),
                        "; ".join(attachment_matches.get(str(value["id"]), [])),
                        str(value["message_id"]),
                    ]
                )
            )

    def _refresh_handoffs(self) -> None:
        if not self.controller.store:
            return
        for value in self.controller.store.list_semantic_records("email", "outlook_handoff_v1"):
            facts = value.get("facts", {})
            item = facts.get("item", {})
            sender = item.get("sender", {})
            self.handoffs.addTopLevelItem(
                QTreeWidgetItem(
                    [
                        str(facts.get("exported_at", "")),
                        str(item.get("subject", "")),
                        str(sender.get("address", "")),
                        str(len(item.get("attachments", []))),
                        str(value.get("entity_key", "")),
                    ]
                )
            )


class RoadmapPage(QWidget):
    def __init__(self, title_text: str, description: str) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        title = QLabel(title_text)
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        text = QLabel(description)
        text.setWordWrap(True)
        layout.addWidget(text)
        layout.addStretch()


def _parse_mapping(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for pair in value.split(";"):
        if "=" not in pair:
            continue
        source, target = pair.split("=", 1)
        if source.strip() and target.strip():
            result[source.strip().casefold()] = target.strip()
    return result


def _normalized_application_name(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _display_timestamp_ns(value: Any) -> str:
    try:
        return (
            datetime.fromtimestamp(int(value) / 1_000_000_000)
            .astimezone()
            .isoformat(timespec="seconds")
        )
    except (OSError, OverflowError, TypeError, ValueError):
        return ""


def _provider_for(controller: WorkspaceController, name: str, model: str) -> Any:
    secrets = SecretStore()
    if name == "deepseek":
        key = secrets.get("deepseek_api_key") or os.getenv("DEEPSEEK_API_KEY", "")
        if not key:
            raise RuntimeError("Set DEEPSEEK_API_KEY in .env or configure it in Settings")
        return DeepSeekProvider(
            key,
            model or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )
    if name == "openai":
        key = secrets.get("openai_api_key") or os.getenv("OPENAI_API_KEY", "")
        if not key:
            raise RuntimeError("Configure an OpenAI API key in Settings")
        return OpenAIProvider(key, model)
    if name == "openrouter":
        key = secrets.get("openrouter_api_key") or os.getenv("OPENROUTER_API_KEY", "")
        if not key:
            raise RuntimeError("Set OPENROUTER_API_KEY in .env or configure it in Settings")
        return OpenRouterProvider(
            key,
            model or os.getenv("OPENROUTER_MODEL", "openai/gpt-5.2"),
            os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        )
    if name == "anthropic":
        key = secrets.get("anthropic_api_key") or os.getenv("ANTHROPIC_API_KEY", "")
        if not key:
            raise RuntimeError("Configure an Anthropic API key in Settings")
        return AnthropicProvider(key, model)
    if name == "codex":
        runtime = CodexRuntimeDetector().detect()
        if not runtime or not runtime.compatible:
            raise RuntimeError("No compatible Codex app-server or pinned SDK runtime")
        workspace = str(controller.store.path) if controller.store else None
        return CodexProvider(runtime, workspace)
    raise ValueError(f"Unsupported provider: {name}")
