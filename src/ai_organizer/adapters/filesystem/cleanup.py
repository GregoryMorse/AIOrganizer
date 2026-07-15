from __future__ import annotations

import os
import time
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path

from ai_organizer.domain.cleanup import CleanupCandidate, CleanupKind
from ai_organizer.domain.models import new_id

_QUARANTINE_NAMES = {".aiorganizer-cleanup-quarantine", ".aiorganizer-quarantine"}
_PYTHON_MARKERS = {"pyproject.toml", "requirements.txt", "poetry.lock", "uv.lock"}
_NODE_MARKERS = {"package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"}
_RUST_MARKERS = {"cargo.toml", "cargo.lock"}
_JAVA_MARKERS = {"pom.xml", "build.gradle", "gradlew"}
_CMAKE_MARKERS = {"cmakelists.txt"}
_LATEX_ARTIFACTS = {
    ".aux",
    ".bbl",
    ".bcf",
    ".blg",
    ".fdb_latexmk",
    ".fls",
    ".glg",
    ".glo",
    ".gls",
    ".idx",
    ".ilg",
    ".ind",
    ".lof",
    ".log",
    ".lot",
    ".nav",
    ".out",
    ".run.xml",
    ".snm",
    ".synctex.gz",
    ".toc",
    ".vrb",
}


class CleanupAnalyzer:
    """Find conservative cleanup candidates without reading file contents."""

    def analyze(
        self,
        root_id: str,
        root: Path,
        items: Iterable[dict[str, object]],
        *,
        active_operation_paths: set[Path] | None = None,
        move_created_empty_paths: set[Path] | None = None,
        now: float | None = None,
        max_candidates: int = 5_000,
    ) -> list[CleanupCandidate]:
        root = root.resolve(strict=True)
        active = {path.resolve(strict=False) for path in (active_operation_paths or set())}
        move_empty = (
            {path.resolve(strict=False) for path in move_created_empty_paths}
            if move_created_empty_paths is not None
            else None
        )
        candidates: list[CleanupCandidate] = []
        item_rows = [row for row in items if str(row.get("root_id", "")) == root_id]

        for row in item_rows:
            if len(candidates) >= max_candidates:
                break
            relative = str(row.get("relative_path", ""))
            path = root / relative
            if _is_internal_path(relative) or path.resolve(strict=False) in active:
                continue
            if (
                bool(row.get("is_dir"))
                and not bool(row.get("inside_protected_project"))
                and not bool(row.get("is_project_root"))
                and (move_empty is None or path.resolve(strict=False) in move_empty)
                and _actually_empty(path)
            ):
                candidates.append(
                    CleanupCandidate(
                        str(row.get("id", new_id("cleanup_item"))),
                        root_id,
                        relative,
                        CleanupKind.EMPTY_FOLDER,
                        0,
                        1,
                        "Folder became empty after an accepted move and was rechecked on disk.",
                        ("Empty folders are restored by moving them back from quarantine.",),
                        ("Project roots and folders inside projects are excluded.",),
                        selected_by_default=True,
                    )
                )
            if _is_abandoned_partial(path, now=now) and path.resolve(strict=False) not in active:
                size, count = _path_facts(path)
                candidates.append(
                    CleanupCandidate(
                        str(row.get("id", new_id("cleanup_item"))),
                        root_id,
                        relative,
                        CleanupKind.ABANDONED_PARTIAL,
                        size,
                        count,
                        "AIOrganizer partial name is older than one hour and is not referenced "
                        "by an active journal.",
                        ("The original source remains authoritative.",),
                        ("Active journal paths are excluded.",),
                        selected_by_default=True,
                    )
                )

        project_paths = _project_paths(root, item_rows)
        seen = {candidate.relative_path.casefold() for candidate in candidates}
        for project_path, inventory_markers in project_paths:
            for candidate in self._project_candidates(
                root_id, root, project_path, inventory_markers, max_candidates - len(candidates)
            ):
                key = candidate.relative_path.casefold()
                if key not in seen and (root / candidate.relative_path).resolve(strict=False) not in active:
                    candidates.append(candidate)
                    seen.add(key)
        return sorted(candidates, key=lambda value: value.relative_path.casefold())

    def _project_candidates(
        self,
        root_id: str,
        source_root: Path,
        project_root: Path,
        inventory_markers: set[str],
        remaining: int,
    ) -> list[CleanupCandidate]:
        if remaining <= 0 or not project_root.is_dir() or project_root.is_symlink():
            return []
        markers = inventory_markers | _directory_names(project_root)
        has_tex = any(name.endswith(".tex") for name in markers)
        candidates: list[CleanupCandidate] = []
        for directory, child_dirs, filenames in os.walk(project_root, followlinks=False):
            current = Path(directory)
            if current != project_root and any(name.casefold() in {".git", ".hg", ".svn"} for name in child_dirs):
                child_dirs.clear()
                continue
            child_dirs[:] = [
                name
                for name in child_dirs
                if name.casefold() not in _QUARANTINE_NAMES
                and not (current / name).is_symlink()
            ]
            for name in list(child_dirs):
                path = current / name
                evidence = _artifact_evidence(name, markers, path)
                if not evidence:
                    continue
                size, count = _path_facts(path)
                candidates.append(
                    _artifact_candidate(root_id, source_root, path, size, count, evidence)
                )
                child_dirs.remove(name)
                if len(candidates) >= remaining:
                    return candidates
            if has_tex or any(name.casefold().endswith(".tex") for name in filenames):
                has_tex = True
                for filename in filenames:
                    path = current / filename
                    if path.is_symlink() or _compound_suffix(path.name) not in _LATEX_ARTIFACTS:
                        continue
                    size, count = _path_facts(path)
                    candidates.append(
                        _artifact_candidate(
                            root_id,
                            source_root,
                            path,
                            size,
                            count,
                            ("TeX source exists in this project", f"generated suffix {_compound_suffix(path.name)}"),
                        )
                    )
                    if len(candidates) >= remaining:
                        return candidates
        return candidates


