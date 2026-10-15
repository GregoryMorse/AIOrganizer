from __future__ import annotations

import os
import re
import shutil
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from io import BytesIO
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

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
class PdfOcrProposal:
    output_path: Path
    page_count: int
    searchable_pages: int
    rasterized_pages: int
    positioned_lines: int
    page_boxes_match: bool


@dataclass(frozen=True, slots=True)
class PdfRepairCommitResult:
    source_path: Path
    output_path: Path
    backup_path: Path | None
    overwritten: bool
    repair_kind: Literal["ocr", "compression", "both"]


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
        if len(restored) != len(value.original_text):
            raise ValueError("AI OCR correction changed positioned-line length")
        original_spaces = tuple(
            index for index, character in enumerate(value.original_text) if character.isspace()
        )
        restored_spaces = tuple(
            index for index, character in enumerate(restored) if character.isspace()
        )
        if restored_spaces != original_spaces:
            raise ValueError("AI OCR correction changed positioned-line whitespace")
        similarity = SequenceMatcher(
            None, value.original_text.casefold(), restored.casefold(), autojunk=False
        ).ratio()
        if similarity < 0.8:
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


def build_searchable_pdf(
    source: Path,
    output: Path,
    layout_pages: dict[str, Any],
    corrections: list[dict[str, Any]] | None = None,
    embedded_pages: list[str] | tuple[str, ...] = (),
    *,
    raster_dpi: int = 300,
) -> PdfOcrProposal:
    """Build a visually reviewed PDF with bounded, positioned invisible OCR text.

    Image-only pages retain their original PDF content. Pages with a defective
    embedded text layer are rasterized at high resolution before the corrected
    OCR layer is added, avoiding duplicate searchable text while preserving the
    reviewed visual appearance.
    """
    from PIL import Image
    from pypdf import PdfReader, PdfWriter
    from PySide6.QtCore import QSize
    from PySide6.QtPdf import QPdfDocument

    if not layout_pages:
        raise ValueError("No positioned OCR pages are available")
    source = source.resolve(strict=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    reader = PdfReader(source, strict=False)
    if reader.is_encrypted:
        raise ValueError("Encrypted PDF cannot receive a searchable OCR layer")
    corrections_by_line = {
        (int(value["page_index"]), str(value["line_id"])): str(value["corrected_text"])
        for value in corrections or []
    }
    parsed_layout = {
        int(page_key): page for page_key, page in layout_pages.items() if isinstance(page, dict)
    }
    if any(index < 0 or index >= len(reader.pages) for index in parsed_layout):
        raise ValueError("OCR layout references a page outside the PDF")
    document = QPdfDocument()
    if document.load(str(source)) != QPdfDocument.Error.None_:
        raise ValueError("Qt could not render the OCR proposal source")
    writer = PdfWriter()
    rasterized_pages = 0
    positioned_lines = 0
    try:
        for page_index, source_page in enumerate(reader.pages):
            layout = parsed_layout.get(page_index)
            has_embedded_text = bool(
                page_index < len(embedded_pages) and str(embedded_pages[page_index]).strip()
            )
            if layout is not None and has_embedded_text:
                points = document.pagePointSize(page_index)
                target_width = max(1, round(points.width() / 72 * raster_dpi))
                target_height = max(1, round(points.height() / 72 * raster_dpi))
                scale = min(1.0, 4800 / max(target_width, target_height))
                image = document.render(
                    page_index,
                    QSize(
                        max(1, round(target_width * scale)),
                        max(1, round(target_height * scale)),
                    ),
                )
                if image.isNull():
                    raise RuntimeError(f"Could not render PDF page {page_index + 1}")
                buffer = BytesIO()
                pil_image = Image.fromqimage(image).convert("RGB")
                pil_image.save(buffer, "PDF", resolution=raster_dpi, quality=95)
                buffer.seek(0)
                raster_page = PdfReader(buffer, strict=False).pages[0]
                writer.add_page(raster_page)
                writer.pages[-1].scale_to(
                    float(source_page.mediabox.width), float(source_page.mediabox.height)
                )
                rasterized_pages += 1
            else:
                writer.add_page(source_page)
            if layout is not None:
                lines = [value for value in layout.get("lines", []) if isinstance(value, dict)]
                positioned_lines += _append_invisible_ocr_lines(
                    writer,
                    writer.pages[-1],
                    page_index,
                    lines,
                    corrections_by_line,
                )
        if reader.metadata:
            writer.add_metadata({str(key): str(value) for key, value in reader.metadata.items()})
        writer.write(output)
    finally:
        document.close()
    proposed = PdfReader(output, strict=False)
    boxes_match = len(proposed.pages) == len(reader.pages) and all(
        tuple(float(value) for value in proposed.pages[index].mediabox)
        == tuple(float(value) for value in reader.pages[index].mediabox)
        for index in range(len(reader.pages))
    )
    if not boxes_match:
        output.unlink(missing_ok=True)
        raise RuntimeError("OCR proposal changed page count or page geometry")
    return PdfOcrProposal(
        output,
        len(proposed.pages),
        len(parsed_layout),
        rasterized_pages,
        positioned_lines,
        boxes_match,
    )


def commit_repaired_pdf(
    source: Path,
    prepared: Path,
    root: Path,
    relative_path: str,
    *,
    digitally_signed: bool,
    repair_kind: Literal["ocr", "compression", "both"],
) -> PdfRepairCommitResult:
    """Atomically commit a validated PDF, retaining originals for recovery."""
    from pypdf import PdfReader

    source = source.resolve(strict=True)
    prepared = prepared.resolve(strict=True)
    root = root.resolve(strict=True)
    try:
        source.relative_to(root)
    except ValueError as error:
        raise PermissionError("PDF repair source is outside its configured root") from error
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts or (root / relative).resolve() != source:
        raise PermissionError("PDF repair relative path does not identify the source")
    if source == prepared:
        raise ValueError("Prepared PDF must be separate from the source")
    source_reader = PdfReader(source, strict=False)
    prepared_reader = PdfReader(prepared, strict=False)
    if len(source_reader.pages) != len(prepared_reader.pages):
        raise RuntimeError("Prepared PDF page count differs from the source")
    if not all(
        tuple(float(value) for value in source_reader.pages[index].mediabox)
        == tuple(float(value) for value in prepared_reader.pages[index].mediabox)
        for index in range(len(source_reader.pages))
    ):
        raise RuntimeError("Prepared PDF page geometry differs from the source")
    operation_id = f"pdfrepair_{uuid4().hex}"
    if digitally_signed:
        suffix = "-compressed" if repair_kind == "compression" else "-rebuilt-with-text"
        output = source.with_name(f"{source.stem}{suffix}{source.suffix}")
        if output.exists():
            raise FileExistsError(f"Signed-PDF derivative already exists: {output.name}")
        _atomic_copy(prepared, output, operation_id)
        return PdfRepairCommitResult(source, output, None, False, repair_kind)
    backup = root / ".AIOrganizer-Quarantine" / "pdf-repair" / operation_id / relative
    backup.parent.mkdir(parents=True, exist_ok=True)
    staged = source.with_name(f".{source.name}.{operation_id}.tmp")
    shutil.copy2(prepared, staged)
    try:
        os.replace(source, backup)
        try:
            os.replace(staged, source)
        except Exception:
            os.replace(backup, source)
            raise
    finally:
        staged.unlink(missing_ok=True)
    return PdfRepairCommitResult(source, source, backup, True, repair_kind)


def _atomic_copy(source: Path, target: Path, operation_id: str) -> None:
    staged = target.with_name(f".{target.name}.{operation_id}.tmp")
    try:
        shutil.copy2(source, staged)
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def _append_invisible_ocr_lines(
    writer: Any,
    page: Any,
    page_index: int,
    lines: list[dict[str, Any]],
    corrections: dict[tuple[int, str], str],
) -> int:
    from pypdf.generic import (
        ArrayObject,
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
    )

    if not lines:
        return 0
    resources_value = page.get("/Resources")
    resources = resources_value.get_object() if resources_value else DictionaryObject()
    if not isinstance(resources, DictionaryObject):
        resources = DictionaryObject()
    fonts_value = resources.get("/Font")
    fonts = fonts_value.get_object() if fonts_value else DictionaryObject()
    if not isinstance(fonts, DictionaryObject):
        fonts = DictionaryObject()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
        }
    )
    fonts[NameObject("/AIOrganizerOCR")] = writer._add_object(font)
    resources[NameObject("/Font")] = fonts
    page[NameObject("/Resources")] = resources
    width = float(page.mediabox.width)
    height = float(page.mediabox.height)
    left_offset = float(page.mediabox.left)
    bottom_offset = float(page.mediabox.bottom)
    commands = ["q", "BT", "3 Tr"]
    accepted = 0
    for line_number, line in enumerate(lines):
        raw_bounds = line.get("bounds", ())
        if not isinstance(raw_bounds, (list, tuple)) or len(raw_bounds) != 4:
            continue
        x, top, box_width, box_height = (float(value) for value in raw_bounds)
        if box_width <= 0 or box_height <= 0:
            continue
        line_id = str(line.get("line_id", f"line-{line_number:04d}"))
        original_text = str(line.get("text", "")).strip()
        text = corrections.get((page_index, line_id), original_text).strip()
        if not text:
            continue
        positioned_words = _corrected_positioned_words(line, original_text, text)
        if positioned_words:
            for word_text, word_bounds in positioned_words:
                _append_invisible_text_command(
                    commands,
                    word_text,
                    word_bounds,
                    width,
                    height,
                    left_offset,
                    bottom_offset,
                )
        else:
            _append_invisible_text_command(
                commands,
                text,
                (x, top, box_width, box_height),
                width,
                height,
                left_offset,
                bottom_offset,
            )
        accepted += 1
    commands.extend(["ET", "Q"])
    if not accepted:
        return 0
    stream = DecodedStreamObject()
    stream.set_data(("\n".join(commands) + "\n").encode("ascii"))
    stream_ref = writer._add_object(stream)
    contents = page.get("/Contents")
    if contents is None:
        page[NameObject("/Contents")] = stream_ref
    elif isinstance(contents, ArrayObject):
        contents.append(stream_ref)
    else:
        page[NameObject("/Contents")] = ArrayObject([contents, stream_ref])
    return accepted


