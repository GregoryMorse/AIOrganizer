from __future__ import annotations

from pathlib import Path

import pytest

from ai_organizer.application.pdf_repair import (
    OcrCorrectionEnvelope,
    PdfRepairAnalyzer,
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
                    "text": "Pay acount 1234-5678-9012-3456",
                    "bounds": [0.1, 0.2, 0.7, 0.04],
                    "confidence": 0.72,
                },
                {
                    "line_id": "b001-p001-l002",
                    "text": "Due tomorow",
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
            "suggestion": prepared[0].envelope.redacted_text.replace("acount", "account"),
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
    invented[1] = {**findings[1], "suggestion": "A completely unrelated invented sentence"}
    with pytest.raises(ValueError, match="safe correction bound"):
        restore_ocr_line_corrections(prepared, invented)


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
