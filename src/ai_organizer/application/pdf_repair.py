from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from ai_organizer.domain.prompts import sensitive_fragments


@dataclass(frozen=True, slots=True)
class PdfCompressionAssessment:
    source_bytes: int
    page_count: int
    image_count: int
    encoded_image_bytes: int
    largest_image_pixels: int
    highest_full_page_dpi: int
    digitally_signed: bool
    encrypted: bool
    candidate: bool
    reason: str
    target_dpi: int = 200
    jpeg_quality: int = 85

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PdfCompressionProposal:
    output_path: Path
    source_bytes: int
    proposed_bytes: int
    images_replaced: int
    page_count: int
    page_boxes_match: bool

    @property
    def savings_bytes(self) -> int:
        return max(0, self.source_bytes - self.proposed_bytes)

    @property
    def savings_percent(self) -> float:
        return 100 * self.savings_bytes / max(1, self.source_bytes)


@dataclass(frozen=True, slots=True)
class OcrCorrectionEnvelope:
    """Keep sensitive OCR values local while allowing bounded surrounding-text correction."""

    redacted_text: str
    protected_values: tuple[tuple[str, str], ...]

    @classmethod
    def create(
        cls, text: str, sensitive_values: list[str] | tuple[str, ...]
    ) -> OcrCorrectionEnvelope:
        if re.search(r"\[\[AIORGANIZER_PRIVATE_\d{4}\]\]", text):
            raise ValueError("OCR text contains a reserved private placeholder")
        values = sorted(
            {value for value in sensitive_values if value},
            key=len,
            reverse=True,
        )
        protected: list[tuple[str, str]] = []
        redacted = text
        for value in values:
            pattern = re.compile(re.escape(value), re.IGNORECASE)

            def replace(match: re.Match[str]) -> str:
                placeholder = f"[[AIORGANIZER_PRIVATE_{len(protected) + 1:04d}]]"
                protected.append((placeholder, match.group(0)))
                return placeholder

            redacted = pattern.sub(replace, redacted)
        protected_by_placeholder = dict(protected)
        ordered = tuple(
            (placeholder, protected_by_placeholder[placeholder])
            for placeholder in re.findall(r"\[\[AIORGANIZER_PRIVATE_\d{4}\]\]", redacted)
        )
        return cls(redacted, ordered)

    @classmethod
    def create_sanitized(
        cls, text: str, private_terms: list[str] | tuple[str, ...] = ()
    ) -> OcrCorrectionEnvelope:
        return cls.create(text, [*private_terms, *sensitive_fragments(text)])

    def restore(self, corrected_text: str) -> str:
        expected = [placeholder for placeholder, _ in self.protected_values]
        found = re.findall(r"\[\[AIORGANIZER_PRIVATE_\d{4}\]\]", corrected_text)
        if found != expected or any(corrected_text.count(value) != 1 for value in expected):
            raise ValueError(
                "AI correction changed, reordered, duplicated, or removed a private placeholder"
            )
        restored = corrected_text
        for placeholder, original in self.protected_values:
            restored = restored.replace(placeholder, original)
        return restored


@dataclass(frozen=True, slots=True)
class OcrLineCorrectionEnvelope:
    item_id: str
    page_index: int
    line_id: str
    bounds: tuple[float, float, float, float]
    confidence: float
    original_text: str
    envelope: OcrCorrectionEnvelope

    def request_payload(self) -> dict[str, str]:
        return {"item_id": self.item_id, "text": self.envelope.redacted_text}


def prepare_ocr_line_corrections(
    item_id: str,
    layout_pages: dict[str, Any],
    private_terms: list[str] | tuple[str, ...] = (),
) -> list[OcrLineCorrectionEnvelope]:
    """Prepare positioned OCR lines for reversible, redacted AI correction."""
    prepared: list[OcrLineCorrectionEnvelope] = []
    for page_key, page in sorted(layout_pages.items(), key=lambda value: int(value[0])):
        if not isinstance(page, dict):
            continue
        page_index = int(page.get("page_index", page_key))
        for line_number, line in enumerate(page.get("lines", [])):
            if not isinstance(line, dict):
                continue
            text = str(line.get("text", "")).strip()
            if not text:
                continue
            if len(text) > 500:
                raise ValueError("An OCR line is too long for bounded AI correction")
            line_id = str(line.get("line_id", f"line-{line_number:04d}"))
            correction_id = f"{item_id}:p{page_index:04d}:{line_id}"[:200]
            raw_bounds = line.get("bounds", (0.0, 0.0, 1.0, 1.0))
            if not isinstance(raw_bounds, (list, tuple)) or len(raw_bounds) != 4:
                raise ValueError("OCR line is missing normalized position bounds")
            x, y, width, height = (max(0.0, min(1.0, float(value))) for value in raw_bounds)
            bounds = (x, y, min(width, 1.0 - x), min(height, 1.0 - y))
            envelope = OcrCorrectionEnvelope.create_sanitized(text, private_terms)
            if len(envelope.redacted_text) > 500:
                raise ValueError("A redacted OCR line is too long for bounded AI correction")
            prepared.append(
                OcrLineCorrectionEnvelope(
                    correction_id,
                    page_index,
                    line_id,
                    bounds,
                    float(line.get("confidence", 0.0)),
                    text,
                    envelope,
                )
            )
    if len({value.item_id for value in prepared}) != len(prepared):
        raise ValueError("OCR layout contains duplicate positioned line identifiers")
    return prepared


