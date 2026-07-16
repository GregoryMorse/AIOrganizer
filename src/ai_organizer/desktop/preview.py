from __future__ import annotations

import codecs
import json
import mimetypes
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from PySide6.QtCore import QPoint, QRegularExpression, QSize, Qt, QUrl
from PySide6.QtGui import (
    QDesktopServices,
    QFont,
    QImageReader,
    QKeySequence,
    QPixmap,
    QSyntaxHighlighter,
    QTextCharFormat,
)
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
except (ImportError, OSError):
    # Some otherwise-supported Linux desktops do not provide PulseAudio's
    # shared library. Keep document inspection available and disable only the
    # optional embedded media player on those hosts.
    QAudioOutput = None  # type: ignore[assignment,misc]
    QMediaPlayer = None  # type: ignore[assignment,misc]
    QVideoWidget = None  # type: ignore[assignment,misc]

_MAX_TEXT_BYTES = 1_000_000
_HEX_PAGE_BYTES = 16 * 256
_MAX_IMAGE_PIXELS = 40_000_000
_MAX_IMAGE_EDGE = 10_000

_MARKDOWN_EXTENSIONS = {".md", ".markdown", ".mdown", ".mkd"}
_TEXT_EXTENSIONS = {
    ".txt",
    ".rst",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".xml",
    ".html",
    ".htm",
    ".css",
    ".sql",
    ".log",
    ".tex",
    ".bib",
    ".ps1",
    ".bat",
    ".cmd",
    ".sh",
}
_CODE_EXTENSIONS = {
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".rs",
    ".go",
    ".java",
    ".kt",
    ".kts",
    ".cs",
    ".cpp",
    ".cc",
    ".cxx",
    ".c",
    ".h",
    ".hpp",
    ".hh",
    ".swift",
    ".rb",
    ".php",
    ".lua",
    ".r",
    ".dart",
    ".vue",
    ".svelte",
}
_IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".webp",
    ".tif",
    ".tiff",
    ".svg",
    ".ico",
    ".heic",
    ".avif",
}
_AUDIO_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".aac",
    ".wav",
    ".flac",
    ".ogg",
    ".opus",
    ".wma",
}
_VIDEO_EXTENSIONS = {
    ".mp4",
    ".m4v",
    ".mkv",
    ".mov",
    ".avi",
    ".webm",
    ".wmv",
    ".mpeg",
    ".mpg",
}
_ARCHIVE_EXTENSIONS = {
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".jar",
    ".whl",
    ".msix",
    ".appx",
}
_UNSAFE_EXTERNAL_EXTENSIONS = {
    ".exe",
    ".com",
    ".scr",
    ".msi",
    ".msp",
    ".bat",
    ".cmd",
    ".ps1",
    ".sh",
}

ArchiveLoader = Callable[[int, int, str], dict[str, Any]]


class ZoomableImageView(QGraphicsView):
    """Image canvas with Ctrl+wheel zoom, drag panning, and fit reset."""

    def __init__(self) -> None:
        super().__init__()
        self._scene = QGraphicsScene(self)
        self._item = QGraphicsPixmapItem()
        self._scene.addItem(self._item)
        self.setScene(self._scene)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setBackgroundBrush(Qt.GlobalColor.darkGray)
        self.setAccessibleName("Zoomable image preview")

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self.resetTransform()
        self._item.setPixmap(pixmap)
        self._scene.setSceneRect(self._item.boundingRect())
        self.reset_zoom()

    def clear_image(self) -> None:
        self._item.setPixmap(QPixmap())
        self.resetTransform()

    def zoom_by(self, factor: float) -> None:
        if self._item.pixmap().isNull():
            return
        current = self.transform().m11()
        if 0.02 <= current * factor <= 64:
            self.scale(factor, factor)

    def reset_zoom(self) -> None:
        self.resetTransform()
        if not self._item.pixmap().isNull():
            self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)

    def wheelEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_by(1.25 if event.angleDelta().y() > 0 else 0.8)
            event.accept()
            return
        super().wheelEvent(event)


