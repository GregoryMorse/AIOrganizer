from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import QModelIndex, QProcess, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
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
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ai_organizer.adapters.extraction import default_registry
from ai_organizer.adapters.providers import (
    AnthropicProvider,
    CodexProvider,
    CodexRuntimeDetector,
    OpenAIProvider,
)
from ai_organizer.adapters.providers.base import detect_secret_kinds
from ai_organizer.adapters.secrets import SecretStore
from ai_organizer.domain.actions import (
    ActionEngine,
    ActionFilter,
    ActionOutputMode,
    ActionPreset,
    ActionRun,
    FilterOperator,
    builtin_actions,
)
from ai_organizer.domain.models import (
    CategoryAssignment,
    CategoryDefinition,
    CloudPolicy,
    FolderRole,
    ItemSnapshot,
)
from ai_organizer.domain.naming import NamingProfile, builtin_naming_profiles, disambiguate
from ai_organizer.domain.prompts import PromptCompiler, PromptLayerKind, PromptRevision

from .controller import WorkspaceController
from .guidance import GuidancePanel
from .preview import FilePreview
from .table_models import DictTableModel


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
        layout.addStretch()
        controller.workspace_changed.connect(self.refresh)
        controller.inventory_changed.connect(self.refresh)
        self.controller = controller

    def refresh(self) -> None:
        if not self.controller.store:
            self.summary.setText("Create or open a workspace to begin.")
            return
        self.summary.setText(
            f"Workspace: {self.controller.store.get_meta('name')}\n"
            f"Sources: {len(self.controller.sources)}\n"
            f"Inventory records: {len(self.controller.items)}"
        )


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
        assign = QPushButton("Assign selected folder policy…")
        assign.clicked.connect(self.assign_folder)
        controls.addWidget(add_source)
        controls.addWidget(scan)
        controls.addWidget(add_category)
        controls.addWidget(assign)
        controls.addStretch()
        layout.addLayout(controls)
        splitter = QSplitter()
        self.sources = QTreeWidget()
        self.sources.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.sources.setHeaderLabels(["Source/folder", "Categories", "Roles", "Cloud", "Status"])
        self.categories = QTreeWidget()
        self.categories.setHeaderLabels(["Category", "Sensitivity", "Cloud", "Max depth"])
        splitter.addWidget(self.sources)
        splitter.addWidget(self.categories)
        self.sources.itemSelectionChanged.connect(self.record_source_selection)
        layout.addWidget(splitter)
        note = QLabel(
            "Assignments inherit into descendants. AI suggestions remain inactive until approved. "
            "Overlapping roots are rejected to prevent duplicate identities."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        controller.workspace_changed.connect(self.refresh)
        self.refresh()

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
        options = SourceOptionsDialog(categories, self)
        if options.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.controller.add_source(
                Path(folder),
                options.roles(),
                options.cloud_policy(),
                options.category_ids(),
                options.exclusions(),
            )
        except Exception as error:
            QMessageBox.critical(self, "Cannot add source", str(error))

    def scan(self) -> None:
        try:
            count = self.controller.scan_all()
            QMessageBox.information(self, "Inventory complete", f"Recorded {count} items.")
        except Exception as error:
            QMessageBox.critical(self, "Inventory failed", str(error))

    def add_category(self) -> None:
        if not self.controller.store:
            QMessageBox.warning(self, "Workspace required", "Open a workspace first.")
            return
        dialog = CategoryDialog(self.controller.store.list_category_payloads(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        category = dialog.category()
        self.controller.store.save_category(category)
        self.controller.store.mark_proposals_stale(
            {"folder", "move", "finding"}, "Category policy revision changed"
        )
        self.controller.store.activity("category.created", f"Created category {category.name}")
        self.refresh()

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
        dialog = AssignmentDialog(self.controller.store.list_category_payloads(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        assignment = CategoryAssignment(
            source.path / str(relative),
            dialog.category_ids(),
            dialog.roles(),
            override_roles=dialog.override_roles.isChecked(),
        )
        self.controller.store.save_assignment(assignment)
        self.controller.store.mark_proposals_stale(
            {"folder", "move", "finding"}, "Folder assignment revision changed"
        )
        self.controller.store.activity(
            "category.assignment",
            f"Assigned approved policy to {source.name}/{relative}",
        )
        self.refresh()

    def refresh(self) -> None:
        self.sources.clear()
        category_payloads = (
            self.controller.store.list_category_payloads() if self.controller.store else []
        )
        category_names = {value["id"]: value["name"] for value in category_payloads}
        assignments = (
            self.controller.store.list_assignment_payloads() if self.controller.store else []
        )
        for source in self.controller.sources.values():
            capabilities = source.capabilities
            root_item = QTreeWidgetItem(
                [
                    str(source.path),
                    ", ".join(category_names.get(value, value) for value in source.category_ids),
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
                roles = set(source.roles)
                resolved = (source.path / relative).resolve(strict=False)
                for assignment in assignments:
                    assignment_path = Path(assignment["path"]).resolve(strict=False)
                    if assignment_path == resolved or assignment_path in resolved.parents:
                        category_ids.update(assignment.get("category_ids", []))
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


class SourceOptionsDialog(QDialog):
    def __init__(self, categories: list[dict[str, Any]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Source policy")
        form = QFormLayout(self)
        self.role_checks: dict[str, QCheckBox] = {}
        role_box = QWidget()
        role_layout = QVBoxLayout(role_box)
        for role in ["inbox", "destination", "archive", "protected", "excluded"]:
            check = QCheckBox(role.title())
            check.setChecked(role == "inbox")
            self.role_checks[role] = check
            role_layout.addWidget(check)
        self.cloud = QComboBox()
        self.cloud.addItems(["none", "text_and_images"])
        self.categories = QListWidget()
        for category in categories:
            item = QListWidgetItem(category["name"])
            item.setData(Qt.ItemDataRole.UserRole, category["id"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.categories.addItem(item)
        self.exclusion_patterns = QLineEdit()
        self.exclusion_patterns.setPlaceholderText("e.g. nested-root/**; cache/**")
        form.addRow("Operational roles", role_box)
        form.addRow("Categories", self.categories)
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


class AssignmentDialog(QDialog):
    def __init__(self, categories: list[dict[str, Any]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Approved folder assignment")
        form = QFormLayout(self)
        self.categories = QListWidget()
        for category in categories:
            item = QListWidgetItem(category["name"])
            item.setData(Qt.ItemDataRole.UserRole, category["id"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.categories.addItem(item)
        role_box = QWidget()
        role_layout = QVBoxLayout(role_box)
        self.role_checks: dict[str, QCheckBox] = {}
        for role in ["inbox", "destination", "archive", "protected", "excluded"]:
            check = QCheckBox(role.title())
            self.role_checks[role] = check
            role_layout.addWidget(check)
        self.override_roles = QCheckBox("Replace inherited routing roles")
        form.addRow("Add categories", self.categories)
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


class CategoryDialog(QDialog):
    def __init__(self, categories: list[dict[str, Any]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New category")
        layout = QFormLayout(self)
        self.name = QLineEdit()
        self.description = QLineEdit()
        self.guidance = QLineEdit()
        self.parent_category = QComboBox()
        self.parent_category.addItem("(top level)", None)
        for category in categories:
            self.parent_category.addItem(category["name"], category["id"])
        self.sensitivity = QComboBox()
        self.sensitivity.addItems(["normal", "confidential", "restricted"])
        self.cloud = QComboBox()
        self.cloud.addItems(["inherit", "none", "text_and_images"])
        self.depth = QSpinBox()
        self.depth.setRange(1, 12)
        self.depth.setValue(4)
        layout.addRow("Name", self.name)
        layout.addRow("Description", self.description)
        layout.addRow("AI guidance", self.guidance)
        layout.addRow("Parent category", self.parent_category)
        layout.addRow("Sensitivity", self.sensitivity)
        layout.addRow("Cloud policy", self.cloud)
        layout.addRow("Maximum hierarchy depth", self.depth)
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
            description=self.description.text().strip(),
            guidance=self.guidance.text().strip(),
            parent_id=self.parent_category.currentData(),
            sensitivity=Sensitivity(self.sensitivity.currentText()),
            cloud_policy=CloudPolicy(self.cloud.currentText()),
            max_hierarchy_depth=self.depth.value(),
        )


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
        controls.addWidget(hydrate)
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
            ]
        )
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.selectionModel().currentChanged.connect(self.preview)
        self.table.selectionModel().selectionChanged.connect(self.record_selection)
        splitter.addWidget(self.table)
        self.file_preview = FilePreview()
        splitter.addWidget(self.file_preview)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter)
        controller.inventory_changed.connect(self.refresh)

    def refresh(self) -> None:
        self.model.set_rows(self.controller.items)

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

    def preview(self, current: QModelIndex, previous: QModelIndex) -> None:
        row = self.model.row(current)
        if not row:
            return
        source = self.controller.sources.get(row["root_id"])
        if not source:
            return
        path = source.path / row["relative_path"]
        self.file_preview.show_path(path, placeholder=bool(row.get("is_placeholder")))


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
        layout = QVBoxLayout(self)
        title = QLabel(title_text)
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        self.guidance = GuidancePanel(
            view_key, controller.save_prompt_revision, controller.compile_prompt
        )
        layout.addWidget(self.guidance)
        self.model = DictTableModel(columns)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.file_preview = FilePreview()
        splitter = QSplitter()
        splitter.addWidget(self.table)
        splitter.addWidget(self.file_preview)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter)
        self.table.selectionModel().currentChanged.connect(self.preview_selection)

    def preview_selection(self, current: QModelIndex, previous: QModelIndex) -> None:
        row = self.model.row(current)
        if not row or "root_id" not in row or "relative_path" not in row:
            return
        source = self.controller.sources.get(str(row["root_id"]))
        if source:
            self.file_preview.show_path(
                source.path / str(row["relative_path"]),
                placeholder=bool(row.get("is_placeholder")),
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
        commit = QPushButton("Freeze, preflight & commit selected…")
        commit.clicked.connect(self.commit_selected)
        controls.addWidget(propose)
        controls.addWidget(analyze)
        controls.addWidget(commit)
        controls.addStretch()
        self.layout().insertLayout(2, controls)

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
            count = self.controller.execute_rename_rows(
                self.model.rows,
                compiled.digest,
                self.guidance.provider.currentText(),
                self.guidance.model.currentText(),
            )
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
            for root_id in {str(row["root_id"]) for row in candidates}:
                allowed, reason = self.controller.cloud_allowed(root_id)
                if not allowed:
                    raise PermissionError(reason)
            registry = default_registry()
            item_lookup = {item["id"]: item for item in self.controller.items}
            evidence_records: list[dict[str, Any]] = []
            for row in candidates:
                payload = item_lookup[row["item_id"]]
                snapshot = ItemSnapshot(
                    id=payload["id"],
                    root_id=payload["root_id"],
                    relative_path=payload["relative_path"],
                    size=payload["size"],
                    modified_ns=payload["modified_ns"],
                    file_id=payload.get("file_id"),
                    mime_type=payload["mime_type"],
                    is_dir=payload.get("is_dir", False),
                    is_placeholder=payload.get("is_placeholder", False),
                    is_project_root=payload.get("is_project_root", False),
                )
                source = self.controller.sources[snapshot.root_id]
                evidence = registry.extract(source.path / snapshot.relative_path, snapshot)
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
                    }
                )
            import json

            compiled = self.guidance.compile_current(
                json.dumps(evidence_records, ensure_ascii=False)
            )
            answer = QMessageBox.warning(
                self,
                "Confirm cloud analysis",
                f"Provider: {compiled.provider}\nModel: {compiled.model}\n"
                f"Items: {len(candidates)}\nRedacted evidence: {compiled.evidence_bytes:,} bytes\n"
                f"Estimated input: {len(compiled.text) // 4:,} tokens\n\n"
                "Secret-like values and long account identifiers are masked before sending.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            provider = self._provider(compiled.provider, compiled.model)
            result = provider.analyze(compiled)
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

    def _provider(self, name: str, model: str) -> Any:
        return _provider_for(self.controller, name, model)


class FolderPlanPage(ReviewPage):
    def __init__(self, controller: WorkspaceController) -> None:
        super().__init__(
            "Folder Plan",
            "folder",
            [
                ("selected", "Create"),
                ("current", "Current folder"),
                ("projected", "Projected folder"),
                ("action", "Action"),
                ("status", "Status"),
            ],
            controller,
        )
        note = QLabel(
            "Folder Plan creates folders or renames them in place; it never moves files implicitly."
        )
        note.setWordWrap(True)
        self.layout().insertWidget(2, note)
        controls = QHBoxLayout()
        propose = QPushButton("Build union hierarchy proposal")
        propose.clicked.connect(self.generate)
        commit = QPushButton("Freeze, preflight & commit selected…")
        commit.clicked.connect(self.commit_selected)
        controls.addWidget(propose)
        controls.addWidget(commit)
        controls.addStretch()
        self.layout().insertLayout(3, controls)

    def generate(self) -> None:
        rows: list[dict[str, Any]] = []
        if not self.controller.store:
            self.model.set_rows(rows)
            return
        categories = [
            category
            for category in self.controller.store.list_category_payloads()
            if not category.get("parent_id")
        ]
        for source in self.controller.sources.values():
            if not source.roles.intersection({FolderRole.DESTINATION, FolderRole.ARCHIVE}):
                continue
            current_folders = {
                item["relative_path"]
                for item in self.controller.items
                if item["root_id"] == source.id and item.get("is_dir")
            }
            for current in sorted(current_folders):
                rows.append(
                    {
                        "selected": False,
                        "current": current,
                        "projected": current,
                        "action": "unchanged",
                        "status": "aligned",
                        "root_id": source.id,
                    }
                )
            for category in categories:
                projected = category["name"].strip()
                if projected in current_folders or not projected:
                    continue
                rows.append(
                    {
                        "selected": True,
                        "current": "—",
                        "projected": projected,
                        "action": "create",
                        "status": "proposed",
                        "root_id": source.id,
                        "category_id": category["id"],
                        "reason": "Top-level category folder in an eligible destination root",
                    }
                )
        self.model.set_rows(rows)

    def commit_selected(self) -> None:
        count = sum(bool(row.get("selected")) for row in self.model.rows)
        if not count:
            QMessageBox.information(self, "Nothing selected", "Select one or more folders.")
            return
        answer = QMessageBox.warning(
            self,
            "Explicit folder commit",
            f"Create {count} selected folder(s)? This plan cannot delete or reparent anything.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            compiled = self.guidance.compile_current("Projected hierarchy is untrusted evidence.")
            completed = self.controller.execute_folder_rows(self.model.rows, compiled.digest)
            QMessageBox.information(self, "Folders verified", f"Created {completed} folder(s).")
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
                ("reason", "Reason"),
            ],
            controller,
        )
        note = QLabel(
            "Move preserves filenames. Cross-volume moves are copy-verified and originals remain in quarantine."
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
        self.layout().insertLayout(3, controls)
        controller.workspace_changed.connect(self.refresh_destinations)
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
        destination_root = self.controller.sources[str(destination_id)]
        rows: list[dict[str, Any]] = []
        for item in self.controller.items:
            source = self.controller.sources.get(item["root_id"])
            if not source or FolderRole.INBOX not in source.roles or source.id == destination_id:
                continue
            if item.get("is_dir") and not item.get("is_project_root"):
                continue
            filename = Path(item["relative_path"]).name
            target = destination_root.path / filename
            blocked = target.exists()
            rows.append(
                {
                    "selected": not blocked,
                    "status": "blocked" if blocked else "proposed",
                    "source": str((source.path / item["relative_path"]).parent),
                    "destination": "",
                    "filename": filename,
                    "reason": "Occupied target"
                    if blocked
                    else "Inbox item to eligible destination",
                    "root_id": source.id,
                    "destination_root_id": destination_id,
                    "relative_path": item["relative_path"],
                    "item_id": item["id"],
                    "is_dir": item.get("is_dir", False),
                    "is_project_root": item.get("is_project_root", False),
                    "is_placeholder": item.get("is_placeholder", False),
                    "size": item.get("size", 0),
                }
            )
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
            f"Commit {count} selected move(s)? Cross-volume originals enter indefinite quarantine.\n"
            f"Required cross-volume duplicate space: {duplicate_bytes:,} bytes.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            compiled = self.guidance.compile_current("Selected move rows are untrusted evidence.")
            completed = self.controller.execute_move_rows(self.model.rows, compiled.digest)
            QMessageBox.information(self, "Moves verified", f"Verified {completed} move(s).")
            self.generate()
        except Exception as error:
            QMessageBox.critical(self, "Move plan not committed", str(error))


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
        self.layout().insertLayout(2, controls)
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
        findings = ActionEngine().evaluate(preset, normalized, run)
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
                result = _provider_for(self.controller, provider_name, model).analyze(compiled)
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
            count = self.controller.undo_last_commit()
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
        provider = QLabel(
            "Provider credentials are stored in the operating-system credential store. "
            "Cloud analysis remains disabled per source until explicitly enabled."
        )
        provider.setWordWrap(True)
        layout.addWidget(provider)
        workspace_group = QGroupBox("Workspace AI guidance")
        workspace_layout = QVBoxLayout(workspace_group)
        self.workspace_guidance = QPlainTextEdit()
        self.workspace_guidance.setMaximumHeight(110)
        self.workspace_guidance.setPlaceholderText(
            "General organization vocabulary, hierarchy, and ambiguity preferences…"
        )
        workspace_layout.addWidget(self.workspace_guidance)
        save_workspace = QPushButton("Save workspace guidance revision")
        save_workspace.clicked.connect(self.save_workspace_guidance)
        workspace_layout.addWidget(save_workspace)
        layout.addWidget(workspace_group)
        credentials = QFormLayout()
        self.openai_key = QLineEdit()
        self.openai_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.anthropic_key = QLineEdit()
        self.anthropic_key.setEchoMode(QLineEdit.EchoMode.Password)
        credentials.addRow("OpenAI API key", self.openai_key)
        credentials.addRow("Anthropic API key", self.anthropic_key)
        layout.addLayout(credentials)
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
        layout.addLayout(controls)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch()

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
        if self.openai_key.text().strip():
            store.set("openai_api_key", self.openai_key.text().strip())
        if self.anthropic_key.text().strip():
            store.set("anthropic_api_key", self.anthropic_key.text().strip())
        self.openai_key.clear()
        self.anthropic_key.clear()
        self.status.setText("Credentials saved without writing them to the workspace or logs.")

    def clear_keys(self) -> None:
        from ai_organizer.adapters.secrets import SecretStore

        store = SecretStore()
        store.delete("openai_api_key")
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


def _provider_for(controller: WorkspaceController, name: str, model: str) -> Any:
    secrets = SecretStore()
    if name == "openai":
        key = secrets.get("openai_api_key")
        if not key:
            raise RuntimeError("Configure an OpenAI API key in Settings")
        return OpenAIProvider(key, model)
    if name == "anthropic":
        key = secrets.get("anthropic_api_key")
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