def restore_ocr_line_corrections(
    prepared: list[OcrLineCorrectionEnvelope], findings: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Validate exact line identity and private-placeholder preservation before accepting AI text."""
    expected = {value.item_id: value for value in prepared}
    returned: dict[str, dict[str, Any]] = {}
    for finding in findings:
        item_id = str(finding.get("item_id", ""))
        if not item_id or item_id in returned:
            raise ValueError("AI OCR correction returned a missing or duplicate line identifier")
        returned[item_id] = finding
    if set(returned) != set(expected):
        raise ValueError("AI OCR correction omitted or invented positioned line identifiers")
    corrected = []
    for item_id, value in expected.items():
        suggestion = str(returned[item_id].get("suggestion", "")).strip()
        if not suggestion:
            raise ValueError("AI OCR correction returned an empty positioned line")
        restored = value.envelope.restore(suggestion)
        similarity = SequenceMatcher(
            None, value.original_text.casefold(), restored.casefold(), autojunk=False
        ).ratio()
        length_ratio = len(restored) / max(1, len(value.original_text))
        if similarity < 0.55 or not 0.5 <= length_ratio <= 2.0:
            raise ValueError("AI OCR correction rewrote a line beyond the safe correction bound")
        corrected.append(
            {
                "item_id": item_id,
                "page_index": value.page_index,
                "line_id": value.line_id,
                "bounds": list(value.bounds),
                "ocr_text": value.original_text,
                "corrected_text": restored,
                "confidence": float(returned[item_id].get("confidence", 0.0)),
            }
        )
    return corrected


def positioned_text(corrections: list[dict[str, Any]]) -> str:
    pages: dict[int, list[str]] = {}
    for correction in corrections:
        pages.setdefault(int(correction["page_index"]), []).append(
            str(correction["corrected_text"])
        )
    return "\n\n".join(
        f"--- Page {page_index + 1} ---\n" + "\n".join(lines)
        for page_index, lines in sorted(pages.items())
    )


class PdfRepairAnalyzer:
    """Inspect PDFs and create cache-only compression proposals without touching sources."""

    def assess(self, path: Path) -> PdfCompressionAssessment:
        from pypdf import PdfReader
        from pypdf.generic import DictionaryObject

        reader = PdfReader(path, strict=False)
        encrypted = bool(reader.is_encrypted)
        if encrypted:
            return PdfCompressionAssessment(
                path.stat().st_size,
                0,
                0,
                0,
                0,
                0,
                False,
                True,
                False,
                "Encrypted PDF requires an explicitly unlocked copy before repair",
            )
        image_count = 0
        encoded_image_bytes = 0
        largest_image_pixels = 0
        highest_full_page_dpi = 0
        seen: set[tuple[int, int]] = set()
        inefficient_filters = False
        for page in reader.pages:
            page_width_inches = max(0.01, float(page.mediabox.width) / 72)
            page_height_inches = max(0.01, float(page.mediabox.height) / 72)
            for image_file in page.images:
                reference = image_file.indirect_reference
                identity = (
                    (reference.idnum, reference.generation)
                    if reference is not None
                    else (id(image_file), 0)
                )
                if identity in seen:
                    continue
                seen.add(identity)
                image_count += 1
                candidate_object = reference.get_object() if reference is not None else None
                image_object = (
                    candidate_object if isinstance(candidate_object, DictionaryObject) else None
                )
                if image_object is not None:
                    width = int(image_object.get("/Width", 0))
                    height = int(image_object.get("/Height", 0))
                    encoded_image_bytes += len(getattr(image_object, "_data", b""))
                else:
                    image = image_file.image
                    width, height = image.size if image is not None else (0, 0)
                    encoded_image_bytes += len(image_file.data)
                pixels = width * height
                largest_image_pixels = max(largest_image_pixels, pixels)
                highest_full_page_dpi = max(
                    highest_full_page_dpi,
                    round(
                        max(
                            width / page_width_inches,
                            height / page_height_inches,
                        )
                    ),
                )
                if image_object is not None:
                    filters = str(image_object.get("/Filter", ""))
                    inefficient_filters |= not any(
                        value in filters for value in ("DCTDecode", "JPXDecode", "CCITTFaxDecode")
                    )
        source_bytes = path.stat().st_size
        image_share = encoded_image_bytes / max(1, source_bytes)
        candidate = bool(
            image_count
            and source_bytes >= 1_000_000
            and image_share >= 0.35
            and (
                highest_full_page_dpi > 240
                or largest_image_pixels > 4_000_000
                or inefficient_filters
            )
        )
        if not image_count:
            reason = "No raster images were found; image recompression is unlikely to help"
        elif source_bytes < 1_000_000:
            reason = "PDF is already below the repair size threshold"
        elif image_share < 0.35:
            reason = "Raster images are not the dominant source of file size"
        elif candidate:
            reason = (
                "Large or inefficient raster images may benefit from a 200 DPI, quality-85 preview"
            )
        else:
            reason = "Images already appear reasonably sized and encoded"
        return PdfCompressionAssessment(
            source_bytes,
            len(reader.pages),
            image_count,
            encoded_image_bytes,
            largest_image_pixels,
            highest_full_page_dpi,
            _is_digitally_signed(reader),
            False,
            candidate,
            reason,
        )

    def build_compression_preview(
        self,
        source: Path,
        output: Path,
        assessment: PdfCompressionAssessment,
    ) -> PdfCompressionProposal:
        from PIL import Image
        from pypdf import PdfReader, PdfWriter

        if assessment.encrypted:
            raise ValueError("Encrypted PDF cannot be repaired without an unlocked copy")
        output.parent.mkdir(parents=True, exist_ok=True)
        writer = PdfWriter(clone_from=source)
        images_replaced = 0
        seen: set[tuple[int, int]] = set()
        for page in writer.pages:
            max_width = max(1, round(float(page.mediabox.width) / 72 * assessment.target_dpi))
            max_height = max(1, round(float(page.mediabox.height) / 72 * assessment.target_dpi))
            for image_file in page.images:
                reference = image_file.indirect_reference
                if reference is None:
                    continue
                identity = (reference.idnum, reference.generation)
                if identity in seen:
                    continue
                seen.add(identity)
                image = image_file.image
                if image is None:
                    continue
                if image.mode == "1" or image.width * image.height < 500_000:
                    continue
                scale = min(1.0, max_width / image.width, max_height / image.height)
                replacement = image
                if scale < 0.98:
                    replacement = image.resize(
                        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                        Image.Resampling.LANCZOS,
                    )
                if replacement.mode not in {"RGB", "L"}:
                    replacement = _flatten_to_rgb(replacement)
                image_file.replace(
                    replacement,
                    quality=assessment.jpeg_quality,
                    optimize=True,
                )
                images_replaced += 1
        writer.write(output)
        proposed = PdfReader(output, strict=False)
        source_reader = PdfReader(source, strict=False)
        boxes_match = len(proposed.pages) == len(source_reader.pages) and all(
            tuple(float(value) for value in proposed.pages[index].mediabox)
            == tuple(float(value) for value in source_reader.pages[index].mediabox)
            for index in range(len(source_reader.pages))
        )
        if not boxes_match:
            raise RuntimeError("Compression preview changed page count or page geometry")
        return PdfCompressionProposal(
            output,
            source.stat().st_size,
            output.stat().st_size,
            images_replaced,
            len(proposed.pages),
            boxes_match,
        )


def _flatten_to_rgb(image: Any) -> Any:
    from PIL import Image

    if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _is_digitally_signed(reader) -> bool:  # type: ignore[no-untyped-def]
    root = reader.trailer.get("/Root", {})
    if root.get("/Perms"):
        return True
    form = root.get("/AcroForm")
    if not form:
        return False
    pending = list(form.get_object().get("/Fields", []))
    while pending:
        field = pending.pop().get_object()
        if field.get("/FT") == "/Sig":
            return True
        pending.extend(field.get("/Kids", []))
    return False
