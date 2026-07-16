from __future__ import annotations

import csv
import email
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import ClassVar, Protocol
from xml.etree import ElementTree

from ai_organizer.adapters.filesystem.metadata import (
    capture_pdf_diagnostics,
    pdf_health_issues,
)
from ai_organizer.domain.models import Evidence, ItemSnapshot

MAX_TEXT_BYTES = 2_000_000
MAX_ARCHIVE_ENTRIES = 2_000


class Extractor(Protocol):
    name: str

    def supports(self, path: Path, item: ItemSnapshot) -> bool: ...

    def extract(self, path: Path, item: ItemSnapshot) -> Evidence: ...


class ExtractionRegistry:
    def __init__(self, extractors: list[Extractor]) -> None:
        self.extractors = extractors

    def extract(self, path: Path, item: ItemSnapshot) -> Evidence:
        if item.is_placeholder:
            return Evidence(item.id, "placeholder", "Cloud-only placeholder; hydration required")
        for extractor in self.extractors:
            if extractor.supports(path, item):
                try:
                    return extractor.extract(path, item)
                except Exception as error:
                    return Evidence(
                        item.id,
                        "extraction_error",
                        f"{extractor.name} could not safely extract this item",
                        facts={"error_type": type(error).__name__},
                        provenance=extractor.name,
                    )
        return GenericExtractor().extract(path, item)


class _PdfTextPage(Protocol):
    def extract_text(self, *args: object, **kwargs: object) -> str | None: ...


class PdfExtractor:
    name = "pypdf"

    def __init__(self, ocr: QtPdfOcr | None = None) -> None:
        self.ocr = ocr

    def supports(self, path: Path, item: ItemSnapshot) -> bool:
        return path.suffix.casefold() == ".pdf" or item.mime_type == "application/pdf"

    def extract(self, path: Path, item: ItemSnapshot) -> Evidence:
        from pypdf import PdfReader

        with capture_pdf_diagnostics() as messages:
            reader = PdfReader(path, strict=False)
            encrypted = bool(reader.is_encrypted)
            page_count = None if encrypted else len(reader.pages)
            page_objects = [] if encrypted else list(reader.pages[:50])
            pages = [] if encrypted else [_extract_pdf_page_text(page) for page in page_objects]
            document_metadata = {
                str(key): str(value) for key, value in (reader.metadata or {}).items()
            }
        issues = pdf_health_issues(messages)
        health = {
            "file_health_status": "warning" if issues else "no_issues_observed",
            "file_health_issue_count": len(issues),
            "file_health_issues": issues,
        }
        if encrypted:
            return Evidence(
                item.id,
                "pdf",
                "Encrypted PDF",
                facts={"encrypted": True, **health},
            )
        embedded_pages = list(pages)
        text = "\n".join(pages)[:MAX_TEXT_BYTES]
        page_coverage = [min(1.0, len(value.strip()) / 800) for value in pages]
        coverage = sum(page_coverage) / max(1, len(page_coverage))
        ocr_reasons: dict[str, list[str]] = {}
        ocr_candidates: list[int] = []
        for index, (page, page_text) in enumerate(zip(page_objects, pages, strict=True)):
            image_count = _pdf_page_image_count(page)
            quality_reasons = _text_quality_reasons(page_text)
            reasons = []
            if image_count and len(page_text.strip()) < 120:
                reasons.append(
                    "image-backed page has insufficient embedded text"
                    if page_text.strip()
                    else "image-backed page has no embedded text"
                )
            if quality_reasons:
                reasons.extend(quality_reasons)
            if reasons:
                ocr_candidates.append(index)
                ocr_reasons[str(index)] = list(dict.fromkeys(reasons))
            if len(ocr_candidates) >= 20:
                break
        ocr_used = False
        ocr_available = bool(self.ocr and self.ocr.available())
        ocr_layout_pages: dict[str, object] = {}
        if ocr_candidates and self.ocr and ocr_available:
            recognized_layout = self.ocr.recognize_pages_with_layout(path, ocr_candidates)
            for page_index, layout in recognized_layout.items():
                ocr_text = str(layout.get("text", ""))
                if ocr_text.strip() and page_index < len(pages):
                    page = reader.pages[page_index]
                    layout["pdf_rotation"] = int(page.rotation or 0)
                    layout["pdf_mediabox"] = [float(value) for value in page.mediabox]
                    pages[page_index] = ocr_text[:100_000]
                    page_coverage[page_index] = min(0.85, len(ocr_text.strip()) / 800)
                    ocr_layout_pages[str(page_index)] = layout
            if ocr_layout_pages:
                ocr_used = True
            text = "\n".join(pages)[:MAX_TEXT_BYTES]
            coverage = sum(page_coverage) / max(1, len(page_coverage))
        completed_ocr = sorted(int(value) for value in ocr_layout_pages)
        remaining_ocr = [index for index in ocr_candidates if index not in completed_ocr]
        ai_cleanup_pages, ai_cleanup_reasons = _ocr_cleanup_assessment(ocr_layout_pages)
        route = _confidence_route(
            coverage,
            bool(remaining_ocr),
            ocr_available,
            ocr_configured=self.ocr is not None,
        )
        languages = LanguageDetector().detect(text)
        return Evidence(
            item.id,
            "pdf",
            text[:2_000],
            facts={
                "page_count": page_count,
                "pages": pages,
                "embedded_pages": embedded_pages,
                "page_text_coverage": page_coverage,
                "text": text,
                "text_coverage": coverage,
                "needs_ocr": bool(remaining_ocr),
                "ocr_candidate_pages": ocr_candidates,
                "ocr_candidate_reasons": ocr_reasons,
                "ocr_completed_pages": completed_ocr,
                "ocr_remaining_pages": remaining_ocr,
                "ocr_available": ocr_available,
                "ocr_availability": (
                    "available"
                    if ocr_available
                    else "unavailable"
                    if self.ocr is not None
                    else "not_checked"
                ),
                "ocr_attempted": bool(ocr_candidates and self.ocr and ocr_available),
                "ocr_used": ocr_used,
                "ocr_layout_pages": ocr_layout_pages,
                "ai_cleanup_recommended": bool(ai_cleanup_pages),
                "ai_cleanup_pages": ai_cleanup_pages,
                "ai_cleanup_reasons": ai_cleanup_reasons,
                "pdf_text_extraction_mode": "layout",
                "metadata": document_metadata,
                **health,
            },
            confidence=coverage,
            language_candidates=languages,
            provenance=self.name,
            confidence_route=route,
            content_classes=["extracted_text"],
            extractor_version="3",
        )