def _artifact_candidate(
    root_id: str,
    source_root: Path,
    path: Path,
    size: int,
    count: int,
    evidence: tuple[str, ...],
) -> CleanupCandidate:
    return CleanupCandidate(
        new_id("cleanup_item"),
        root_id,
        path.relative_to(source_root).as_posix(),
        CleanupKind.BUILD_ARTIFACT,
        size,
        count,
        "Known generated output matched a project tool manifest and artifact rule.",
        evidence,
        (
            "Version-control metadata, source files, virtual environments, and unknown build "
            "directories are excluded.",
        ),
        selected_by_default=False,
    )


def _artifact_evidence(name: str, markers: set[str], path: Path) -> tuple[str, ...]:
    folded = name.casefold()
    if folded in {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"} and markers & _PYTHON_MARKERS:
        return (f"Python manifest: {sorted(markers & _PYTHON_MARKERS)[0]}", f"known Python cache: {name}")
    if folded == "node_modules" and markers & _NODE_MARKERS:
        return (f"Node manifest: {sorted(markers & _NODE_MARKERS)[0]}", "dependency install output")
    if folded == "target" and markers & _RUST_MARKERS:
        return (f"Rust manifest: {sorted(markers & _RUST_MARKERS)[0]}", "Cargo target output")
    if folded in {"target", "build"} and markers & _JAVA_MARKERS:
        return (f"Java build manifest: {sorted(markers & _JAVA_MARKERS)[0]}", f"generated {name} output")
    if folded == "build" and markers & _CMAKE_MARKERS and ((path / "CMakeCache.txt").exists() or (path / "CMakeFiles").is_dir()):
        return ("CMakeLists.txt", "CMakeCache.txt/CMakeFiles generation marker")
    return ()


def _project_paths(
    root: Path, items: list[dict[str, object]]
) -> list[tuple[Path, set[str]]]:
    projects: list[tuple[Path, set[str]]] = []
    if any(bool(row.get("inside_protected_project")) and not row.get("protected_project_path") for row in items):
        projects.append((root, _directory_names(root)))
    for row in items:
        if not bool(row.get("is_project_root")):
            continue
        project = root / str(row["relative_path"])
        markers = {str(value).casefold() for value in row.get("project_markers", ())}
        projects.append((project, markers))
    return projects


def _directory_names(path: Path) -> set[str]:
    try:
        return {entry.name.casefold() for entry in os.scandir(path)}
    except OSError:
        return set()


def _path_facts(path: Path) -> tuple[int, int]:
    if path.is_file() or path.is_symlink():
        try:
            return path.stat(follow_symlinks=False).st_size, 1
        except OSError:
            return 0, 1
    size = 0
    count = 1
    for directory, dirs, files in os.walk(path, followlinks=False):
        current = Path(directory)
        dirs[:] = [name for name in dirs if not (current / name).is_symlink()]
        count += len(dirs) + len(files)
        for filename in files:
            with suppress(OSError):
                size += (current / filename).stat(follow_symlinks=False).st_size
    return size, count


def _actually_empty(path: Path) -> bool:
    try:
        return path.is_dir() and not any(path.iterdir())
    except OSError:
        return False


def _is_internal_path(relative: str) -> bool:
    return any(part.casefold() in _QUARANTINE_NAMES for part in Path(relative).parts)


def _is_abandoned_partial(path: Path, *, now: float | None) -> bool:
    if not path.name.casefold().startswith(".aiorganizer-partial-"):
        return False
    try:
        age = (time.time() if now is None else now) - path.stat(follow_symlinks=False).st_mtime
    except OSError:
        return False
    return age >= 3_600


def _compound_suffix(name: str) -> str:
    folded = name.casefold()
    for suffix in (".synctex.gz", ".run.xml", ".fdb_latexmk"):
        if folded.endswith(suffix):
            return suffix
    return Path(folded).suffix
