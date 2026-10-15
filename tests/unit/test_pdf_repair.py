from __future__ import annotations

from pathlib import Path

import pytest

from ai_organizer.application.pdf_repair import (
    OcrCorrectionEnvelope,
    PdfRepairAnalyzer,
    build_searchable_pdf,
    commit_repaired_pdf,
    positioned_text,
    prepare_ocr_line_corrections,
    restore_ocr_line_corrections,
)


def test_ocr_correction_envelope_round_trips_private_values() -> None:
    envelope = OcrCorrectionEnvelope.create("Pay account 1234-5678 tomorrow.", ["1234-5678"])
    assert "1234-5678" not in envelope.redacted_text
    corrected = envelope.redacted_text.replace("Pay account", "Pay the account")
    assert envelope.restore(corrected) == "Pay the account 1234-5678 tomorrow."
    with pytest.raises(ValueError, match="placeholder"):
        envelope.restore(corrected.replace("[[AIORGANIZER_PRIVATE_0001]]", ""))


def test_ocr_sanitizer_round_trips_detected_and_repeated_private_values() -> None:
    original = "Greg pays account 1234-5678-9012-3456. Send the receipt to Greg."

    envelope = OcrCorrectionEnvelope.create_sanitized(original, ["Greg"])

    assert "Greg" not in envelope.redacted_text
    assert "1234-5678-9012-3456" not in envelope.redacted_text
    assert len(envelope.protected_values) == 3
    assert envelope.restore(envelope.redacted_text) == original


def test_positioned_ocr_correction_preserves_line_ids_bounds_and_placeholders() -> None:
    layout = {
        "0": {
            "page_index": 0,
            "lines": [
                {
                    "line_id": "b001-p001-l001",
                    "text": "Pay acconut 1234-5678-9012-3456",
                    "bounds": [0.1, 0.2, 0.7, 0.04],
                    "confidence": 0.72,
                },
                {
                    "line_id": "b001-p001-l002",
                    "text": "Due tomorrov",
                    "bounds": [0.1, 0.25, 0.3, 0.04],
                    "confidence": 0.8,
                },
            ],
        }
    }
    prepared = prepare_ocr_line_corrections("item_pdf", layout)
    findings = [
        {
            "item_id": prepared[0].item_id,
            "suggestion": prepared[0].envelope.redacted_text.replace("acconut", "account"),
            "confidence": 0.95,
        },
        {
            "item_id": prepared[1].item_id,
            "suggestion": "Due tomorrow",
            "confidence": 0.98,
        },
    ]

    corrected = restore_ocr_line_corrections(prepared, findings)

    assert corrected[0]["corrected_text"] == "Pay account 1234-5678-9012-3456"
    assert corrected[0]["bounds"] == [0.1, 0.2, 0.7, 0.04]
    assert corrected[1]["line_id"] == "b001-p001-l002"
    assert "--- Page 1 ---" in positioned_text(corrected)
    with pytest.raises(ValueError, match="omitted or invented"):
        restore_ocr_line_corrections(prepared, findings[:1])
    invented = [*findings]
    invented[1] = {**findings[1], "suggestion": "Bad xxxxxxxx"}
    with pytest.raises(ValueError, match="safe correction bound"):
        restore_ocr_line_corrections(prepared, invented)
    moved_space = [*findings]
    moved_space[1] = {**findings[1], "suggestion": "Duet omorrow"}
    with pytest.raises(ValueError, match="whitespace"):
        restore_ocr_line_corrections(prepared, moved_space)
    lengthened = [*findings]
    lengthened[1] = {**findings[1], "suggestion": "Due tomorrow!"}
    with pytest.raises(ValueError, match="line length"):
        restore_ocr_line_corrections(prepared, lengthened)


def test_pdf_compression_preview_preserves_page_geometry(tmp_path: Path) -> None:
    from PIL import Image

    source = tmp_path / "large-scan.pdf"
    output = tmp_path / "compressed-preview.pdf"
    image = Image.effect_noise((2400, 3200), 100).convert("RGB")
    image.save(source, "PDF", resolution=300, quality=100)

    analyzer = PdfRepairAnalyzer()
    assessment = analyzer.assess(source)
    assert assessment.candidate is True
    proposal = analyzer.build_compression_preview(source, output, assessment)

    assert proposal.images_replaced == 1
    assert proposal.page_count == 1
    assert proposal.page_boxes_match is True
    assert output.is_file()
    assert proposal.proposed_bytes < proposal.source_bytes