def _extract_pdf_page_text(page: _PdfTextPage) -> str:
    """Prefer pypdf's position-aware layout reconstruction with a compatibility fallback."""
    try:
        value = page.extract_text(
            extraction_mode="layout",
            layout_mode_space_vertically=False,
            layout_mode_strip_rotated=False,
        )
    except TypeError:
        value = page.extract_text()
    return str(value or "")[:100_000]


def _pdf_page_image_count(page: object) -> int:
    try:
        return sum(1 for _value in getattr(page, "images", ()))
    except (AttributeError, KeyError, TypeError, ValueError):
        return 0


def _text_quality_reasons(text: str) -> list[str]:
    """Conservative local signals that justify re-OCR, not a claim that AI is required."""
    value = text.strip()
    if not value:
        return []
    reasons = []
    printable = sum(character.isprintable() or character in "\r\n\t" for character in value)
    if printable / max(1, len(value)) < 0.97 or "\ufffd" in value:
        reasons.append("embedded text contains invalid or non-printable characters")
    alphanumeric = sum(character.isalnum() for character in value)
    separators = sum(character.isspace() for character in value)
    if len(value) >= 80 and alphanumeric >= 60 and separators / max(1, alphanumeric) < 0.015:
        reasons.append("embedded text has implausibly little word spacing")
    return reasons


