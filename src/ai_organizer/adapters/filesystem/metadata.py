from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import struct
import subprocess
import tarfile
import threading
import warnings
import zipfile
import zlib
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from ai_organizer.domain.models import ItemSnapshot

TEXT_SUFFIXES = {
    ".c",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".h",
    ".htm",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".log",
    ".md",
    ".py",
    ".rs",
    ".rst",
    ".tex",
    ".toml",
    ".ts",
    ".tsv",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
OFFICE_SUFFIXES = {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}
LEGACY_OFFICE_SUFFIXES = {".doc", ".xls", ".ppt"}
PE_SUFFIXES = {".exe", ".dll", ".sys", ".ocx", ".cpl", ".scr", ".drv", ".efi"}
MSI_SUFFIXES = {".msi", ".msp", ".mst"}
MSIX_SUFFIXES = {".msix", ".appx", ".msixbundle", ".appxbundle"}
UNIX_BINARY_SUFFIXES = {".elf", ".so", ".dylib", ".bundle", ".bin", ".run"}
EXECUTABLE_MIME_TYPES = {
    "application/vnd.microsoft.portable-executable",
    "application/x-dosexec",
    "application/x-executable",
    "application/x-mach-binary",
    "application/x-msdownload",
    "application/x-pie-executable",
    "application/x-sharedlib",
}
_PDF_DIAGNOSTIC_LOCK = threading.Lock()


class MetadataIndexer:
    """Extract bounded, non-content metadata suitable for inventory search and summaries."""

    def extract(self, path: Path, item: ItemSnapshot) -> dict[str, Any]:
        facts: dict[str, Any] = {
            "name": item.name or path.name,
            "extension": item.extension or path.suffix.casefold(),
            "mime_type": item.mime_type,
            "size": item.size,
            "modified_ns": item.modified_ns,
            "created_ns": item.created_ns,
            "is_directory": item.is_dir,
            "file_health_status": "no_issues_observed",
            "file_health_issue_count": 0,
        }
        with suppress(OSError):
            facts.update(_filesystem_metadata(path))
        if item.is_dir:
            facts.update(
                {
                    "child_file_count": item.child_file_count,
                    "child_folder_count": item.child_folder_count,
                    "project_markers": list(item.project_markers),
                    "is_project_root": item.is_project_root,
                    "inside_protected_project": item.inside_protected_project,
                    "protected_project_path": item.protected_project_path,
                    "has_build_outputs": item.has_build_outputs,
                    "has_virtual_environment": item.has_virtual_environment,
                    "has_nested_repositories": item.has_nested_repositories,
                }
            )
            return facts
        suffix = path.suffix.casefold()
        try:
            if suffix in TEXT_SUFFIXES or item.mime_type.startswith("text/"):
                facts.update(_text_metadata(path))
            elif suffix == ".pdf":
                facts.update(_pdf_metadata(path))
            elif suffix in OFFICE_SUFFIXES or suffix in LEGACY_OFFICE_SUFFIXES:
                facts.update(_office_metadata(path, suffix))
            elif suffix in MSI_SUFFIXES:
                facts.update(_msi_metadata(path))
            elif suffix in MSIX_SUFFIXES:
                facts.update(_msix_metadata(path))
            elif item.mime_type.startswith("image/"):
                facts.update(_image_metadata(path))
            elif suffix in {
                ".zip",
                ".jar",
                ".whl",
                ".rar",
                ".tar",
                ".tgz",
                ".gz",
                ".bz2",
                ".xz",
            }:
                facts.update(_archive_metadata(path))
            elif item.mime_type.startswith(("audio/", "video/")) or suffix in {
                ".m4a",
                ".m4v",
                ".mp3",
                ".mp4",
                ".mov",
            }:
                facts.update(_media_metadata(path))
            elif (
                suffix in PE_SUFFIXES
                or suffix in UNIX_BINARY_SUFFIXES
                or item.mime_type in EXECUTABLE_MIME_TYPES
                or (platform.system() != "Windows" and os.access(path, os.X_OK))
            ):
                facts.update(_executable_metadata(path))
        except Exception as error:
            facts["metadata_error"] = type(error).__name__
            facts["metadata_error_detail"] = str(error)[:500]
            facts["file_health_status"] = "error"
            facts["file_health_issue_count"] = 1
            facts["file_health_issues"] = [
                {
                    "code": "metadata_extraction_error",
                    "severity": "error",
                    "source": "metadata_indexer",
                    "message": f"{type(error).__name__}: {str(error)[:400]}",
                    "interpretation": "The parser could not inspect this file; this is not proof of data loss.",
                }
            ]
        return facts


def metadata_fingerprint(item: ItemSnapshot) -> str:
    return f"{item.size}:{item.modified_ns}"


def metadata_cache_compatible(item: ItemSnapshot, payload: dict[str, Any]) -> bool:
    """Refresh only formats whose extraction contract gained required durable fields."""
    return not (
        not item.is_dir
        and (item.extension or Path(item.relative_path).suffix).casefold() == ".pdf"
        and "file_health_status" not in payload
    )


def content_fingerprint(path: Path, algorithm: str) -> dict[str, Any]:
    normalized = algorithm.casefold()
    if normalized not in {"crc32", "sha256"}:
        raise ValueError("Content fingerprint must be crc32 or sha256")
    consumed = 0
    crc = 0
    digest = hashlib.sha256() if normalized == "sha256" else None
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            consumed += len(chunk)
            if digest is not None:
                digest.update(chunk)
            else:
                crc = zlib.crc32(chunk, crc)
    return {
        "algorithm": normalized,
        "value": digest.hexdigest() if digest is not None else f"{crc & 0xFFFFFFFF:08x}",
        "bytes_read": consumed,
    }


def _filesystem_metadata(path: Path) -> dict[str, Any]:
    info = path.stat(follow_symlinks=False)
    result: dict[str, Any] = {
        "accessed_ns": info.st_atime_ns,
        "permissions": f"0o{info.st_mode & 0o7777:o}",
        "hard_link_count": info.st_nlink,
        "device_id": info.st_dev,
        "inode": info.st_ino,
    }
    attributes = int(getattr(info, "st_file_attributes", 0))
    if attributes:
        flags = {
            0x0001: "read_only",
            0x0002: "hidden",
            0x0004: "system",
            0x0020: "archive",
            0x0040: "device",
            0x0080: "normal",
            0x0100: "temporary",
            0x0200: "sparse",
            0x0400: "reparse_point",
            0x0800: "compressed",
            0x1000: "offline",
            0x2000: "not_content_indexed",
            0x4000: "encrypted",
            0x80000: "pinned",
            0x100000: "unpinned",
        }
        result["windows_file_attributes"] = f"0x{attributes:08x}"
        result["windows_file_attribute_flags"] = [
            name for value, name in flags.items() if attributes & value
        ]
        reparse_tag = int(getattr(info, "st_reparse_tag", 0))
        if reparse_tag:
            result["windows_reparse_tag"] = f"0x{reparse_tag:08x}"
    if hasattr(info, "st_uid"):
        result["owner_id"] = info.st_uid
    if hasattr(info, "st_gid"):
        result["group_id"] = info.st_gid
    return result


def _text_metadata(path: Path, exact_limit: int = 8 * 1024 * 1024) -> dict[str, Any]:
    size = path.stat().st_size
    if size > exact_limit:
        return _sampled_text_metadata(path, size)
    lines = 0
    consumed = 0
    last = b""
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            consumed += len(chunk)
            lines += chunk.count(b"\n")
            last = chunk[-1:]
    if consumed and last != b"\n":
        lines += 1
    return {
        "line_count": lines,
        "line_count_estimated": False,
        "line_count_sampled_bytes": consumed,
    }


def _sampled_text_metadata(path: Path, size: int) -> dict[str, Any]:
    chunk_size = min(512 * 1024, max(64 * 1024, size // 16))
    offsets = sorted({0, max(0, size // 2 - chunk_size // 2), max(0, size - chunk_size)})
    newline_count = 0
    sampled = 0
    with path.open("rb") as stream:
        for offset in offsets:
            stream.seek(offset)
            chunk = stream.read(min(chunk_size, size - offset))
            sampled += len(chunk)
            newline_count += chunk.count(b"\n")
    estimate = round(newline_count * size / sampled) if sampled else 0
    if size and estimate == 0:
        estimate = 1
    return {
        "line_count": estimate,
        "line_count_estimated": True,
        "line_count_sampled_bytes": sampled,
    }


def _pdf_metadata(path: Path) -> dict[str, Any]:
    from pypdf import PdfReader

    with capture_pdf_diagnostics() as messages:
        reader = PdfReader(path, strict=False)
        encrypted = bool(reader.is_encrypted)
        page_count = None if encrypted else len(reader.pages)
        properties = (
            {str(key).lstrip("/"): str(value) for key, value in (reader.metadata or {}).items()}
            if not encrypted
            else {}
        )
    issues = pdf_health_issues(messages)
    return {
        "encrypted": encrypted,
        "page_count": page_count,
        "document_properties": properties,
        "file_health_status": "warning" if issues else "no_issues_observed",
        "file_health_issue_count": len(issues),
        "file_health_issues": issues,
    }


class _MessageCapture(logging.Handler):
    def __init__(self, messages: list[str]) -> None:
        super().__init__(logging.WARNING)
        self.messages = messages

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextmanager
def capture_pdf_diagnostics():  # type: ignore[no-untyped-def]
    """Capture pypdf diagnostics without allowing worker threads to print them."""
    messages: list[str] = []
    handler = _MessageCapture(messages)
    logger = logging.getLogger("pypdf")
    with _PDF_DIAGNOSTIC_LOCK:
        old_level = logger.level
        old_propagate = logger.propagate
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        logger.propagate = False
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                yield messages
                messages.extend(str(value.message) for value in caught)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
            logger.propagate = old_propagate


def pdf_health_issues(messages: list[str]) -> list[dict[str, str]]:
    result = []
    seen: set[tuple[str, str]] = set()
    patterns = (
        ("multiple definitions in dictionary", "pdf_duplicate_dictionary_key"),
        ("wrong pointing object", "pdf_broken_object_reference"),
        ("invalid pdf header", "pdf_nonstandard_header"),
        ("incorrect startxref", "pdf_incorrect_startxref"),
        ("xref table not zero-indexed", "pdf_nonstandard_xref"),
        ("object id", "pdf_object_reference_warning"),
    )
    for raw in messages:
        message = " ".join(str(raw).split())[:500]
        if not message:
            continue
        folded = message.casefold()
        code = next((value for needle, value in patterns if needle in folded), "pdf_parser_warning")
        identity = (code, message)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(
            {
                "code": code,
                "severity": "warning",
                "source": "pypdf",
                "message": message,
                "interpretation": (
                    "The parser recovered, but the PDF structure is nonstandard or inconsistent; "
                    "verify the document visually before acting on extracted metadata."
                ),
            }
        )
        if len(result) >= 50:
            break
    return result


def _office_metadata(path: Path, suffix: str) -> dict[str, Any]:
    if suffix in LEGACY_OFFICE_SUFFIXES:
        signature = _read_prefix(path, 8)
        return {
            "encrypted": None,
            "container": "ole_compound" if signature.startswith(b"\xd0\xcf\x11\xe0") else "unknown",
        }
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            facts: dict[str, Any] = {"encrypted": False, "container_entries": len(names)}
            if "docProps/core.xml" in names:
                with archive.open("docProps/core.xml") as stream:
                    facts["document_properties"] = _xml_leaf_values(stream.read(1_000_000))
            if suffix == ".docx":
                facts["paragraph_count"] = _count_xml_elements(archive, "word/document.xml", "}p")
            elif suffix == ".xlsx":
                facts["worksheet_count"] = sum(
                    name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
                    for name in names
                )
            elif suffix == ".pptx":
                facts["slide_count"] = sum(
                    name.startswith("ppt/slides/slide") and name.endswith(".xml") for name in names
                )
            return facts
    except zipfile.BadZipFile:
        signature = _read_prefix(path, 8)
        return {
            "encrypted": signature.startswith(b"\xd0\xcf\x11\xe0"),
            "container": "ole_encrypted_or_legacy"
            if signature.startswith(b"\xd0\xcf\x11\xe0")
            else "invalid",
        }


def _media_metadata(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        creation_flags = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
        try:
            completed = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-probesize",
                    "5000000",
                    "-analyzeduration",
                    "5000000",
                    "-show_format",
                    "-show_streams",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                check=False,
                creationflags=creation_flags,
                text=True,
                timeout=20,
            )
            if completed.returncode == 0:
                payload = json.loads(completed.stdout)
                format_info = payload.get("format", {})
                streams = [_ffprobe_stream(value) for value in payload.get("streams", [])]
                return {
                    "media_probe": "ffprobe",
                    "media_format": format_info.get("format_name", ""),
                    "media_format_description": format_info.get("format_long_name", ""),
                    "duration_seconds": _number(format_info.get("duration")),
                    "bitrate": _integer(format_info.get("bit_rate")),
                    "media_tags": _string_mapping(format_info.get("tags", {})),
                    "media_stream_count": len(streams),
                    "media_streams": streams,
                }
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            pass
    from mutagen import File

    media = File(path, easy=True)
    if media is None:
        return {"media_probe": "unavailable"}
    info = getattr(media, "info", None)
    tags = {
        str(key): [str(value) for value in values]
        for key, values in (getattr(media, "tags", None) or {}).items()
    }
    return {
        "media_probe": "mutagen",
        "duration_seconds": float(getattr(info, "length", 0.0)),
        "bitrate": int(getattr(info, "bitrate", 0) or 0),
        "sample_rate": int(getattr(info, "sample_rate", 0) or 0),
        "channels": int(getattr(info, "channels", 0) or 0),
        "media_tags": tags,
    }


def _ffprobe_stream(value: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "index",
        "codec_type",
        "codec_name",
        "codec_long_name",
        "profile",
        "width",
        "height",
        "pix_fmt",
        "sample_rate",
        "channels",
        "channel_layout",
        "bit_rate",
        "duration",
        "avg_frame_rate",
        "r_frame_rate",
    )
    result = {key: value[key] for key in keys if value.get(key) not in {None, ""}}
    if value.get("tags"):
        result["tags"] = _string_mapping(value["tags"])
    return result


def _string_mapping(value: Any) -> dict[str, str]:
    return {str(key): str(item) for key, item in value.items()} if isinstance(value, dict) else {}


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _image_metadata(path: Path) -> dict[str, Any]:
    from PIL import Image

    with Image.open(path) as image:
        return {
            "image_format": image.format,
            "width": image.width,
            "height": image.height,
            "color_mode": image.mode,
            "frames": int(getattr(image, "n_frames", 1)),
            "exif": {str(key): str(value) for key, value in image.getexif().items()},
        }


def _archive_metadata(path: Path) -> dict[str, Any]:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            members = [_zip_member(info) for info in infos[: _archive_member_limit()]]
            return {
                "archive_format": "zip",
                "archive_entry_count": len(infos),
                "archive_members": members,
                "archive_members_truncated": len(members) < len(infos),
                "archive_compressed_bytes": sum(info.compress_size for info in infos),
                "archive_uncompressed_bytes": sum(info.file_size for info in infos),
                "encrypted": any(bool(info.flag_bits & 0x1) for info in infos),
                "archive_comment": archive.comment.decode("utf-8", errors="replace"),
            }
    if path.suffix.casefold() == ".rar":
        return _rar_metadata(path)
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as archive:
            members = archive.getmembers()
            member_rows = [_tar_member(member) for member in members[: _archive_member_limit()]]
            return {
                "archive_format": "tar",
                "archive_entry_count": len(members),
                "archive_members": member_rows,
                "archive_members_truncated": len(member_rows) < len(members),
                "archive_compressed_bytes": path.stat().st_size,
                "archive_uncompressed_bytes": sum(member.size for member in members),
                "encrypted": False,
            }
    return {}


def _zip_member(info: zipfile.ZipInfo) -> dict[str, Any]:
    try:
        modified = datetime(*info.date_time).isoformat()
    except (TypeError, ValueError):
        modified = ""
    return {
        "path": info.filename,
        "is_directory": info.is_dir(),
        "compressed_size": info.compress_size,
        "uncompressed_size": info.file_size,
        "modified_at": modified,
        "crc32": f"{info.CRC:08x}",
        "compression_method": zipfile.compressor_names.get(info.compress_type, info.compress_type),
        "encrypted": bool(info.flag_bits & 0x1),
        "create_system": info.create_system,
        "create_version": info.create_version,
        "extract_version": info.extract_version,
        "external_attributes": info.external_attr,
        "comment": info.comment.decode("utf-8", errors="replace"),
    }


def _rar_metadata(path: Path) -> dict[str, Any]:
    try:
        import rarfile
    except ImportError:
        return {
            "archive_format": "rar",
            "archive_metadata_unavailable": "Install the analysis dependency group for RAR headers",
        }
    with rarfile.RarFile(path) as archive:
        infos = archive.infolist()
        rows = [_rar_member(info) for info in infos[: _archive_member_limit()]]
        return {
            "archive_format": "rar",
            "archive_entry_count": len(infos),
            "archive_members": rows,
            "archive_members_truncated": len(rows) < len(infos),
            "archive_compressed_bytes": sum(info.compress_size for info in infos),
            "archive_uncompressed_bytes": sum(info.file_size for info in infos),
            "encrypted": bool(archive.needs_password()),
            "solid": bool(archive.is_solid()),
            "archive_comment": str(archive.comment or ""),
        }


def _rar_member(info: Any) -> dict[str, Any]:
    return {
        "path": str(info.filename),
        "is_directory": bool(info.is_dir()),
        "compressed_size": int(info.compress_size),
        "uncompressed_size": int(info.file_size),
        "modified_at": _iso_datetime(getattr(info, "mtime", None)),
        "created_at": _iso_datetime(getattr(info, "ctime", None)),
        "accessed_at": _iso_datetime(getattr(info, "atime", None)),
        "crc32": f"{int(info.CRC):08x}" if getattr(info, "CRC", None) is not None else "",
        "compression_method": int(info.compress_type),
        "encrypted": bool(info.needs_password()),
        "host_os": int(info.host_os),
        "mode": int(info.mode),
        "extract_version": int(info.extract_version),
    }


def _tar_member(member: tarfile.TarInfo) -> dict[str, Any]:
    return {
        "path": member.name,
        "is_directory": member.isdir(),
        "compressed_size": None,
        "uncompressed_size": member.size,
        "modified_at": datetime.fromtimestamp(member.mtime, UTC).isoformat(),
        "mode": member.mode,
        "uid": member.uid,
        "gid": member.gid,
        "user": member.uname,
        "group": member.gname,
        "link_name": member.linkname,
        "member_type": member.type.decode("ascii", errors="replace"),
    }


def _iso_datetime(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else ""


def _archive_member_limit() -> int:
    try:
        return max(
            1_000, min(250_000, int(os.getenv("AIORGANIZER_ARCHIVE_MEMBER_LIMIT", "100000")))
        )
    except ValueError:
        return 100_000


def _executable_metadata(path: Path) -> dict[str, Any]:
    magic = _read_prefix(path, 4)
    if magic[:2] == b"MZ":
        return {**_pe_header_metadata(path), **_windows_version_metadata(path)}
    if magic == b"\x7fELF":
        return _elf_metadata(path)
    if magic in {
        b"\xce\xfa\xed\xfe",
        b"\xcf\xfa\xed\xfe",
        b"\xfe\xed\xfa\xce",
        b"\xfe\xed\xfa\xcf",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf",
        b"\xbf\xba\xfe\xca",
    }:
        return _macho_metadata(path)
    return {"binary_format": "unknown", "binary_magic": magic.hex()}


def _pe_header_metadata(path: Path) -> dict[str, Any]:
    machines = {
        0x014C: "x86",
        0x01C0: "ARM",
        0x01C4: "ARMv7",
        0x0200: "Itanium",
        0x8664: "x86-64",
        0xAA64: "ARM64",
    }
    subsystems = {
        1: "native",
        2: "windows-gui",
        3: "windows-console",
        7: "posix-console",
        9: "windows-ce-gui",
        10: "efi-application",
        11: "efi-boot-service-driver",
        12: "efi-runtime-driver",
        14: "xbox",
        16: "windows-boot-application",
    }
    with path.open("rb") as stream:
        dos = stream.read(64)
        if len(dos) < 64 or dos[:2] != b"MZ":
            return {"binary_format": "unknown"}
        pe_offset = struct.unpack_from("<I", dos, 0x3C)[0]
        if pe_offset > min(path.stat().st_size - 24, 16 * 1024 * 1024):
            return {"binary_format": "pe", "pe_header_valid": False}
        stream.seek(pe_offset)
        header = stream.read(24)
        if len(header) < 24 or header[:4] != b"PE\0\0":
            return {"binary_format": "pe", "pe_header_valid": False}
        machine, sections, timestamp, _, _, optional_size, characteristics = struct.unpack_from(
            "<HHIIIHH", header, 4
        )
        optional = stream.read(min(optional_size, 4096))
    result: dict[str, Any] = {
        "binary_format": "pe",
        "pe_header_valid": True,
        "machine": machines.get(machine, f"0x{machine:04x}"),
        "machine_id": machine,
        "section_count": sections,
        "linker_timestamp": datetime.fromtimestamp(timestamp, UTC).isoformat() if timestamp else "",
        "pe_characteristics": f"0x{characteristics:04x}",
    }
    if len(optional) < 70:
        return result
    magic = struct.unpack_from("<H", optional)[0]
    result["pe_kind"] = {0x10B: "PE32", 0x20B: "PE32+", 0x107: "ROM"}.get(magic, f"0x{magic:04x}")
    result["linker_version"] = f"{optional[2]}.{optional[3]}"
    result["operating_system_version"] = (
        f"{struct.unpack_from('<H', optional, 40)[0]}.{struct.unpack_from('<H', optional, 42)[0]}"
    )
    result["image_version"] = (
        f"{struct.unpack_from('<H', optional, 44)[0]}.{struct.unpack_from('<H', optional, 46)[0]}"
    )
    result["subsystem_version"] = (
        f"{struct.unpack_from('<H', optional, 48)[0]}.{struct.unpack_from('<H', optional, 50)[0]}"
    )
    subsystem = struct.unpack_from("<H", optional, 68)[0]
    result["subsystem"] = subsystems.get(subsystem, str(subsystem))
    if len(optional) >= 72:
        result["dll_characteristics"] = f"0x{struct.unpack_from('<H', optional, 70)[0]:04x}"
    if magic == 0x10B and len(optional) >= 32:
        result["image_base"] = f"0x{struct.unpack_from('<I', optional, 28)[0]:x}"
    elif magic == 0x20B and len(optional) >= 32:
        result["image_base"] = f"0x{struct.unpack_from('<Q', optional, 24)[0]:x}"
    return result


def _windows_version_metadata(path: Path) -> dict[str, Any]:
    if platform.system() != "Windows":
        return {"portable_executable": True, "version_resource_available": False}
    import ctypes
    from ctypes import wintypes

    class VSFixedFileInfo(ctypes.Structure):
        _fields_ = [
            (name, wintypes.DWORD)
            for name in (
                "signature",
                "struct_version",
                "file_version_ms",
                "file_version_ls",
                "product_version_ms",
                "product_version_ls",
                "file_flags_mask",
                "file_flags",
                "file_os",
                "file_type",
                "file_subtype",
                "file_date_ms",
                "file_date_ls",
            )
        ]

    version = ctypes.windll.version
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version.GetFileVersionInfoW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    version.GetFileVersionInfoW.restype = wintypes.BOOL
    version.VerQueryValueW.argtypes = [
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.UINT),
    ]
    version.VerQueryValueW.restype = wintypes.BOOL

    ignored = wintypes.DWORD()
    size = version.GetFileVersionInfoSizeW(str(path), ctypes.byref(ignored))
    if not size:
        return {"portable_executable": True, "version_resource_available": False}
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        return {"portable_executable": True, "version_resource_available": False}
    pointer = ctypes.c_void_p()
    length = wintypes.UINT()
    result: dict[str, Any] = {"portable_executable": True, "version_resource_available": True}
    if version.VerQueryValueW(
        buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)
    ) and length.value >= ctypes.sizeof(VSFixedFileInfo):
        fixed = ctypes.cast(pointer, ctypes.POINTER(VSFixedFileInfo)).contents
        if fixed.signature == 0xFEEF04BD:
            result.update(
                {
                    "fixed_file_version": _quad_version(
                        fixed.file_version_ms, fixed.file_version_ls
                    ),
                    "fixed_product_version": _quad_version(
                        fixed.product_version_ms, fixed.product_version_ls
                    ),
                    "version_file_flags": f"0x{fixed.file_flags:08x}",
                    "version_file_os": f"0x{fixed.file_os:08x}",
                    "version_file_type": fixed.file_type,
                    "version_file_subtype": fixed.file_subtype,
                }
            )
    translations: list[tuple[int, int]] = []
    if (
        version.VerQueryValueW(
            buffer, "\\VarFileInfo\\Translation", ctypes.byref(pointer), ctypes.byref(length)
        )
        and length.value >= 4
    ):
        values = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ushort))
        translations = [
            (values[index], values[index + 1]) for index in range(0, length.value // 2, 2)
        ]
    for fallback in ((0x0409, 0x04B0), (0x0409, 0x04E4), (0, 0x04B0), (0, 0x04E4)):
        if fallback not in translations:
            translations.append(fallback)
    result["version_translations"] = [
        f"{language:04x}{codepage:04x}" for language, codepage in translations
    ]
    for language, codepage in translations:
        prefix = f"\\StringFileInfo\\{language:04x}{codepage:04x}\\"
        for key in (
            "CompanyName",
            "Comments",
            "FileDescription",
            "FileVersion",
            "InternalName",
            "LegalCopyright",
            "LegalTrademarks",
            "OriginalFilename",
            "PrivateBuild",
            "ProductName",
            "ProductVersion",
            "SpecialBuild",
        ):
            if key not in result and (
                version.VerQueryValueW(
                    buffer, prefix + key, ctypes.byref(pointer), ctypes.byref(length)
                )
                and length.value
            ):
                result[key] = ctypes.wstring_at(pointer, length.value).rstrip("\x00")
    return result


def _quad_version(high: int, low: int) -> str:
    return f"{high >> 16}.{high & 0xFFFF}.{low >> 16}.{low & 0xFFFF}"


def _msi_metadata(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"binary_format": "msi", "installer_database": True}
    if platform.system() != "Windows":
        result["installer_properties_available"] = False
        return result
    import ctypes
    from ctypes import wintypes

    msi = ctypes.windll.msi
    database = wintypes.UINT()
    if msi.MsiOpenDatabaseW(str(path), None, ctypes.byref(database)) != 0:
        result["installer_database"] = False
        return result
    view = wintypes.UINT()
    properties: dict[str, str] = {}
    try:
        query = "SELECT `Property`, `Value` FROM `Property`"
        if msi.MsiDatabaseOpenViewW(database, query, ctypes.byref(view)) != 0:
            return result
        if msi.MsiViewExecute(view, 0) != 0:
            return result
        record = wintypes.UINT()
        for _ in range(512):
            status = msi.MsiViewFetch(view, ctypes.byref(record))
            if status != 0:
                break
            try:
                key = _msi_record_string(msi, record.value, 1)
                value = _msi_record_string(msi, record.value, 2)
                if key:
                    properties[key] = value
            finally:
                msi.MsiCloseHandle(record.value)
    finally:
        if view.value:
            msi.MsiViewClose(view.value)
            msi.MsiCloseHandle(view.value)
        msi.MsiCloseHandle(database.value)
    result["installer_properties_available"] = True
    result["installer_properties"] = properties
    for source, target in (
        ("ProductName", "ProductName"),
        ("ProductVersion", "ProductVersion"),
        ("Manufacturer", "CompanyName"),
        ("ProductCode", "ProductCode"),
        ("UpgradeCode", "UpgradeCode"),
        ("ProductLanguage", "ProductLanguage"),
    ):
        if source in properties:
            result[target] = properties[source]
    return result


def _msi_record_string(msi: Any, record: int, field: int) -> str:
    import ctypes
    from ctypes import wintypes

    length = wintypes.DWORD()
    msi.MsiRecordGetStringW(record, field, None, ctypes.byref(length))
    buffer = ctypes.create_unicode_buffer(length.value + 1)
    capacity = wintypes.DWORD(len(buffer))
    if msi.MsiRecordGetStringW(record, field, buffer, ctypes.byref(capacity)) != 0:
        return ""
    return buffer.value


def _msix_metadata(path: Path) -> dict[str, Any]:
    result = {"binary_format": "msix", **_archive_metadata(path)}
    if not zipfile.is_zipfile(path):
        return result
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        manifest = next(
            (
                candidate
                for candidate in ("AppxManifest.xml", "AppxMetadata/AppxBundleManifest.xml")
                if candidate in names
            ),
            "",
        )
        if not manifest:
            return result
        with archive.open(manifest) as stream:
            root = ElementTree.fromstring(stream.read(2_000_000))
        identity = next(
            (element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "Identity"),
            None,
        )
        if identity is not None:
            result["package_identity"] = {
                str(key): str(value) for key, value in identity.attrib.items()
            }
        result["package_properties"] = {
            element.tag.rsplit("}", 1)[-1]: (element.text or "").strip()
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1]
            in {"DisplayName", "PublisherDisplayName", "Description", "Logo"}
            and (element.text or "").strip()
        }
    return result


def _elf_metadata(path: Path) -> dict[str, Any]:
    try:
        from elftools.elf.dynamic import DynamicSection
        from elftools.elf.elffile import ELFFile
        from elftools.elf.sections import NoteSection
    except ImportError:
        return _elf_header_metadata(path) | {"elf_details_available": False}

    with path.open("rb") as stream:
        elf = ELFFile(stream)
        header = elf.header
        result: dict[str, Any] = {
            "binary_format": "elf",
            "elf_details_available": True,
            "elf_class": elf.elfclass,
            "endianness": "little" if elf.little_endian else "big",
            "elf_type": str(header["e_type"]),
            "machine": str(header["e_machine"]),
            "entry_point": f"0x{int(header['e_entry']):x}",
            "elf_flags": f"0x{int(header['e_flags']):x}",
            "section_count": int(header["e_shnum"]),
            "program_header_count": int(header["e_phnum"]),
            "os_abi": str(header["e_ident"]["EI_OSABI"]),
            "abi_version": int(header["e_ident"]["EI_ABIVERSION"]),
        }
        needed: list[str] = []
        comments: list[str] = []
        notes: list[dict[str, Any]] = []
        for section in elf.iter_sections():
            if section.name == ".comment":
                comments = [
                    value.decode("utf-8", errors="replace")
                    for value in section.data()[:1_000_000].split(b"\0")
                    if value
                ]
            if isinstance(section, DynamicSection):
                for tag in section.iter_tags():
                    if tag.entry.d_tag == "DT_NEEDED":
                        needed.append(str(tag.needed))
                    elif tag.entry.d_tag == "DT_SONAME":
                        result["shared_object_name"] = str(tag.soname)
                    elif tag.entry.d_tag in {"DT_RPATH", "DT_RUNPATH"}:
                        result[str(tag.entry.d_tag).removeprefix("DT_").casefold()] = str(
                            getattr(tag, "rpath", getattr(tag, "runpath", ""))
                        )
            if isinstance(section, NoteSection):
                for note in section.iter_notes():
                    description = note.get("n_desc", "")
                    if isinstance(description, bytes):
                        description = description.hex()
                    notes.append(
                        {
                            "name": str(note.get("n_name", "")),
                            "type": str(note.get("n_type", "")),
                            "description": str(description),
                        }
                    )
                    if len(notes) >= 64:
                        break
        for segment in elf.iter_segments():
            if segment.header.p_type == "PT_INTERP":
                result["interpreter"] = str(segment.get_interp_name())
                break
        result["needed_libraries"] = needed[:512]
        result["compiler_comments"] = comments[:128]
        result["elf_notes"] = notes
        return result


def _elf_header_metadata(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        header = stream.read(64)
    if len(header) < 52 or header[:4] != b"\x7fELF":
        return {"binary_format": "unknown"}
    elf_class = 64 if header[4] == 2 else 32
    endian = "<" if header[5] == 1 else ">"
    return {
        "binary_format": "elf",
        "elf_class": elf_class,
        "endianness": "little" if endian == "<" else "big",
        "elf_type": struct.unpack_from(endian + "H", header, 16)[0],
        "machine_id": struct.unpack_from(endian + "H", header, 18)[0],
        "os_abi": header[7],
        "abi_version": header[8],
    }


def _macho_metadata(path: Path) -> dict[str, Any]:
    magic = _read_prefix(path, 4)
    if magic in {
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf",
        b"\xbf\xba\xfe\xca",
    }:
        return _fat_macho_metadata(path, magic)
    endian = "<" if magic in {b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe"} else ">"
    is_64 = magic in {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"}
    header_size = 32 if is_64 else 28
    with path.open("rb") as stream:
        header = stream.read(header_size)
        if len(header) < header_size:
            return {"binary_format": "mach-o", "macho_header_valid": False}
        values = struct.unpack_from(endian + "IiiIIII", header)
        _, cpu_type, cpu_subtype, file_type, command_count, command_bytes, flags = values
        if command_bytes > 16 * 1024 * 1024 or command_bytes > path.stat().st_size - header_size:
            return {"binary_format": "mach-o", "macho_header_valid": False}
        commands = stream.read(command_bytes)
    result: dict[str, Any] = {
        "binary_format": "mach-o",
        "macho_header_valid": True,
        "macho_bits": 64 if is_64 else 32,
        "endianness": "little" if endian == "<" else "big",
        "cpu_type": cpu_type,
        "cpu_subtype": cpu_subtype,
        "macho_file_type": file_type,
        "load_command_count": command_count,
        "load_command_bytes": command_bytes,
        "macho_flags": f"0x{flags:08x}",
    }
    offset = 0
    for _ in range(command_count):
        if offset + 8 > len(commands):
            break
        command, size = struct.unpack_from(endian + "II", commands, offset)
        if size < 8 or offset + size > len(commands):
            break
        data = commands[offset : offset + size]
        if command == 0x1B and len(data) >= 24:
            raw = data[8:24].hex()
            result["uuid"] = f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
        elif command == 0x32 and len(data) >= 24:
            platform_id, minimum, sdk, _ = struct.unpack_from(endian + "IIII", data, 8)
            result["build_platform"] = platform_id
            result["minimum_os_version"] = _packed_version(minimum)
            result["sdk_version"] = _packed_version(sdk)
        elif command == 0x2A and len(data) >= 16:
            result["source_version"] = _source_version(struct.unpack_from(endian + "Q", data, 8)[0])
        elif command == 0xD and len(data) >= 24:
            name_offset, _, current, compatibility = struct.unpack_from(endian + "IIII", data, 8)
            if 0 < name_offset < len(data):
                result["dylib_install_name"] = (
                    data[name_offset:].split(b"\0", 1)[0].decode("utf-8", errors="replace")
                )
            result["dylib_current_version"] = _packed_version(current)
            result["dylib_compatibility_version"] = _packed_version(compatibility)
        offset += size
    return result


def _fat_macho_metadata(path: Path, magic: bytes) -> dict[str, Any]:
    endian = ">" if magic in {b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"} else "<"
    is_64 = magic in {b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}
    with path.open("rb") as stream:
        header = stream.read(8)
        count = struct.unpack_from(endian + "I", header, 4)[0]
        count = min(count, 128)
        record_size = 32 if is_64 else 20
        records = stream.read(count * record_size)
    architectures = []
    for index in range(count):
        offset = index * record_size
        if is_64:
            cpu, subtype, file_offset, size, align, _ = struct.unpack_from(
                endian + "iiQQII", records, offset
            )
        else:
            cpu, subtype, file_offset, size, align = struct.unpack_from(
                endian + "iiIII", records, offset
            )
        architectures.append(
            {
                "cpu_type": cpu,
                "cpu_subtype": subtype,
                "offset": file_offset,
                "size": size,
                "align": align,
            }
        )
    return {
        "binary_format": "mach-o-fat",
        "architecture_count": count,
        "architectures": architectures,
    }


def _packed_version(value: int) -> str:
    return f"{value >> 16}.{(value >> 8) & 0xFF}.{value & 0xFF}"


def _source_version(value: int) -> str:
    return ".".join(
        str(part)
        for part in (
            (value >> 40) & 0xFFFFFF,
            (value >> 30) & 0x3FF,
            (value >> 20) & 0x3FF,
            (value >> 10) & 0x3FF,
            value & 0x3FF,
        )
    )


def _xml_leaf_values(data: bytes) -> dict[str, str]:
    root = ElementTree.fromstring(data)
    return {
        element.tag.rsplit("}", 1)[-1]: (element.text or "").strip()
        for element in root.iter()
        if len(element) == 0 and (element.text or "").strip()
    }


def _count_xml_elements(archive: zipfile.ZipFile, name: str, suffix: str) -> int:
    if name not in archive.namelist():
        return 0
    with archive.open(name) as stream:
        root = ElementTree.fromstring(stream.read(16_000_000))
    return sum(element.tag.endswith(suffix) for element in root.iter())


def _read_prefix(path: Path, length: int) -> bytes:
    with path.open("rb") as stream:
        return stream.read(length)
