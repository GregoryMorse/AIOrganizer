from __future__ import annotations

from contextlib import suppress
from pathlib import Path

from ai_organizer.adapters.filesystem import FileSystemInventory


def test_inventory_does_not_follow_symlinks_and_detects_project(tmp_path: Path) -> None:
    root = tmp_path / "root"
    project = root / "project"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='demo'", encoding="utf-8")
    (project / "module.py").write_text("print('ok')", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("private", encoding="utf-8")
    with suppress(OSError):
        (root / "link").symlink_to(outside, target_is_directory=True)
    scanner = FileSystemInventory()
    items = scanner.scan("root", root, [])
    assert any(item.relative_path == "project" and item.is_project_root for item in items)
    assert not any(item.relative_path == "project/module.py" for item in items)
    assert not any("private.txt" in item.relative_path for item in items)


def test_project_source_root_protects_every_scanned_child(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "PYPROJECT.TOML").write_text("[project]", encoding="utf-8")
    (root / "notes.txt").write_text("notes", encoding="utf-8")
    folder = root / "docs"
    folder.mkdir()
    (folder / "readme.md").write_text("readme", encoding="utf-8")

    items = FileSystemInventory().scan("root", root, [])

    assert {item.relative_path for item in items} == {
        "PYPROJECT.TOML",
        "docs",
        "docs/readme.md",
        "notes.txt",
    }
    assert all(item.inside_protected_project for item in items)
    assert all(item.protected_project_path == "" for item in items)


def test_cleanup_quarantine_is_excluded_from_inventory(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    visible = root / "visible.txt"
    visible.write_text("visible", encoding="utf-8")
    quarantined = root / ".AIOrganizer-Cleanup-Quarantine" / "journal" / "old.txt"
    quarantined.parent.mkdir(parents=True)
    quarantined.write_text("quarantined", encoding="utf-8")

    items = FileSystemInventory().scan("root", root, [])

    assert [item.relative_path for item in items] == ["visible.txt"]
