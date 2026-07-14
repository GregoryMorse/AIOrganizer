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
        pages: list[str] = []
        for page in reader.pages[:50]:
            pages.append((page.extract_text() or "")[:100_000])
        text = "\n".join(pages)[:MAX_TEXT_BYTES]
        coverage = min(1.0, len(text.strip()) / max(1, len(reader.pages) * 800))
        ocr_used = False
        if coverage < 0.15 and self.ocr and self.ocr.available():
            ocr_text = self.ocr.recognize(path)
            if ocr_text.strip():
                text = ocr_text[:MAX_TEXT_BYTES]
                coverage = min(0.85, len(text.strip()) / max(1, len(reader.pages) * 800))
                ocr_used = True
        languages = LanguageDetector().detect(text)
        return Evidence(
            item.id,
            "pdf",
            text[:2_000],
            facts={
                "page_count": len(reader.pages),
                "text": text,
                "text_coverage": coverage,
                "needs_ocr": coverage < 0.15,
                "ocr_used": ocr_used,
                "metadata": {str(k): str(v) for k, v in (reader.metadata or {}).items()},
            },
            confidence=coverage,
            language_candidates=languages,
            provenance=self.name,
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
        raw = path.read_bytes()[:MAX_TEXT_BYTES]
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
        )


class ImageExtractor:
    name = "pillow"

    def supports(self, path: Path, item: ItemSnapshot) -> bool:
        return item.mime_type.startswith("image/")

    def extract(self, path: Path, item: ItemSnapshot) -> Evidence:
        from PIL import Image

        with Image.open(path) as image:
            exif = {str(key): str(value) for key, value in image.getexif().items()}
            return Evidence(
                item.id,
                "image",
                f"{image.format} image, {image.width} x {image.height}",
                facts={
                    "format": image.format,
                    "width": image.width,
                    "height": image.height,
                    "exif": exif,
                    "needs_ocr": True,
                },
                confidence=0.9,
                provenance=self.name,
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
                data = archive.read(name)[:500_000]
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
        )


class EmailExtractor:
    name = "stdlib-email"

    def supports(self, path: Path, item: ItemSnapshot) -> bool:
        return path.suffix.casefold() == ".eml"

    def extract(self, path: Path, item: ItemSnapshot) -> Evidence:
        message = email.message_from_bytes(path.read_bytes()[:MAX_TEXT_BYTES])
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
        if bundled_data.is_dir():
            self.environment["TESSDATA_PREFIX"] = str(bundled_data)

    def available(self) -> bool:
        try:
            result = subprocess.run(
                [self.executable, "--version"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return self.REQUIRED_VERSION in result.stdout.splitlines()[0]
        except (OSError, subprocess.SubprocessError):
            return False

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

    def recognize(self, pdf_path: Path) -> str:
        from PySide6.QtCore import QSize
        from PySide6.QtPdf import QPdfDocument

        document = QPdfDocument()
        if document.load(str(pdf_path)) != QPdfDocument.Error.None_:
            return ""
        fragments: list[str] = []
        with tempfile.TemporaryDirectory(prefix="aiorganizer-ocr-") as temporary:
            for page in range(min(document.pageCount(), self.max_pages)):
                image = document.render(page, QSize(1800, 2400))
                image_path = Path(temporary) / f"page-{page:04d}.png"
                if image.isNull() or not image.save(str(image_path), "PNG"):
                    continue
                fragments.append(self.tesseract.recognize(image_path, ["eng"]))
        document.close()
        return "\n".join(fragments)


def default_registry() -> ExtractionRegistry:
    return ExtractionRegistry(
        [
            PdfExtractor(QtPdfOcr()),
            TextExtractor(),
            ImageExtractor(),
            OfficeContainerExtractor(),
            EmailExtractor(),
            ArchiveExtractor(),
            AudioExtractor(),
            GenericExtractor(),
        ]
    )
