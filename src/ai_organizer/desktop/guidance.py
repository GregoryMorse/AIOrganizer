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
    "rename": ["Prefer document dates", "Omit redundant folder context", "Preserve entity names"],
    "folder": ["Keep hierarchy at most 3 levels", "Group recurring documents by year"],
    "move": ["Prefer existing destinations", "Separate archives from active material"],
    "action": ["Route uncertainty to review", "Explain sensitive classifications"],
}


class GuidancePanel(QGroupBox):
    revision_saved = Signal(str)

    def __init__(
        self,
        view_key: str,
        save_revision: Callable[[PromptRevision], None],
        compile_context: Callable[[str, str, str, str, str], CompiledPrompt] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("AI Guidance", parent)
        self.setCheckable(True)
        self.setChecked(False)
        self.view_key = view_key
        self._save_revision = save_revision
        self._compile_context = compile_context
        self.compiler = PromptCompiler()
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.provider = QComboBox()
        self.provider.addItems(["local", "openai", "anthropic", "codex"])
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

    def save(self) -> None:
        text = self.editor.toPlainText().strip()
        self.compiler.validate_editable(text)
        revision = PromptRevision(f"view:{self.view_key}", PromptLayerKind.VIEW, text)
        self._save_revision(revision)
        self.history.insertItem(0, f"{revision.created_at} — {revision.id}", revision.text)
        self.revision_saved.emit(revision.id)
        self.show_preview()

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
        models = {
            "local": ["deterministic"],
            "openai": ["gpt-5.6-terra", "gpt-5.6-sol"],
            "anthropic": ["claude-sonnet-5", "claude-opus-4-8"],
            "codex": ["user-default"],
        }[current]
        self.model.clear()
        self.model.addItems(models)
