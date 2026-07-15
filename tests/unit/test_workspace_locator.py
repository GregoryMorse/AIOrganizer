from __future__ import annotations

from pathlib import Path

from ai_organizer.bootstrap.workspace_locator import (
    active_workspace_file,
    publish_active_workspace,
    read_active_workspace,
)


def test_active_workspace_pointer_round_trip(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AIORGANIZER_STATE_DIR", str(tmp_path / "state"))
    workspace = tmp_path / "test.aioworkspace"
    workspace.write_bytes(b"workspace")

    publish_active_workspace(workspace)

    assert active_workspace_file().is_file()
    assert read_active_workspace() == workspace.resolve()
