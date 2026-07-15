from __future__ import annotations

import os
import time
from pathlib import Path

from ai_organizer.adapters.filesystem import CleanupAnalyzer
from ai_organizer.domain.cleanup import CleanupKind


def test_empty_folder_and_manifest_backed_artifacts_are_distinct(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    empty = root / "old-empty"
    empty.mkdir()
    (root / "pyproject.toml").write_text("[project]", encoding="utf-8")
    cache = root / "package" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "module.pyc").write_bytes(b"generated")
    items = [
        {
            "id": "empty",
            "root_id": "root",
            "relative_path": "old-empty",
            "is_dir": True,
        },
        {
            "id": "manifest",
            "root_id": "root",
            "relative_path": "pyproject.toml",
            "inside_protected_project": True,
            "protected_project_path": "",
        },
    ]

    candidates = CleanupAnalyzer().analyze(
        "root", root, items, move_created_empty_paths={empty}
    )
    lookup = {candidate.relative_path: candidate for candidate in candidates}

    assert lookup["old-empty"].kind == CleanupKind.EMPTY_FOLDER
    assert lookup["old-empty"].selected_by_default
    assert lookup["package/__pycache__"].kind == CleanupKind.BUILD_ARTIFACT
    assert lookup["package/__pycache__"].regeneration_evidence
    assert not lookup["package/__pycache__"].selected_by_default


def test_directory_name_without_tool_evidence_is_not_a_build_artifact(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    build = root / "build"
    build.mkdir(parents=True)
    (build / "important.txt").write_text("hand authored", encoding="utf-8")
    items = [
        {
            "id": "root-file",
            "root_id": "root",
            "relative_path": "notes.txt",
            "inside_protected_project": True,
            "protected_project_path": "",
        }
    ]
    candidates = CleanupAnalyzer().analyze("root", root, items)
    assert not any(candidate.kind == CleanupKind.BUILD_ARTIFACT for candidate in candidates)


def test_active_or_recent_partial_is_not_proposed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    active = root / ".aiorganizer-partial-active"
    old = root / ".aiorganizer-partial-old"
    recent = root / ".aiorganizer-partial-recent"
    for path in (active, old, recent):
        path.write_bytes(b"partial")
    old_time = time.time() - 7_200
    os.utime(active, (old_time, old_time))
    os.utime(old, (old_time, old_time))
    items = [
        {
            "id": path.name,
            "root_id": "root",
            "relative_path": path.name,
            "is_dir": False,
        }
        for path in (active, old, recent)
    ]
    candidates = CleanupAnalyzer().analyze(
        "root", root, items, active_operation_paths={active}
    )
    partials = {
        candidate.relative_path
        for candidate in candidates
        if candidate.kind == CleanupKind.ABANDONED_PARTIAL
    }
    assert partials == {old.name}
