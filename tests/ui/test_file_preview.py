from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from PySide6.QtGui import QImage, QPainter, QPdfWriter

from ai_organizer.desktop.controller import WorkspaceController
from ai_organizer.desktop.pages import InventoryPage, RenamePage, UpdatesPage
from ai_organizer.desktop.preview import FilePreview


@pytest.mark.ui
def test_shared_inspector_has_bounded_text_metadata_and_seekable_hex(qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "large.py"
    path.write_bytes(b"def example():\n    return 42\n" + b"# padding\n" * 120_000)
    inspector = FilePreview()
    qtbot.addWidget(inspector)

    inspector.show_path(path, metadata={"line_count": 120_002}, record={"id": "item-1"})

    assert [inspector.tabs.tabText(index) for index in range(inspector.tabs.count())] == [
        "Preview",
        "Text / Code",
        "Metadata",
        "Archive",
        "Hex",
    ]
    assert inspector.text_is_truncated
    assert "def example" in inspector.text.toPlainText()
    assert inspector.metadata_table.rowCount() >= 6
    inspector.tabs.setCurrentIndex(inspector.hex_tab_index)
    assert inspector.hex_bytes_read == 4096
    inspector.hex_offset.setText("0x1000")
    inspector._jump_hex()
    assert inspector.hex.toPlainText().startswith("00001000")
    assert inspector.hex_bytes_read == 4096


@pytest.mark.ui
def test_image_and_pdf_are_legit_zoomable_previews(qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    image_path = tmp_path / "receipt.png"
    image = QImage(800, 1200, QImage.Format.Format_RGB32)
    image.fill(0xFFFDF7)
    assert image.save(str(image_path))
    inspector = FilePreview()
    qtbot.addWidget(inspector)

    inspector.show_path(image_path)
    assert inspector.preview_stack.currentWidget() is inspector.image
    before = inspector.image.transform().m11()
    inspector.zoom_in()
    assert inspector.image.transform().m11() > before
    inspector.reset_zoom()

    pdf_path = tmp_path / "statement.pdf"
    writer = QPdfWriter(str(pdf_path))
    painter = QPainter(writer)
    painter.drawText(100, 100, "Statement date: 2026-07-15")
    painter.end()
    inspector.show_path(pdf_path)
    assert inspector.preview_stack.currentWidget() is inspector.pdf
    assert inspector.pdf_document.pageCount() == 1
    inspector.zoom_in()
    assert inspector.pdf.zoomMode() == inspector.pdf.ZoomMode.Custom


@pytest.mark.ui
def test_markdown_archive_and_media_have_friendly_views(qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    inspector = FilePreview()
    qtbot.addWidget(inspector)
    markdown = tmp_path / "notes.md"
    markdown.write_text("# Receipt notes\n\n- paid\n", encoding="utf-8")

    inspector.show_path(markdown)
    assert inspector.preview_stack.currentWidget() is inspector.markdown
    assert "Receipt notes" in inspector.markdown.toPlainText()
    assert "# Receipt notes" in inspector.text.toPlainText()

    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("documents/receipt.pdf", b"example")
    members = {
        "members": [
            {
                "path": "documents/receipt.pdf",
                "uncompressed_size": 7,
                "compressed_size": 7,
                "modified_at": "2026-07-15T10:00:00",
                "encrypted": False,
                "crc32": "12345678",
                "compression_method": "stored",
            }
        ],
        "total": 1,
    }
    calls = []

    def load_members(offset: int, limit: int, glob: str):  # type: ignore[no-untyped-def]
        calls.append((offset, limit, glob))
        return {**members, "offset": offset, "glob": glob, "has_more": False}

    inspector.show_archive(
        archive,
        {"archive_format": "zip"},
        members,
        member_loader=load_members,
    )
    assert inspector.tabs.isTabVisible(inspector.archive_tab_index)
    assert inspector.archive_table.rowCount() == 1
    assert inspector.archive_table.item(0, 0).text() == "documents/receipt.pdf"
    inspector.archive_filter.setText("**/*.pdf")
    inspector._filter_archive()
    assert calls == [(0, 1_000, "**/*.pdf")]

    media = tmp_path / "interview.mp3"
    media.write_bytes(b"not played until requested")
    inspector.show_path(media)
    assert inspector.preview_stack.currentWidget() is inspector.media
    assert inspector.play_button.text() == "Play"


@pytest.mark.ui
def test_file_backed_pages_use_the_shared_inspector(qtbot) -> None:  # type: ignore[no-untyped-def]
    controller = WorkspaceController()
    pages = [InventoryPage(controller), UpdatesPage(controller), RenamePage(controller)]
    for page in pages:
        qtbot.addWidget(page)
        assert page.findChildren(FilePreview)