def test_searchable_pdf_uses_positioned_corrected_words(tmp_path: Path) -> None:
    from PIL import Image
    from pypdf import PdfReader

    source = tmp_path / "scan.pdf"
    output = tmp_path / "searchable.pdf"
    Image.new("RGB", (600, 800), "white").save(source, "PDF", resolution=100)
    layout = {
        "0": {
            "page_index": 0,
            "lines": [
                {
                    "line_id": "b001-p001-l001",
                    "text": "Pay acconut",
                    "bounds": [0.1, 0.2, 0.4, 0.04],
                    "words": [
                        {"text": "Pay", "bounds": [0.1, 0.2, 0.08, 0.04]},
                        {"text": "acconut", "bounds": [0.22, 0.2, 0.2, 0.04]},
                    ],
                }
            ],
        }
    }
    corrections = [
        {
            "page_index": 0,
            "line_id": "b001-p001-l001",
            "corrected_text": "Pay account",
        }
    ]

    proposal = build_searchable_pdf(
        source, output, layout, corrections, embedded_pages=["defective embedded text"]
    )

    assert proposal.page_count == 1
    assert proposal.searchable_pages == 1
    assert proposal.rasterized_pages == 1
    assert proposal.positioned_lines == 1
    assert proposal.page_boxes_match is True
    assert "Pay account" in (PdfReader(output).pages[0].extract_text() or "")


def test_commit_repaired_pdf_retains_unsigned_original(tmp_path: Path) -> None:
    from PIL import Image

    root = tmp_path / "root"
    root.mkdir()
    source = root / "statement.pdf"
    prepared = tmp_path / "prepared.pdf"
    Image.new("RGB", (300, 400), "white").save(source, "PDF", resolution=100)
    original_bytes = source.read_bytes()
    Image.new("RGB", (300, 400), "gray").save(prepared, "PDF", resolution=100)

    result = commit_repaired_pdf(
        source,
        prepared,
        root,
        source.name,
        digitally_signed=False,
        repair_kind="compression",
    )

    assert result.overwritten is True
    assert result.output_path == source
    assert result.backup_path is not None
    assert result.backup_path.read_bytes() == original_bytes
    assert source.read_bytes() == prepared.read_bytes()


def test_commit_signed_pdf_creates_neighbor_and_preserves_original(tmp_path: Path) -> None:
    from PIL import Image

    root = tmp_path / "root"
    root.mkdir()
    source = root / "signed.pdf"
    prepared = tmp_path / "prepared.pdf"
    Image.new("RGB", (300, 400), "white").save(source, "PDF", resolution=100)
    original_bytes = source.read_bytes()
    Image.new("RGB", (300, 400), "gray").save(prepared, "PDF", resolution=100)

    result = commit_repaired_pdf(
        source,
        prepared,
        root,
        source.name,
        digitally_signed=True,
        repair_kind="ocr",
    )

    assert result.overwritten is False
    assert result.output_path.name == "signed-rebuilt-with-text.pdf"
    assert source.read_bytes() == original_bytes
    assert result.output_path.read_bytes() == prepared.read_bytes()


def test_commit_repaired_pdf_rejects_mismatched_relative_path(tmp_path: Path) -> None:
    from PIL import Image

    root = tmp_path / "root"
    root.mkdir()
    source = root / "statement.pdf"
    prepared = tmp_path / "prepared.pdf"
    Image.new("RGB", (300, 400), "white").save(source, "PDF", resolution=100)
    Image.new("RGB", (300, 400), "gray").save(prepared, "PDF", resolution=100)

    with pytest.raises(PermissionError, match="relative path"):
        commit_repaired_pdf(
            source,
            prepared,
            root,
            "../statement.pdf",
            digitally_signed=False,
            repair_kind="ocr",
        )
