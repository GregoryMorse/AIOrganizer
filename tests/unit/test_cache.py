from __future__ import annotations

from pathlib import Path

from ai_organizer.adapters.persistence import WorkspaceCache


def test_workspace_cache_is_external_and_keyed(tmp_path: Path) -> None:
    cache = WorkspaceCache("workspace_test", tmp_path)
    first = cache.artifact_path("item_test", "evidence", "thumbnail", ".png")
    second = cache.artifact_path("item_test", "evidence", "thumbnail", ".png")
    assert first == second
    assert tmp_path in first.parents
    first.write_bytes(b"derived")
    cache.clear()
    assert not cache.root.exists()