class PannablePdfView(QPdfView):
    """Qt PDF renderer with explicit zoom plus click-drag panning."""

    def __init__(self) -> None:
        super().__init__()
        self._drag_origin: QPoint | None = None
        self.setPageMode(QPdfView.PageMode.MultiPage)
        self.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        self.setAccessibleName("Zoomable PDF preview")

    def zoom_by(self, factor: float) -> None:
        if self.zoomMode() != QPdfView.ZoomMode.Custom:
            self.setZoomMode(QPdfView.ZoomMode.Custom)
        self.setZoomFactor(max(0.1, min(10.0, self.zoomFactor() * factor)))

    def reset_zoom(self) -> None:
        self.setZoomMode(QPdfView.ZoomMode.FitToWidth)

    def wheelEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_by(1.2 if event.angleDelta().y() > 0 else 1 / 1.2)
            event.accept()
            return
        super().wheelEvent(event)

    def mousePressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self._drag_origin is not None:
            current = event.position().toPoint()
            delta = current - self._drag_origin
            self._drag_origin = current
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.button() == Qt.MouseButton.LeftButton and self._drag_origin is not None:
            self._drag_origin = None
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class CodeHighlighter(QSyntaxHighlighter):
    """Small dependency-free highlighter for common source and data formats."""

    _KEYWORDS: ClassVar[set[str]] = {
        "and",
        "as",
        "async",
        "await",
        "break",
        "case",
        "catch",
        "class",
        "const",
        "continue",
        "def",
        "default",
        "do",
        "else",
        "enum",
        "except",
        "export",
        "extends",
        "false",
        "finally",
        "fn",
        "for",
        "from",
        "function",
        "if",
        "import",
        "in",
        "interface",
        "lambda",
        "let",
        "match",
        "namespace",
        "new",
        "none",
        "null",
        "or",
        "package",
        "pass",
        "private",
        "protected",
        "public",
        "raise",
        "return",
        "self",
        "static",
        "struct",
        "super",
        "switch",
        "this",
        "throw",
        "trait",
        "true",
        "try",
        "type",
        "use",
        "var",
        "while",
        "with",
        "yield",
    }

    def __init__(self, document) -> None:  # type: ignore[no-untyped-def]
        super().__init__(document)
        self._rules: list[tuple[QRegularExpression, QTextCharFormat]] = []
        keyword = QTextCharFormat()
        keyword.setForeground(Qt.GlobalColor.darkBlue)
        keyword.setFontWeight(QFont.Weight.Bold)
        self._rules.append(
            (
                QRegularExpression(r"\b(?:" + "|".join(sorted(self._KEYWORDS)) + r")\b"),
                keyword,
            )
        )
        string = QTextCharFormat()
        string.setForeground(Qt.GlobalColor.darkGreen)
        self._rules.extend(
            [
                (QRegularExpression(r'"(?:\\.|[^"\\])*"'), string),
                (QRegularExpression(r"'(?:\\.|[^'\\])*'"), string),
            ]
        )
        number = QTextCharFormat()
        number.setForeground(Qt.GlobalColor.darkMagenta)
        self._rules.append((QRegularExpression(r"\b(?:0x[0-9A-Fa-f]+|\d+(?:\.\d+)?)\b"), number))
        comment = QTextCharFormat()
        comment.setForeground(Qt.GlobalColor.darkGray)
        comment.setFontItalic(True)
        self._rules.extend(
            [
                (QRegularExpression(r"#.*$"), comment),
                (QRegularExpression(r"//.*$"), comment),
            ]
        )

    def highlightBlock(self, text: str) -> None:
        for expression, text_format in self._rules:
            iterator = expression.globalMatch(text)
            while iterator.hasNext():
                match = iterator.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), text_format)