def _ocr_cleanup_assessment(
    layout_pages: dict[str, object],
) -> tuple[list[int], dict[str, list[str]]]:
    pages: list[int] = []
    page_reasons: dict[str, list[str]] = {}
    for page_key, raw_page in layout_pages.items():
        if not isinstance(raw_page, dict):
            continue
        reasons: list[str] = []
        lines = [value for value in raw_page.get("lines", []) if isinstance(value, dict)]
        confidences = [float(value.get("confidence", 0.0)) for value in lines]
        if confidences and sum(confidences) / len(confidences) < 0.88:
            reasons.append("average local OCR confidence is below 88%")
        if any(value < 0.65 for value in confidences):
            reasons.append("one or more OCR lines have very low confidence")
        for line in lines:
            if _text_quality_reasons(str(line.get("text", ""))):
                reasons.append("OCR line spacing or character quality looks suspicious")
                break
        if reasons:
            page_index = int(raw_page.get("page_index", page_key))
            pages.append(page_index)
            page_reasons[str(page_index)] = reasons
    return sorted(pages), page_reasons


class TextExtractor:
    name = "bounded-text"
    TEXT_SUFFIXES: ClassVar[frozenset[str]] = frozenset(
        {
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
            ".html",
            ".htm",
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
        }
    )

    def supports(self, path: Path, item: ItemSnapshot) -> bool:
        return path.suffix.casefold() in self.TEXT_SUFFIXES or item.mime_type.startswith("text/")

    def extract(self, path: Path, item: ItemSnapshot) -> Evidence:
        with path.open("rb") as stream:
            raw = stream.read(MAX_TEXT_BYTES)
        for encoding in ("utf-8-sig", "utf-16", "latin-1"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        return Evidence(
            item.id,
            "text",
            text[:2_000],
            facts={"text": text, "truncated": path.stat().st_size > len(raw)},
            confidence=0.95,
            provenance=self.name,
            confidence_route="high_confidence",
            content_classes=["extracted_text"],
        )


class ImageExtractor:
    name = "pillow"

    def __init__(self, ocr: TesseractOcr | None = None) -> None:
        self.ocr = ocr

    def supports(self, path: Path, item: ItemSnapshot) -> bool:
        return item.mime_type.startswith("image/")

    def extract(self, path: Path, item: ItemSnapshot) -> Evidence:
        from PIL import Image

        with Image.open(path) as image:
            exif = {str(key): str(value) for key, value in image.getexif().items()}
            ocr_available = bool(self.ocr and self.ocr.available())
            text = (
                self.ocr.recognize(path, ["eng"])[:MAX_TEXT_BYTES]
                if ocr_available and self.ocr
                else ""
            )
            return Evidence(
                item.id,
                "image",
                f"{image.format} image, {image.width} x {image.height}",
                facts={
                    "format": image.format,
                    "width": image.width,
                    "height": image.height,
                    "exif": exif,
                    "text": text,
                    "needs_ocr": not bool(text.strip()),
                    "ocr_available": ocr_available,
                    "ocr_used": bool(text.strip()),
                },
                confidence=0.85 if text.strip() else 0.5,
                provenance=self.name,
                confidence_route=(
                    "needs_review"
                    if text.strip()
                    else "ocr_unavailable"
                    if not ocr_available
                    else "needs_review"
                ),
                content_classes=["visual_content", "extracted_text"]
                if text.strip()
                else ["visual_content"],
                extractor_version="2",
            )


class OfficeContainerExtractor:
    name = "safe-office-container"
    SUFFIXES: ClassVar[frozenset[str]] = frozenset(
        {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}
    )

    def supports(self, path: Path, item: ItemSnapshot) -> bool:
        return path.suffix.casefold() in self.SUFFIXES

    def extract(self, path: Path, item: ItemSnapshot) -> Evidence:
        fragments: list[str] = []
        with zipfile.ZipFile(path) as archive:
            safe_names = [
                name
                for name in archive.namelist()
                if name.endswith(".xml") and not name.startswith(("_rels/", "customXml/"))
            ]
            for name in safe_names[:200]:
                with archive.open(name) as stream:
                    data = stream.read(500_000)
                try:
                    root = ElementTree.fromstring(data)
                except ElementTree.ParseError:
                    continue
                fragments.extend(text for text in root.itertext() if text.strip())
                if sum(map(len, fragments)) >= MAX_TEXT_BYTES:
                    break
        text = " ".join(fragments)[:MAX_TEXT_BYTES]
        return Evidence(
            item.id,
            "office_document",
            text[:2_000],
            facts={"text": text},
            confidence=0.85,
            provenance=self.name,
            confidence_route="high_confidence" if text.strip() else "needs_review",
            content_classes=["extracted_text"],
        )


class EmailExtractor:
    name = "stdlib-email"

    def supports(self, path: Path, item: ItemSnapshot) -> bool:
        return path.suffix.casefold() == ".eml"

    def extract(self, path: Path, item: ItemSnapshot) -> Evidence:
        with path.open("rb") as stream:
            message = email.message_from_bytes(stream.read(MAX_TEXT_BYTES))
        body = ""
        if message.is_multipart():
            for part in message.walk():
                if part.get_content_type() == "text/plain" and not part.get_filename():
                    body += part.get_payload(decode=True).decode(errors="replace")[:200_000]
        else:
            payload = message.get_payload(decode=True) or b""
            body = payload.decode(errors="replace")[:200_000]
        facts = {
            "subject": message.get("Subject", ""),
            "from": message.get("From", ""),
            "to": message.get("To", ""),
            "date": message.get("Date", ""),
            "text": body,
        }
        return Evidence(
            item.id,
            "email",
            f"{facts['subject']} — {facts['from']}",
            facts=facts,
            confidence=0.9,
            provenance=self.name,
            confidence_route="high_confidence",
            content_classes=["extracted_text"],
        )


class ArchiveExtractor:
    name = "bounded-archive-list"

    def supports(self, path: Path, item: ItemSnapshot) -> bool:
        return path.suffix.casefold() in {".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz"}

    def extract(self, path: Path, item: ItemSnapshot) -> Evidence:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()[:MAX_ARCHIVE_ENTRIES]
        elif tarfile.is_tarfile(path):
            with tarfile.open(path) as archive:
                names = [member.name for member in archive.getmembers()[:MAX_ARCHIVE_ENTRIES]]
        else:
            names = []
        return Evidence(
            item.id,
            "archive",
            f"Archive containing {len(names)} listed entries",
            facts={"entries": names, "truncated": len(names) == MAX_ARCHIVE_ENTRIES},
            confidence=0.8,
            provenance=self.name,
        )


class AudioExtractor:
    name = "mutagen"

    def supports(self, path: Path, item: ItemSnapshot) -> bool:
        return item.mime_type.startswith("audio/")

    def extract(self, path: Path, item: ItemSnapshot) -> Evidence:
        from mutagen import File

        audio = File(path, easy=True)
        tags = {key: [str(value) for value in values] for key, values in (audio or {}).items()}
        return Evidence(
            item.id,
            "audio",
            "; ".join(f"{key}: {', '.join(values)}" for key, values in tags.items())[:2_000],
            facts={"tags": tags},
            confidence=0.8,
            provenance=self.name,
        )


class GenericExtractor:
    name = "generic-metadata"

    def supports(self, path: Path, item: ItemSnapshot) -> bool:
        return True

    def extract(self, path: Path, item: ItemSnapshot) -> Evidence:
        return Evidence(
            item.id,
            "metadata",
            f"{item.mime_type}; {item.size} bytes",
            facts={
                "name": path.name,
                "extension": path.suffix,
                "mime_type": item.mime_type,
                "size": item.size,
            },
            confidence=0.4,
            provenance=self.name,
        )


class LanguageDetector:
    def detect(self, text: str, limit: int = 3) -> list[tuple[str, float]]:
        if not text.strip():
            return [("und", 0.0)]
        try:
            from lingua import LanguageDetectorBuilder

            detector = LanguageDetectorBuilder.from_all_languages().build()
            values = detector.compute_language_confidence_values(text[:100_000])[:limit]
            return [(value.language.iso_code_639_1.name.lower(), value.value) for value in values]
        except (ImportError, ValueError):
            ascii_ratio = sum(character.isascii() for character in text) / max(1, len(text))
            return [("en", 0.55)] if ascii_ratio > 0.9 else [("und", 0.1)]


class TesseractOcr:
    REQUIRED_VERSION = "5.5.2"

    def __init__(self, executable: str | None = None) -> None:
        bundled_name = "tesseract.exe" if sys.platform == "win32" else "tesseract"
        bundled = Path(sys.executable).parent / "resources" / "tesseract" / bundled_name
        system_install = None
        if sys.platform == "win32":
            candidates = (
                Path(os.getenv("PROGRAMFILES", r"C:\Program Files"))
                / "Tesseract-OCR"
                / bundled_name,
                Path(os.getenv("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR" / bundled_name,
            )
            system_install = next((str(path) for path in candidates if path.is_file()), None)
        self.executable = (
            executable
            or os.getenv("AIORGANIZER_TESSERACT")
            or (
                str(bundled)
                if bundled.exists()
                else shutil.which("tesseract") or system_install or "tesseract"
            )
        )
        bundled_data = bundled.parent / "tessdata"
        self.environment = os.environ.copy()
        self._available: bool | None = None
        if bundled_data.is_dir():
            self.environment["TESSDATA_PREFIX"] = str(bundled_data)

    def available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            result = subprocess.run(
                [self.executable, "--version"],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            first_line = result.stdout.splitlines()[0] if result.stdout else ""
            match = re.search(r"tesseract\s+v?(\d+)\.(\d+)", first_line, re.IGNORECASE)
            self._available = bool(match and int(match.group(1)) >= 5)
        except (OSError, subprocess.SubprocessError):
            self._available = False
        return self._available

    def detect_script(self, image: Path) -> str:
        result = subprocess.run(
            [self.executable, str(image), "stdout", "--psm", "0"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=self.environment,
        )
        match = re.search(r"Script:\s*(\S+)", result.stdout)
        return match.group(1) if match else "Unknown"

    def recognize(self, image: Path, languages: list[str]) -> str:
        result = subprocess.run(
            [self.executable, str(image), "stdout", "-l", "+".join(languages or ["eng"])],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            env=self.environment,
        )
        return result.stdout

    def recognize_layout(
        self, image: Path, languages: list[str], *, page_index: int = 0
    ) -> dict[str, object]:
        result = subprocess.run(
            [
                self.executable,
                str(image),
                "stdout",
                "-l",
                "+".join(languages or ["eng"]),
                "tsv",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            env=self.environment,
        )
        from PIL import Image

        with Image.open(image) as rendered:
            width, height = rendered.size
        return _parse_tesseract_tsv(result.stdout, width, height, page_index)


class QtPdfOcr:
    """Render bounded PDF pages with Qt and OCR them in Tesseract subprocesses."""

    def __init__(self, tesseract: TesseractOcr | None = None, max_pages: int = 20) -> None:
        self.tesseract = tesseract or TesseractOcr()
        self.max_pages = max_pages

    def available(self) -> bool:
        try:
            import PySide6.QtPdf  # noqa: F401

            return self.tesseract.available()
        except ImportError:
            return False

    def recognize_pages(self, pdf_path: Path, page_indices: list[int]) -> dict[int, str]:
        return {
            page_index: str(layout.get("text", ""))
            for page_index, layout in self.recognize_pages_with_layout(
                pdf_path, page_indices
            ).items()
        }

    def recognize_pages_with_layout(
        self, pdf_path: Path, page_indices: list[int]
    ) -> dict[int, dict[str, object]]:
        from PySide6.QtCore import QSize
        from PySide6.QtPdf import QPdfDocument

        document = QPdfDocument()
        if document.load(str(pdf_path)) != QPdfDocument.Error.None_:
            return {}
        fragments: dict[int, dict[str, object]] = {}
        with tempfile.TemporaryDirectory(prefix="aiorganizer-ocr-") as temporary:
            allowed = sorted({page for page in page_indices if 0 <= page < document.pageCount()})[
                : self.max_pages
            ]
            for page in allowed:
                points = document.pagePointSize(page)
                target_width = max(1, round(points.width() / 72 * 220))
                target_height = max(1, round(points.height() / 72 * 220))
                scale = min(1.0, 3200 / max(target_width, target_height))
                render_size = QSize(
                    max(1, round(target_width * scale)),
                    max(1, round(target_height * scale)),
                )
                image = document.render(page, render_size)
                image_path = Path(temporary) / f"page-{page:04d}.png"
                if image.isNull() or not image.save(str(image_path), "PNG"):
                    continue
                fragments[page] = self.tesseract.recognize_layout(
                    image_path, ["eng"], page_index=page
                )
        document.close()
        return fragments

    def recognize(self, pdf_path: Path) -> str:
        return "\n".join(self.recognize_pages(pdf_path, list(range(self.max_pages))).values())


def _parse_tesseract_tsv(
    text: str, image_width: int, image_height: int, page_index: int
) -> dict[str, object]:
    """Convert Tesseract word boxes into stable, normalized positioned lines."""
    groups: dict[tuple[int, int, int], list[dict[str, object]]] = {}
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    for row in reader:
        word = str(row.get("text", "")).strip()
        if not word or str(row.get("level", "")) != "5":
            continue
        try:
            key = (int(row["block_num"]), int(row["par_num"]), int(row["line_num"]))
            groups.setdefault(key, []).append(
                {
                    "text": word,
                    "left": int(row["left"]),
                    "top": int(row["top"]),
                    "width": int(row["width"]),
                    "height": int(row["height"]),
                    "confidence": max(0.0, float(row.get("conf", 0.0))) / 100,
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    lines: list[dict[str, object]] = []
    for (block, paragraph, line), words in sorted(groups.items()):
        left = min(int(word["left"]) for word in words)
        top = min(int(word["top"]) for word in words)
        right = max(int(word["left"]) + int(word["width"]) for word in words)
        bottom = max(int(word["top"]) + int(word["height"]) for word in words)
        lines.append(
            {
                "line_id": f"b{block:03d}-p{paragraph:03d}-l{line:03d}",
                "text": " ".join(str(word["text"]) for word in words),
                "bounds": [
                    left / max(1, image_width),
                    top / max(1, image_height),
                    (right - left) / max(1, image_width),
                    (bottom - top) / max(1, image_height),
                ],
                "confidence": sum(float(word["confidence"]) for word in words) / len(words),
                "word_count": len(words),
                "words": [
                    {
                        "text": str(word["text"]),
                        "bounds": [
                            int(word["left"]) / max(1, image_width),
                            int(word["top"]) / max(1, image_height),
                            int(word["width"]) / max(1, image_width),
                            int(word["height"]) / max(1, image_height),
                        ],
                        "confidence": float(word["confidence"]),
                    }
                    for word in words
                ],
            }
        )
    return {
        "page_index": page_index,
        "image_width": image_width,
        "image_height": image_height,
        "coordinate_space": "normalized_top_left",
        "text": "\n".join(str(line["text"]) for line in lines),
        "lines": lines,
    }


def default_registry(*, enable_ocr: bool = False) -> ExtractionRegistry:
    tesseract = TesseractOcr()
    pdf_ocr = QtPdfOcr(tesseract) if enable_ocr else None
    image_ocr = tesseract if enable_ocr else None
    return ExtractionRegistry(
        [
            PdfExtractor(pdf_ocr),
            TextExtractor(),
            ImageExtractor(image_ocr),
            OfficeContainerExtractor(),
            EmailExtractor(),
            ArchiveExtractor(),
            AudioExtractor(),
            GenericExtractor(),
        ]
    )


def _confidence_route(
    coverage: float,
    needs_ocr: bool,
    ocr_available: bool,
    *,
    ocr_configured: bool = True,
) -> str:
    if needs_ocr and not ocr_configured:
        return "local_ocr_not_run"
    if needs_ocr and not ocr_available:
        return "ocr_unavailable"
    if needs_ocr:
        return "ocr_required"
    if coverage >= 0.6:
        return "high_confidence"
    return "needs_review"
