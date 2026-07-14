from __future__ import annotations

from pathlib import Path

from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import QLabel, QPlainTextEdit, QStackedWidget, QVBoxLayout, QWidget


class FilePreview(QWidget):
    """Bounded, read-only preview with an embedded Qt PDF renderer."""

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        self.caption = QLabel("Select a file to inspect its evidence.")
        self.caption.setWordWrap(True)
        layout.addWidget(self.caption)
        self.stack = QStackedWidget()
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.pdf_document = QPdfDocument(self)
        self.pdf = QPdfView()
        self.pdf.setDocument(self.pdf_document)
        self.pdf.setPageMode(QPdfView.PageMode.MultiPage)
        self.pdf.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        self.stack.addWidget(self.text)
        self.stack.addWidget(self.pdf)
        layout.addWidget(self.stack)

    def show_path(self, path: Path, *, placeholder: bool = False) -> None:
        self.caption.setText(str(path))
        if placeholder:
            self._show_text(
                "Cloud-only placeholder. Explicit hydration is required before preview."
            )
            return
        if not path.exists() or not path.is_file():
            self._show_text("The selected file is unavailable or changed since inventory.")
            return
        if path.suffix.casefold() == ".pdf":
            self.pdf_document.close()
            error = self.pdf_document.load(str(path))
            if error == QPdfDocument.Error.None_:
                self.stack.setCurrentWidget(self.pdf)
                return
            self._show_text(f"Qt could not render this PDF ({error.name}).")
            return
        if path.suffix.casefold() in {
            ".txt",
            ".md",
            ".rst",
            ".csv",
            ".tsv",
            ".json",
            ".yaml",
            ".yml",
            ".toml",
            ".xml",
            ".py",
            ".js",
            ".ts",
            ".rs",
            ".go",
            ".java",
            ".cs",
            ".cpp",
            ".c",
            ".h",
        }:
            try:
                self._show_text(path.read_text(encoding="utf-8", errors="replace")[:200_000])
            except OSError as error:
                self._show_text(str(error))
            return
        self._show_text(
            f"{path.name}\n{path.stat().st_size:,} bytes\n\n"
            "Content extraction is bounded and performed separately from this preview."
        )

    def _show_text(self, value: str) -> None:
        self.text.setPlainText(value)
        self.stack.setCurrentWidget(self.text)