class FilePreview(QWidget):
    """Shared, bounded file inspector used by all file-backed review pages."""

    def __init__(self) -> None:
        super().__init__()
        self._path: Path | None = None
        self._hex_offset = 0
        self._image_downsampled = False
        self._archive_loader: ArchiveLoader | None = None
        self._archive_page: dict[str, Any] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        self.caption = QLabel("Select a file to inspect its preview and metadata.")
        self.caption.setWordWrap(True)
        self.caption.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        header.addWidget(self.caption, 1)
        self.zoom_out_button = _tool_button("-", "Zoom out (Ctrl+-)", self.zoom_out)
        self.zoom_reset_button = _tool_button(
            "Fit", "Fit preview to the available area", self.reset_zoom
        )
        self.zoom_in_button = _tool_button("+", "Zoom in (Ctrl++)", self.zoom_in)
        self.open_button = QPushButton("Open externally")
        self.open_button.setToolTip("Open this document with its operating-system application")
        self.open_button.clicked.connect(self._open_external)
        self.reveal_button = QPushButton("Containing folder")
        self.reveal_button.setToolTip("Open the folder containing the selected inventory item")
        self.reveal_button.clicked.connect(self._reveal_in_folder)
        header.addWidget(self.zoom_out_button)
        header.addWidget(self.zoom_reset_button)
        header.addWidget(self.zoom_in_button)
        header.addWidget(self.reveal_button)
        header.addWidget(self.open_button)
        layout.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.setAccessibleName("File inspection views")
        layout.addWidget(self.tabs, 1)

        self.preview_stack = QStackedWidget()
        self.message = QLabel("Select a file to preview it.")
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setWordWrap(True)
        self.message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.image = ZoomableImageView()
        self.pdf_document = QPdfDocument(self)
        self.pdf = PannablePdfView()
        self.pdf.setDocument(self.pdf_document)
        self.markdown = QTextBrowser()
        self.markdown.setOpenExternalLinks(False)
        self.markdown.anchorClicked.connect(self._open_safe_link)
        self.media = self._build_media_view()
        for widget in (self.message, self.image, self.pdf, self.markdown, self.media):
            self.preview_stack.addWidget(widget)
        self.tabs.addTab(self.preview_stack, "Preview")

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text.setFont(QFont("Consolas"))
        self.text.setPlaceholderText("No bounded text representation is available for this file.")
        self.highlighter = CodeHighlighter(self.text.document())
        self.tabs.addTab(self.text, "Text / Code")

        self.metadata_table = _table(["Property", "Value"], "Extracted and filesystem metadata")
        self.tabs.addTab(self.metadata_table, "Metadata")

        self.archive_widget = QWidget()
        archive_layout = QVBoxLayout(self.archive_widget)
        archive_controls = QHBoxLayout()
        self.archive_previous = QPushButton("Previous page")
        self.archive_previous.clicked.connect(self._previous_archive)
        self.archive_next = QPushButton("Next page")
        self.archive_next.clicked.connect(self._next_archive)
        self.archive_filter = QLineEdit()
        self.archive_filter.setPlaceholderText("Filter members, e.g. **/*.pdf")
        self.archive_filter.returnPressed.connect(self._filter_archive)
        archive_filter_button = QPushButton("Filter")
        archive_filter_button.clicked.connect(self._filter_archive)
        self.archive_status = QLabel()
        archive_controls.addWidget(self.archive_previous)
        archive_controls.addWidget(self.archive_next)
        archive_controls.addWidget(self.archive_filter, 1)
        archive_controls.addWidget(archive_filter_button)
        archive_controls.addWidget(self.archive_status)
        archive_layout.addLayout(archive_controls)
        self.archive_table = _table(
            [
                "Path",
                "Uncompressed",
                "Compressed",
                "Modified",
                "Encrypted",
                "CRC32",
                "Method",
            ],
            "Archive contents",
        )
        archive_layout.addWidget(self.archive_table)
        self.archive_tab_index = self.tabs.addTab(self.archive_widget, "Archive")

        self.hex_widget = QWidget()
        hex_layout = QVBoxLayout(self.hex_widget)
        hex_controls = QHBoxLayout()
        previous = QPushButton("Previous block")
        previous.clicked.connect(self._previous_hex)
        next_button = QPushButton("Next block")
        next_button.clicked.connect(self._next_hex)
        self.hex_offset = QLineEdit("0x0")
        self.hex_offset.setMaximumWidth(150)
        self.hex_offset.setPlaceholderText("byte offset")
        self.hex_offset.returnPressed.connect(self._jump_hex)
        jump = QPushButton("Go")
        jump.clicked.connect(self._jump_hex)
        self.hex_status = QLabel()
        hex_controls.addWidget(previous)
        hex_controls.addWidget(next_button)
        hex_controls.addWidget(QLabel("Offset"))
        hex_controls.addWidget(self.hex_offset)
        hex_controls.addWidget(jump)
        hex_controls.addWidget(self.hex_status, 1)
        hex_layout.addLayout(hex_controls)
        self.hex = QPlainTextEdit()
        self.hex.setReadOnly(True)
        self.hex.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.hex.setFont(QFont("Consolas"))
        hex_layout.addWidget(self.hex)
        self.hex_tab_index = self.tabs.addTab(self.hex_widget, "Hex")

        self.tabs.currentChanged.connect(self._tab_changed)
        self.clear()

    @property
    def hex_bytes_read(self) -> int:
        return int(self.hex.property("bytesRead") or 0)

    @property
    def text_is_truncated(self) -> bool:
        return bool(self.text.property("truncated"))

    def clear(self) -> None:
        self._stop_media()
        self.pdf_document.close()
        self.image.clear_image()
        self._path = None
        self._archive_loader = None
        self._archive_page = {}
        self.caption.setText("Select a file to inspect its preview and metadata.")
        self.message.setText("Select a file to preview it.")
        self.preview_stack.setCurrentWidget(self.message)
        self.text.clear()
        self.tabs.setTabText(self.tabs.indexOf(self.text), "Text / Code")
        self.metadata_table.setRowCount(0)
        self.archive_table.setRowCount(0)
        self.archive_filter.clear()
        self.archive_status.clear()
        self.archive_previous.setEnabled(False)
        self.archive_next.setEnabled(False)
        self.hex.clear()
        self.hex_status.clear()
        self.tabs.setTabVisible(self.archive_tab_index, False)
        self.tabs.setTabVisible(self.hex_tab_index, False)
        self.open_button.setEnabled(False)
        self.reveal_button.setEnabled(False)
        self._update_zoom_buttons()

    def show_record(self, record: dict[str, Any], *, caption: str = "Indexed record") -> None:
        """Display non-file records through the same metadata inspector."""
        self.clear()
        self.caption.setText(caption)
        self.message.setText(
            "This row has indexed metadata but no directly previewable local file."
        )
        self._populate_metadata(record)
        self.tabs.setCurrentWidget(self.metadata_table)

    def show_path(
        self,
        path: Path,
        *,
        placeholder: bool = False,
        metadata: dict[str, Any] | None = None,
        member_page: dict[str, Any] | None = None,
        member_loader: ArchiveLoader | None = None,
        record: dict[str, Any] | None = None,
        extracted_text: str | None = None,
    ) -> None:
        self.clear()
        self._path = path
        self.caption.setText(str(path))
        combined = dict(record or {})
        combined.pop("metadata", None)
        combined["indexed_metadata"] = dict(metadata or {})
        combined.update(self._filesystem_metadata(path))
        self._populate_metadata(combined)

        if placeholder:
            self.message.setText(
                "Cloud-only placeholder. Explicit hydration is required before preview."
            )
            return
        if not path.exists() or not path.is_file():
            self.message.setText("The selected file is unavailable or changed since inventory.")
            return

        suffix = path.suffix.casefold()
        self._archive_loader = member_loader
        self.tabs.setTabVisible(self.hex_tab_index, True)
        self.open_button.setEnabled(suffix not in _UNSAFE_EXTERNAL_EXTENSIONS)
        self.reveal_button.setEnabled(True)
        self._populate_archive(member_page)
        if suffix in _ARCHIVE_EXTENSIONS or (metadata or {}).get("archive_format"):
            self.tabs.setTabVisible(self.archive_tab_index, True)

        if suffix == ".pdf":
            self._show_pdf(path)
            self.tabs.setTabText(self.tabs.indexOf(self.text), "Extracted / OCR Text")
            if extracted_text is not None:
                self._set_text(
                    extracted_text[:_MAX_TEXT_BYTES],
                    len(extracted_text) > _MAX_TEXT_BYTES,
                    code=False,
                )
        elif suffix in _IMAGE_EXTENSIONS:
            self._show_image(path)
            if extracted_text is not None:
                self.tabs.setTabText(self.tabs.indexOf(self.text), "Extracted / OCR Text")
                self._set_text(
                    extracted_text[:_MAX_TEXT_BYTES],
                    len(extracted_text) > _MAX_TEXT_BYTES,
                    code=False,
                )
        elif suffix in _MARKDOWN_EXTENSIONS:
            self._show_markdown(path)
        elif suffix in _CODE_EXTENSIONS or suffix in _TEXT_EXTENSIONS:
            self._show_text(path, code=suffix in _CODE_EXTENSIONS)
        elif suffix in _AUDIO_EXTENSIONS or suffix in _VIDEO_EXTENSIONS:
            self._show_media(path, video=suffix in _VIDEO_EXTENSIONS)
        elif suffix in _ARCHIVE_EXTENSIONS or (metadata or {}).get("archive_format"):
            self.message.setText(
                "Archive headers are indexed without extraction. Use the Archive tab to sort "
                "members by path, sizes, date, encryption, CRC, or compression method."
            )
            if member_page and member_page.get("members"):
                self.tabs.setCurrentIndex(self.archive_tab_index)
        else:
            self.message.setText(
                "No embedded visual renderer is available for this format. Metadata and a "
                "bounded, seekable hexadecimal view remain available."
            )
        self._update_zoom_buttons()

    def show_archive(
        self,
        path: Path,
        metadata: dict[str, Any],
        member_page: dict[str, Any],
        *,
        member_loader: ArchiveLoader | None = None,
        record: dict[str, Any] | None = None,
    ) -> None:
        self.show_path(
            path,
            metadata=metadata,
            member_page=member_page,
            member_loader=member_loader,
            record=record,
        )

    def zoom_in(self) -> None:
        if self.preview_stack.currentWidget() is self.image:
            self.image.zoom_by(1.25)
        elif self.preview_stack.currentWidget() is self.pdf:
            self.pdf.zoom_by(1.2)

    def zoom_out(self) -> None:
        if self.preview_stack.currentWidget() is self.image:
            self.image.zoom_by(0.8)
        elif self.preview_stack.currentWidget() is self.pdf:
            self.pdf.zoom_by(1 / 1.2)

    def reset_zoom(self) -> None:
        if self.preview_stack.currentWidget() is self.image:
            self.image.reset_zoom()
        elif self.preview_stack.currentWidget() is self.pdf:
            self.pdf.reset_zoom()

    def _show_pdf(self, path: Path) -> None:
        error = self.pdf_document.load(str(path))
        if error == QPdfDocument.Error.None_:
            self.preview_stack.setCurrentWidget(self.pdf)
            self.tabs.setCurrentWidget(self.preview_stack)
            return
        self.message.setText(f"Qt could not render this PDF ({error.name}).")

    def _show_image(self, path: Path) -> None:
        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        size = reader.size()
        self._image_downsampled = False
        if size.isValid() and (
            size.width() * size.height() > _MAX_IMAGE_PIXELS
            or max(size.width(), size.height()) > _MAX_IMAGE_EDGE
        ):
            scale = min(
                _MAX_IMAGE_EDGE / max(size.width(), size.height()),
                (_MAX_IMAGE_PIXELS / (size.width() * size.height())) ** 0.5,
            )
            reader.setScaledSize(
                QSize(max(1, int(size.width() * scale)), max(1, int(size.height() * scale)))
            )
            self._image_downsampled = True
        image = reader.read()
        if image.isNull():
            self.message.setText(f"Qt could not decode this image: {reader.errorString()}")
            return
        self.image.set_pixmap(QPixmap.fromImage(image))
        self.preview_stack.setCurrentWidget(self.image)
        self.tabs.setCurrentWidget(self.preview_stack)
        if self._image_downsampled:
            self.caption.setText(
                f"{path} — safety-scaled from {size.width():,} x {size.height():,}"
            )

    def _show_markdown(self, path: Path) -> None:
        value, truncated = _bounded_text(path)
        self.markdown.setMarkdown(value)
        self.preview_stack.setCurrentWidget(self.markdown)
        self.tabs.setCurrentWidget(self.preview_stack)
        self._set_text(value, truncated, code=False)

    def _show_text(self, path: Path, *, code: bool) -> None:
        try:
            value, truncated = _bounded_text(path)
        except OSError as error:
            self.message.setText(str(error))
            return
        self._set_text(value, truncated, code=code)
        self.preview_stack.setCurrentWidget(self.message)
        self.message.setText(
            "Syntax-highlighted bounded source is available in Text / Code."
            if code
            else "Bounded text is available in Text / Code."
        )
        self.tabs.setCurrentWidget(self.text)

    def _set_text(self, value: str, truncated: bool, *, code: bool) -> None:
        suffix = "\n\n[Preview truncated after 1,000,000 bytes.]" if truncated else ""
        self.text.setPlainText(value + suffix)
        self.text.setProperty("truncated", truncated)
        self.highlighter.setDocument(self.text.document() if code else None)
        if not code:
            self.text.setExtraSelections([])

    def _build_media_view(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        if QAudioOutput is None or QMediaPlayer is None or QVideoWidget is None:
            self.video = QWidget()
            self.media_label = QLabel(
                "Embedded media preview is unavailable because this system is missing "
                "a Qt Multimedia runtime library. The file can still be opened externally."
            )
            self.media_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.media_label.setWordWrap(True)
            layout.addWidget(self.media_label, 1)
            controls = QHBoxLayout()
            self.play_button = QPushButton("Play")
            self.play_button.setEnabled(False)
            self.position = QSlider(Qt.Orientation.Horizontal)
            self.position.setEnabled(False)
            self.media_time = QLabel("00:00 / 00:00")
            controls.addWidget(self.play_button)
            controls.addWidget(self.position, 1)
            controls.addWidget(self.media_time)
            layout.addLayout(controls)
            self.player = None
            return widget
        self.video = QVideoWidget()
        self.video.setMinimumHeight(180)
        layout.addWidget(self.video, 1)
        self.media_label = QLabel("Select Play to open the local media stream.")
        self.media_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.media_label)
        controls = QHBoxLayout()
        self.play_button = QPushButton("Play")
        self.play_button.clicked.connect(self._toggle_media)
        self.position = QSlider(Qt.Orientation.Horizontal)
        self.position.sliderMoved.connect(self._seek_media)
        self.media_time = QLabel("00:00 / 00:00")
        controls.addWidget(self.play_button)
        controls.addWidget(self.position, 1)
        controls.addWidget(self.media_time)
        layout.addLayout(controls)
        self.audio_output = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video)
        self.audio_output.setVolume(0.7)
        self.player.positionChanged.connect(self._media_position_changed)
        self.player.durationChanged.connect(self._media_duration_changed)
        self.player.playbackStateChanged.connect(self._media_state_changed)
        self.player.errorOccurred.connect(self._media_error)
        return widget

    def _show_media(self, path: Path, *, video: bool) -> None:
        if self.player is None:
            self.preview_stack.setCurrentWidget(self.media)
            self.tabs.setCurrentWidget(self.preview_stack)
            return
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.video.setVisible(video)
        self.media_label.setText(path.name if video else f"Audio: {path.name}")
        self.preview_stack.setCurrentWidget(self.media)
        self.tabs.setCurrentWidget(self.preview_stack)

    def _toggle_media(self) -> None:
        if self.player is None:
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _stop_media(self) -> None:
        if getattr(self, "player", None) is not None:
            self.player.stop()
            self.player.setSource(QUrl())

    def _seek_media(self, position: int) -> None:
        if self.player is None:
            return
        self.player.setPosition(position)

    def _media_position_changed(self, position: int) -> None:
        if self.player is None:
            return
        self.position.setValue(position)
        self.media_time.setText(f"{_media_time(position)} / {_media_time(self.player.duration())}")

    def _media_duration_changed(self, duration: int) -> None:
        if self.player is None:
            return
        self.position.setRange(0, max(0, duration))
        self._media_position_changed(self.player.position())

    def _media_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        if self.player is None:
            return
        self.play_button.setText(
            "Pause" if state == QMediaPlayer.PlaybackState.PlayingState else "Play"
        )

    def _media_error(self, _error: QMediaPlayer.Error, message: str) -> None:
        if message:
            self.media_label.setText(f"Media could not be played: {message}")

    def _populate_metadata(self, record: dict[str, Any]) -> None:
        rows = list(_flatten_metadata(record))
        self.metadata_table.setSortingEnabled(False)
        self.metadata_table.setRowCount(len(rows))
        for index, (key, value) in enumerate(rows):
            self.metadata_table.setItem(index, 0, QTableWidgetItem(key))
            self.metadata_table.setItem(index, 1, QTableWidgetItem(value))
        self.metadata_table.setSortingEnabled(True)
        self.metadata_table.resizeColumnToContents(0)

    def _populate_archive(self, member_page: dict[str, Any] | None) -> None:
        self._archive_page = dict(member_page or {})
        members = list((member_page or {}).get("members", []))
        self.archive_table.setSortingEnabled(False)
        self.archive_table.setRowCount(len(members))
        fields = (
            "path",
            "uncompressed_size",
            "compressed_size",
            "modified_at",
            "encrypted",
            "crc32",
            "compression_method",
        )
        for row, member in enumerate(members):
            for column, field in enumerate(fields):
                value = member.get(field, "")
                item = QTableWidgetItem()
                if field in {"uncompressed_size", "compressed_size"} and isinstance(value, int):
                    item.setData(Qt.ItemDataRole.DisplayRole, value)
                    item.setToolTip(_format_bytes(value))
                else:
                    item.setText(str(value if value is not None else ""))
                self.archive_table.setItem(row, column, item)
        self.archive_table.setSortingEnabled(True)
        self.archive_table.resizeColumnToContents(0)
        if member_page is not None:
            offset = int(member_page.get("offset", 0))
            total = int(member_page.get("total", len(members)))
            end = min(total, offset + len(members))
            self.archive_status.setText(f"{offset + 1 if total else 0:,}-{end:,} of {total:,}")
            self.archive_previous.setEnabled(bool(self._archive_loader and offset > 0))
            self.archive_next.setEnabled(bool(self._archive_loader and member_page.get("has_more")))
            self.tabs.setTabVisible(self.archive_tab_index, True)

    def _load_archive_page(self, offset: int) -> None:
        if not self._archive_loader:
            return
        glob = self.archive_filter.text().strip() or "**"
        try:
            page = self._archive_loader(max(0, offset), 1_000, glob)
        except Exception as error:
            self.archive_status.setText(f"Archive index unavailable: {error}")
            return
        self._populate_archive(page)

    def _previous_archive(self) -> None:
        self._load_archive_page(int(self._archive_page.get("offset", 0)) - 1_000)

    def _next_archive(self) -> None:
        self._load_archive_page(int(self._archive_page.get("offset", 0)) + 1_000)

    def _filter_archive(self) -> None:
        self._load_archive_page(0)

    def _load_hex(self, offset: int) -> None:
        if not self._path or not self._path.is_file():
            return
        size = self._path.stat().st_size
        offset = max(0, min(offset, max(0, size - 1))) if size else 0
        with self._path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(_HEX_PAGE_BYTES)
        self._hex_offset = offset
        self.hex_offset.setText(f"0x{offset:X}")
        self.hex.setPlainText(_hex_dump(data, offset))
        self.hex.setProperty("bytesRead", len(data))
        end = offset + len(data)
        self.hex_status.setText(
            f"bytes {offset:,}-{max(offset, end - 1):,} of {size:,} ({_format_bytes(size)})"
        )

    def _previous_hex(self) -> None:
        self._load_hex(self._hex_offset - _HEX_PAGE_BYTES)

    def _next_hex(self) -> None:
        self._load_hex(self._hex_offset + _HEX_PAGE_BYTES)

    def _jump_hex(self) -> None:
        value = self.hex_offset.text().strip().casefold()
        try:
            offset = int(value, 16 if value.startswith("0x") else 10)
        except ValueError:
            self.hex_status.setText("Enter a decimal offset or a hexadecimal value beginning 0x.")
            return
        self._load_hex(offset)

    def _tab_changed(self, index: int) -> None:
        if index == self.hex_tab_index and self._path and not self.hex.toPlainText():
            try:
                self._load_hex(0)
            except OSError as error:
                self.hex.setPlainText(str(error))
        self._update_zoom_buttons()

    def _update_zoom_buttons(self) -> None:
        enabled = (
            self.tabs.currentWidget() is self.preview_stack
            and self.preview_stack.currentWidget() in {self.image, self.pdf}
        )
        self.zoom_in_button.setEnabled(enabled)
        self.zoom_out_button.setEnabled(enabled)
        self.zoom_reset_button.setEnabled(enabled)

    def _filesystem_metadata(self, path: Path) -> dict[str, Any]:
        result: dict[str, Any] = {
            "path": str(path),
            "extension": path.suffix.casefold(),
            "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        }
        try:
            stat = path.stat()
            result.update(
                {
                    "filesystem.size_bytes": stat.st_size,
                    "filesystem.modified_ns": stat.st_mtime_ns,
                    "filesystem.created_ns": stat.st_ctime_ns,
                    "filesystem.read_only": not bool(stat.st_mode & 0o200),
                }
            )
        except OSError as error:
            result["filesystem.error"] = str(error)
        return result

    def _open_external(self) -> None:
        if self._path and self._path.suffix.casefold() not in _UNSAFE_EXTERNAL_EXTENSIONS:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._path)))

    def _reveal_in_folder(self) -> None:
        if self._path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._path.parent)))

    def _open_safe_link(self, url: QUrl) -> None:
        if url.scheme().casefold() in {"https", "http"}:
            QDesktopServices.openUrl(url)

    def keyPressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.matches(QKeySequence.StandardKey.ZoomIn):
            self.zoom_in()
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.ZoomOut):
            self.zoom_out()
            event.accept()
            return
        super().keyPressEvent(event)


