from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ai_organizer.domain.prompts import (
    CompiledPrompt,
    PromptCompiler,
    PromptLayerKind,
    PromptRevision,
)

SUGGESTIONS = {
    "audit": ["Prefer repeated patterns", "Explain counterexamples", "Avoid brittle rules"],
    "rename": ["Prefer document dates", "Omit redundant folder context", "Preserve entity names"],
    "folder": ["Keep hierarchy at most 3 levels", "Group recurring documents by year"],
    "move": ["Prefer existing destinations", "Separate archives from active material"],
    "action": ["Route uncertainty to review", "Explain sensitive classifications"],
    "cleanup": [
        "Preserve source and final outputs",
        "Require review before removal",
        "Clean only detected project roots",
    ],
    "updates": [
        "Prefer official human-readable release pages",
        "Preserve reusable version and changelog locators",
        "Revalidate saved hints before starting a fresh search",
    ],
}

_MODELS = {
    "local": ["deterministic"],
    "deepseek": ["deepseek-v4-flash", "deepseek-v4-pro"],
    "openrouter": ["openai/gpt-5.2", "anthropic/claude-sonnet-4.6"],
    "openai": ["gpt-5.6-terra", "gpt-5.6-sol"],
    "anthropic": ["claude-sonnet-5", "claude-opus-4-8"],
    "codex": ["user-default"],
}