def _corrected_positioned_words(
    line: dict[str, Any], original_text: str, corrected_text: str
) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Map same-length line corrections back onto Tesseract's original word boxes."""
    raw_words = [value for value in line.get("words", []) if isinstance(value, dict)]
    if not raw_words or len(original_text) != len(corrected_text):
        return []
    original_words = [str(value.get("text", "")) for value in raw_words]
    if " ".join(original_words) != original_text:
        return []
    positioned: list[tuple[str, tuple[float, float, float, float]]] = []
    offset = 0
    for raw_word, original_word in zip(raw_words, original_words, strict=True):
        raw_bounds = raw_word.get("bounds", ())
        if not isinstance(raw_bounds, (list, tuple)) or len(raw_bounds) != 4:
            return []
        bounds = (
            float(raw_bounds[0]),
            float(raw_bounds[1]),
            float(raw_bounds[2]),
            float(raw_bounds[3]),
        )
        word = corrected_text[offset : offset + len(original_word)]
        positioned.append((word, bounds))
        offset += len(original_word) + 1
    return positioned


def _append_invisible_text_command(
    commands: list[str],
    text: str,
    bounds: tuple[float, float, float, float],
    page_width: float,
    page_height: float,
    left_offset: float,
    bottom_offset: float,
) -> None:
    x, top, box_width, box_height = bounds
    font_size = max(3.0, min(72.0, box_height * page_height * 0.82))
    estimated_width = max(1.0, len(text) * font_size * 0.5)
    horizontal_scale = max(10.0, min(500.0, box_width * page_width / estimated_width * 100))
    pdf_x = left_offset + x * page_width
    pdf_y = bottom_offset + page_height - (top + box_height) * page_height
    commands.extend(
        [
            f"/AIOrganizerOCR {font_size:.4f} Tf",
            f"{horizontal_scale:.4f} Tz",
            f"1 0 0 1 {pdf_x:.4f} {pdf_y:.4f} Tm",
            f"({_pdf_literal(text)}) Tj",
        ]
    )


def _pdf_literal(text: str) -> str:
    encoded = text.encode("cp1252", errors="replace").decode("latin-1")
    return (
        encoded.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("\r", " ")
        .replace("\n", " ")
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