class DocumentRepairPreview(FilePreview):
    """PDF inspector with separate, non-destructive OCR and compression proposal views."""

    def __init__(self) -> None:
        super().__init__()
        self.proposed_ocr = QPlainTextEdit()
        self.proposed_ocr.setReadOnly(True)
        self.proposed_ocr.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.proposed_ocr.setFont(QFont("Consolas"))
        self.proposed_ocr.setPlaceholderText("No OCR text proposal has been generated.")
        self.proposed_ocr_tab_index = self.tabs.addTab(self.proposed_ocr, "Proposed OCR Text")

        self.compression_stack = QStackedWidget()
        self.compression_message = QLabel("No image-compression proposal has been generated.")
        self.compression_message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.compression_message.setWordWrap(True)
        self.proposed_pdf_document = QPdfDocument(self)
        self.proposed_pdf = PannablePdfView()
        self.proposed_pdf.setDocument(self.proposed_pdf_document)
        self.compression_stack.addWidget(self.compression_message)
        self.compression_stack.addWidget(self.proposed_pdf)
        self.proposed_compression_tab_index = self.tabs.addTab(
            self.compression_stack, "Proposed Compression"
        )

    def clear(self) -> None:
        super().clear()
        if hasattr(self, "proposed_ocr"):
            self.proposed_ocr.clear()
        if hasattr(self, "proposed_pdf_document"):
            self.proposed_pdf_document.close()
            self.compression_message.setText("No image-compression proposal has been generated.")
            self.compression_stack.setCurrentWidget(self.compression_message)

    def set_repair_proposals(
        self,
        *,
        proposed_ocr_text: str,
        compression_path: Path | None,
        compression_summary: str,
    ) -> None:
        self.proposed_ocr.setPlainText(proposed_ocr_text[:_MAX_TEXT_BYTES])
        self.proposed_ocr.setProperty("truncated", len(proposed_ocr_text) > _MAX_TEXT_BYTES)
        self.proposed_pdf_document.close()
        if compression_path and compression_path.is_file():
            error = self.proposed_pdf_document.load(str(compression_path))
            if error == QPdfDocument.Error.None_:
                self.compression_stack.setCurrentWidget(self.proposed_pdf)
                self.proposed_pdf.setToolTip(compression_summary)
                return
            compression_summary = f"{compression_summary}\nPreview error: {error.name}"
        self.compression_message.setText(compression_summary)
        self.compression_stack.setCurrentWidget(self.compression_message)