class GuidanceContextBar(QWidget):
    """Compact runtime context controls for working pages."""

    def __init__(
        self,
        view_key: str,
        compile_context: Callable[[str, str, str, str, str], CompiledPrompt],
        parent: QWidget | None = None,
        *,
        load_context: Callable[[str], tuple[str, str]] | None = None,
        save_context: Callable[[str, str, str], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.view_key = view_key
        self._compile_context = compile_context
        self._load_context = load_context
        self._save_context = save_context
        self._loading_context = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.addWidget(QLabel("Work with"))
        self.content_kind = QComboBox()
        self.content_kind.addItems(["Files & folders", "Email"])
        self.content_kind.setToolTip("One content system per proposal set")
        layout.addWidget(self.content_kind)
        layout.addWidget(QLabel("AI context"))
        self.provider = QComboBox()
        self.provider.addItems(list(_MODELS))
        self.provider.setToolTip("Provider used when an action requests AI analysis")
        layout.addWidget(self.provider)
        self.model = QComboBox()
        self.model.setEditable(True)
        self.provider.currentTextChanged.connect(self._provider_changed)
        self._update_models()
        self.model.currentTextChanged.connect(self._context_changed)
        layout.addWidget(self.model)
        view_label = {
            "folder": "Folder Plan",
            "action": "Focused Actions",
        }.get(view_key, view_key.replace("_", " ").title())
        location = QLabel(f"Guidance: Settings > {view_label}")
        location.setObjectName("subtleText")
        layout.addWidget(location)
        layout.addStretch()
        self.refresh_context()

    def compile_current(self, evidence: str) -> CompiledPrompt:
        return self._compile_context(
            self.view_key,
            self.provider.currentText(),
            self.model.currentText(),
            "",
            evidence,
        )

    def _update_models(self) -> None:
        current = self.provider.currentText()
        self.model.clear()
        self.model.addItems(_MODELS[current])

    def _provider_changed(self) -> None:
        self._update_models()
        self._context_changed()

    def _context_changed(self) -> None:
        if self._save_context and not self._loading_context:
            self._save_context(
                self.view_key, self.provider.currentText(), self.model.currentText()
            )

    def refresh_context(self) -> None:
        if not self._load_context:
            return
        provider, model = self._load_context(self.view_key)
        self._loading_context = True
        try:
            index = self.provider.findText(provider)
            self.provider.setCurrentIndex(max(0, index))
            self._update_models()
            model_index = self.model.findText(model)
            if model_index >= 0:
                self.model.setCurrentIndex(model_index)
            elif model:
                self.model.setEditText(model)
        finally:
            self._loading_context = False


class GuidancePanel(QGroupBox):
    revision_saved = Signal(str)

    def __init__(
        self,
        view_key: str,
        save_revision: Callable[[PromptRevision], None],
        compile_context: Callable[[str, str, str, str, str], CompiledPrompt] | None = None,
        load_text: Callable[[str], str] | None = None,
        parent: QWidget | None = None,
        *,
        load_context: Callable[[str], tuple[str, str]] | None = None,
        save_context: Callable[[str, str, str], None] | None = None,
    ) -> None:
        super().__init__("AI guidance configuration", parent)
        self.view_key = view_key
        self._save_revision = save_revision
        self._compile_context = compile_context
        self._load_text = load_text
        self._load_context = load_context
        self._save_context = save_context
        self.compiler = PromptCompiler()
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.provider = QComboBox()
        self.provider.addItems(list(_MODELS))
        self.model = QComboBox()
        self.model.setEditable(True)
        self._update_models()
        self.provider.currentTextChanged.connect(self._update_models)
        form.addRow("Provider", self.provider)
        form.addRow("Model", self.model)
        self.layers = QLabel("Active layers: safety, schema, view, evidence")
        self.layers.setWordWrap(True)
        form.addRow("Layers", self.layers)
        layout.addLayout(form)
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("Short guidance for this view…")
        self.editor.setMaximumHeight(110)
        layout.addWidget(self.editor)
        chips = QHBoxLayout()
        for suggestion in SUGGESTIONS.get(view_key, []):
            button = QPushButton(suggestion)
            button.clicked.connect(lambda checked=False, value=suggestion: self._append(value))
            chips.addWidget(button)
        chips.addStretch()
        layout.addLayout(chips)
        controls = QHBoxLayout()
        save = QPushButton("Save revision")
        save.clicked.connect(self.save)
        preview = QPushButton("Compile preview")
        preview.clicked.connect(self.show_preview)
        test = QPushButton("Test on selection")
        test.clicked.connect(self.show_preview)
        duplicate = QPushButton("Duplicate")
        duplicate.clicked.connect(self.duplicate_revision)
        preset = QPushButton("Save as preset…")
        preset.clicked.connect(self.save_as_preset)
        reset = QPushButton("Reset")
        reset.clicked.connect(self.editor.clear)
        controls.addWidget(save)
        controls.addWidget(preview)
        controls.addWidget(test)
        controls.addWidget(duplicate)
        controls.addWidget(preset)
        controls.addWidget(reset)
        controls.addStretch()
        layout.addLayout(controls)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMaximumHeight(180)
        layout.addWidget(self.preview)
        self.history = QComboBox()
        self.history.setToolTip("Prompt revisions created during this session")
        layout.addWidget(self.history)
        self.cloud_summary = QLabel("Cloud content: disabled until source opt-in")
        layout.addWidget(self.cloud_summary)
        self.refresh_saved()

    def save(self) -> None:
        text = self.editor.toPlainText().strip()
        self.compiler.validate_editable(text)
        revision = PromptRevision(f"view:{self.view_key}", PromptLayerKind.VIEW, text)
        self._save_revision(revision)
        if self._save_context:
            self._save_context(
                self.view_key, self.provider.currentText(), self.model.currentText()
            )
        self.history.insertItem(0, f"{revision.created_at} — {revision.id}", revision.text)
        self.revision_saved.emit(revision.id)
        self.show_preview()

    def refresh_saved(self) -> None:
        if self._load_text:
            self.editor.setPlainText(self._load_text(f"view:{self.view_key}"))
        if self._load_context:
            provider, model = self._load_context(self.view_key)
            index = self.provider.findText(provider)
            self.provider.setCurrentIndex(max(0, index))
            self._update_models()
            model_index = self.model.findText(model)
            if model_index >= 0:
                self.model.setCurrentIndex(model_index)
            elif model:
                self.model.setEditText(model)

    def show_preview(self) -> None:
        compiled = self.compile_current(
            "Selected evidence will be inserted here and marked untrusted."
        )
        self.preview.setPlainText(compiled.text)
        self.cloud_summary.setText(
            f"Cloud content: {compiled.evidence_bytes:,} evidence bytes; "
            f"provider {compiled.provider}; model {compiled.model}; redaction required before send"
        )

    def compile_current(self, evidence: str) -> CompiledPrompt:
        text = self.editor.toPlainText().strip()
        if self._compile_context:
            return self._compile_context(
                self.view_key,
                self.provider.currentText(),
                self.model.currentText(),
                text,
                evidence,
            )
        revision = (
            PromptRevision(f"view:{self.view_key}", PromptLayerKind.VIEW, text) if text else None
        )
        return self.compiler.compile(
            provider=self.provider.currentText(),
            model=self.model.currentText(),
            view=revision,
            evidence=evidence,
        )

    def duplicate_revision(self) -> None:
        text = self.editor.toPlainText().strip()
        if text:
            self.editor.setPlainText(text + "\n")
            self.editor.setFocus()

    def save_as_preset(self) -> None:
        name, accepted = QInputDialog.getText(self, "Save guidance preset", "Preset name")
        if not accepted or not name.strip():
            return
        text = self.editor.toPlainText().strip()
        self.compiler.validate_editable(text)
        revision = PromptRevision(f"preset:{name.strip()}", PromptLayerKind.VIEW, text)
        self._save_revision(revision)
        self.history.insertItem(0, f"Preset: {name.strip()}", revision.text)

    def _append(self, suggestion: str) -> None:
        existing = self.editor.toPlainText().strip()
        self.editor.setPlainText(f"{existing}\n{suggestion}.".strip())

    def _update_models(self) -> None:
        current = self.provider.currentText()
        self.model.clear()
        self.model.addItems(_MODELS[current])
