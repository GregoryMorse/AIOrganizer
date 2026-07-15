from __future__ import annotations

import email
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


class PdfExtractor:
    name = "pypdf"

    def __init__(self, ocr: QtPdfOcr | None = None) -> None:
        self.ocr = ocr

    def supports(self, path: Path, item: ItemSnapshot) -> bool:
        return path.suffix.casefold() == ".pdf" or item.mime_type == "application/pdf"

    def extract(self, path: Path, item: ItemSnapshot) -> Evidence:
        from pypdf import PdfReader

        reader = PdfReader(path)
        if reader.is_encrypted:
            return Evidence(item.id, "pdf", "Encrypted PDF", facts={"encrypted": True})
        pages = [(page.extract_text() or "")[:100_000] for page in reader.pages[:50]]
        text = "\n".join(pages)[:MAX_TEXT_BYTES]
        page_coverage = [min(1.0, len(value.strip()) / 800) for value in pages]
        coverage = sum(page_coverage) / max(1, len(page_coverage))
        ocr_candidates = [
            index for index, value in enumerate(page_coverage) if value < 0.15
        ][:20]
        ocr_used = False
        ocr_available = bool(self.ocr and self.ocr.available())
        if ocr_candidates and self.ocr and ocr_available:
            recognized = self.ocr.recognize_pages(path, ocr_candidates)
            for page_index, ocr_text in recognized.items():
                if ocr_text.strip() and page_index < len(pages):
                    pages[page_index] = ocr_text[:100_000]
                    page_coverage[page_index] = min(0.85, len(ocr_text.strip()) / 800)
            if recognized:
                ocr_used = True
            text = "\n".join(pages)[:MAX_TEXT_BYTES]
            coverage = sum(page_coverage) / max(1, len(page_coverage))
        remaining_ocr = [
            index for index, value in enumerate(page_coverage) if value < 0.15
        ]
        route = _confidence_route(coverage, bool(remaining_ocr), ocr_available)
        languages = LanguageDetector().detect(text)
        return Evidence(
            item.id,
            "pdf",
            text[:2_000],
            facts={
                "page_count": len(reader.pages),
                "pages": pages,
                "page_text_coverage": page_coverage,
                "text": text,
                "text_coverage": coverage,
                "needs_ocr": bool(remaining_ocr),
                "ocr_candidate_pages": ocr_candidates,
                "ocr_remaining_pages": remaining_ocr,
                "ocr_available": ocr_available,
                "ocr_used": ocr_used,
                "metadata": {str(k): str(v) for k, v in (reader.metadata or {}).items()},
            },
            confidence=coverage,
            language_candidates=languages,
            provenance=self.name,
            confidence_route=route,
            content_classes=["extracted_text"],
            extractor_version="2",
        )


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
            text = self.ocr.recognize(path, ["eng"])[:MAX_TEXT_BYTES] if ocr_available and self.ocr else ""
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
                    "needs_review" if text.strip() else "ocr_unavailable" if not ocr_available else "needs_review"
                ),
                content_classes=["visual_content", "extracted_text"] if text.strip() else ["visual_content"],
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
        self.executable = (
            executable
            or os.getenv("AIORGANIZER_TESSERACT")
            or (str(bundled) if bundled.exists() else shutil.which("tesseract") or "tesseract")
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
                timeout=10,
            )
            first_line = result.stdout.splitlines()[0] if result.stdout else ""
            match = re.search(r"tesseract\s+(\d+)\.(\d+)", first_line, re.IGNORECASE)
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
            timeout=180,
            env=self.environment,
        )
        return result.stdout


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
        from PySide6.QtCore import QSize
        from PySide6.QtPdf import QPdfDocument

        document = QPdfDocument()
        if document.load(str(pdf_path)) != QPdfDocument.Error.None_:
            return {}
        fragments: dict[int, str] = {}
        with tempfile.TemporaryDirectory(prefix="aiorganizer-ocr-") as temporary:
            allowed = sorted(
                {
                    page
                    for page in page_indices
                    if 0 <= page < document.pageCount()
                }
            )[: self.max_pages]
            for page in allowed:
                image = document.render(page, QSize(1800, 2400))
                image_path = Path(temporary) / f"page-{page:04d}.png"
                if image.isNull() or not image.save(str(image_path), "PNG"):
                    continue
                fragments[page] = self.tesseract.recognize(image_path, ["eng"])
        document.close()
        return fragments

    def recognize(self, pdf_path: Path) -> str:
        return "\n".join(self.recognize_pages(pdf_path, list(range(self.max_pages))).values())


def default_registry() -> ExtractionRegistry:
    tesseract = TesseractOcr()
    return ExtractionRegistry(
        [
            PdfExtractor(QtPdfOcr(tesseract)),
            TextExtractor(),
            ImageExtractor(tesseract),
            OfficeContainerExtractor(),
            EmailExtractor(),
            ArchiveExtractor(),
            AudioExtractor(),
            GenericExtractor(),
        ]
    )


def _confidence_route(coverage: float, needs_ocr: bool, ocr_available: bool) -> str:
    if needs_ocr and not ocr_available:
        return "ocr_unavailable"
    if needs_ocr:
        return "ocr_required"
    if coverage >= 0.6:
        return "high_confidence"
    return "needs_review"