def _table(headers: list[str], accessible_name: str) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setSortingEnabled(True)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.setAccessibleName(accessible_name)
    table.horizontalHeader().setStretchLastSection(True)
    return table


def _tool_button(text: str, tooltip: str, callback) -> QToolButton:  # type: ignore[no-untyped-def]
    button = QToolButton()
    button.setText(text)
    button.setToolTip(tooltip)
    button.clicked.connect(callback)
    return button


def _bounded_text(path: Path) -> tuple[str, bool]:
    with path.open("rb") as stream:
        data = stream.read(_MAX_TEXT_BYTES + 1)
    truncated = len(data) > _MAX_TEXT_BYTES
    bounded = data[:_MAX_TEXT_BYTES]
    if bounded.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        encoding = "utf-32"
    elif bounded.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    elif bounded.startswith(codecs.BOM_UTF8):
        encoding = "utf-8-sig"
    else:
        try:
            return bounded.decode("utf-8"), truncated
        except UnicodeDecodeError:
            encoding = "utf-16" if bounded[:1] == b"\x00" or b"\x00" in bounded[:64] else "cp1252"
    return bounded.decode(encoding, errors="replace"), truncated


def _flatten_metadata(value: Any, prefix: str = ""):
    if isinstance(value, dict):
        for key in sorted(value, key=lambda item: str(item).casefold()):
            child = f"{prefix}.{key}" if prefix else str(key)
            yield from _flatten_metadata(value[key], child)
        return
    if isinstance(value, (list, tuple, set)):
        text = json.dumps(list(value), ensure_ascii=False, default=str)
    elif value is None:
        text = ""
    elif isinstance(value, bool):
        text = "Yes" if value else "No"
    else:
        text = str(value)
    yield prefix or "value", text


def _hex_dump(data: bytes, offset: int) -> str:
    rows = []
    width = max(8, len(f"{offset + len(data):X}"))
    for start in range(0, len(data), 16):
        block = data[start : start + 16]
        left = " ".join(f"{value:02X}" for value in block[:8])
        right = " ".join(f"{value:02X}" for value in block[8:])
        hexadecimal = f"{left:<23}  {right:<23}"
        printable = "".join(chr(value) if 32 <= value < 127 else "." for value in block)
        rows.append(f"{offset + start:0{width}X}  {hexadecimal}  |{printable:<16}|")
    return "\n".join(rows)


def _format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("bytes", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:,.0f} {unit}" if unit == "bytes" else f"{amount:,.1f} {unit}"
        amount /= 1024
    return f"{value:,} bytes"


def _media_time(milliseconds: int) -> str:
    seconds = max(0, milliseconds // 1000)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"
