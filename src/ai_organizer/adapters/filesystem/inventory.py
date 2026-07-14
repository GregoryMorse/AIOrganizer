from __future__ import annotations

import fnmatch
import hashlib
import mimetypes
import os
import platform
import stat
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from ai_organizer.domain.models import ItemSnapshot, RootCapabilities

PROJECT_MARKERS = {
    ".git",
    ".hg",
    ".svn",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "CMakeLists.txt",
}


class FileSystemInventory:
    def capabilities(self, root: Path) -> RootCapabilities:
        resolved = root.resolve(strict=False)
        reachable = resolved.exists() and resolved.is_dir()
        writable = reachable and os.access(resolved, os.W_OK)
        volume_id = _volume_id(resolved) if reachable else "unavailable"
        text = str(resolved)
        is_network = text.startswith("\\\\") or text.startswith("//")
        is_removable = _is_removable(resolved)
        case_sensitive = (
            _detect_case_sensitivity(resolved) if writable else platform.system() != "Windows"
        )
        return RootCapabilities(
            reachable=reachable,
            writable=writable,
            case_sensitive=case_sensitive,
            supports_rename=writable,
            stable_file_id=reachable,
            volume_id=volume_id,
            is_network=is_network,
            is_removable=is_removable,
            supports_placeholders=platform.system() == "Windows",
        )

    def scan(self, root_id: str, root: Path, exclusions: Sequence[str]) -> list[ItemSnapshot]:
        root_resolved = root.resolve(strict=True)
        root_device = root_resolved.stat(follow_symlinks=False).st_dev
        results: list[ItemSnapshot] = []
        stack = [root_resolved]
        while stack:
            directory = stack.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue
            for entry in entries:
                path = Path(entry.path)
                relative = path.relative_to(root_resolved).as_posix()
                if _excluded(relative, exclusions):
                    continue
                try:
                    if entry.is_symlink():
                        continue
                    info = entry.stat(follow_symlinks=False)
                    placeholder = _is_placeholder(info)
                    crosses_device = bool(
                        info.st_dev and root_device and info.st_dev != root_device
                    )
                    if (_is_reparse_point(info) and not placeholder) or crosses_device:
                        continue
                    is_dir = stat.S_ISDIR(info.st_mode)
                    file_id = f"{info.st_dev}:{info.st_ino}"
                    mime = "inode/directory" if is_dir else _mime(path)
                    project_markers = (
                        tuple(
                            sorted(marker for marker in PROJECT_MARKERS if (path / marker).exists())
                        )
                        if is_dir
                        else ()
                    )
                    item = ItemSnapshot(
                        id=f"item_{uuid4().hex}",
                        root_id=root_id,
                        relative_path=relative,
                        size=info.st_size,
                        modified_ns=info.st_mtime_ns,
                        file_id=file_id,
                        mime_type=mime,
                        is_dir=is_dir,
                        is_placeholder=placeholder,
                        is_project_root=bool(project_markers),
                        project_markers=project_markers,
                        has_build_outputs=is_dir
                        and any(
                            (path / name).is_dir() for name in ("build", "dist", "target", "out")
                        ),
                        has_virtual_environment=is_dir
                        and any(
                            (path / name).is_dir() for name in (".venv", "venv", "node_modules")
                        ),
                        has_nested_repositories=is_dir and _has_nested_repository(path),
                    )
                    results.append(item)
                    if is_dir and not item.is_project_root and not item.is_placeholder:
                        stack.append(path)
                except OSError:
                    continue
        return sorted(results, key=lambda item: item.relative_path.casefold())


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _mime(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _excluded(relative: str, patterns: Sequence[str]) -> bool:
    folded = relative.casefold()
    return any(fnmatch.fnmatch(folded, pattern.casefold()) for pattern in patterns)


def _volume_id(path: Path) -> str:
    info = path.stat()
    anchor = path.anchor.casefold()
    return hashlib.sha256(f"{anchor}:{info.st_dev}".encode()).hexdigest()[:24]


def _detect_case_sensitivity(root: Path) -> bool:
    probe = root / f".aiorganizer-case-{uuid4().hex}"
    try:
        probe.touch(exist_ok=False)
        return not probe.with_name(probe.name.upper()).exists()
    except OSError:
        return platform.system() != "Windows"
    finally:
        with suppress(OSError):
            probe.unlink(missing_ok=True)


def _is_removable(path: Path) -> bool:
    if platform.system() != "Windows":
        return str(path).startswith(("/media/", "/run/media/", "/Volumes/"))
    try:
        import ctypes

        drive = path.drive + "\\"
        return bool(drive and ctypes.windll.kernel32.GetDriveTypeW(drive) == 2)
    except (AttributeError, OSError):
        return False


def _is_placeholder(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    offline = getattr(stat, "FILE_ATTRIBUTE_OFFLINE", 0x1000)
    recall = 0x400000 | 0x40000000
    return bool(attributes & (offline | recall))


def _is_reparse_point(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse)


def _has_nested_repository(path: Path) -> bool:
    try:
        for child in path.iterdir():
            if child.is_dir() and not child.is_symlink() and (child / ".git").exists():
                return True
    except OSError:
        return False
    return False
