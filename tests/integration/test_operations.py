from __future__ import annotations

from pathlib import Path

import pytest

from ai_organizer.adapters.filesystem import (
    FileOperationEngine,
    FolderCreateRequest,
    MoveRequest,
    RenameRequest,
    SnapshotToken,
)
from ai_organizer.adapters.filesystem import operations as operations_module


def move_request(source: Path, target: Path, source_root: Path, destination: Path) -> MoveRequest:
    return MoveRequest(
        source,
        target,
        SnapshotToken.capture(source),
        source_root,
        destination,
        str(source_root.stat().st_dev),
        str(destination.stat().st_dev),
    )


def test_rename_swap_is_staged_without_overwrite(tmp_path: Path) -> None:
    left = tmp_path / "left.txt"
    right = tmp_path / "right.txt"
    left.write_text("left", encoding="utf-8")
    right.write_text("right", encoding="utf-8")
    journals = []
    engine = FileOperationEngine(journals.append)
    completed = engine.execute_renames(
        "plan",
        [
            RenameRequest(left, right, SnapshotToken.capture(left)),
            RenameRequest(right, left, SnapshotToken.capture(right)),
        ],
    )
    assert left.read_text(encoding="utf-8") == "right"
    assert right.read_text(encoding="utf-8") == "left"
    assert journals[-1].state == "completed"
    engine.execute_undo("undo_plan", completed)
    assert left.read_text(encoding="utf-8") == "left"
    assert right.read_text(encoding="utf-8") == "right"


def test_existing_unplanned_target_is_never_overwritten(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    target = tmp_path / "target.txt"
    source.write_text("source", encoding="utf-8")
    target.write_text("target", encoding="utf-8")
    engine = FileOperationEngine(lambda journal: None)
    with pytest.raises(FileExistsError):
        engine.execute_renames(
            "plan", [RenameRequest(source, target, SnapshotToken.capture(source))]
        )
    assert source.read_text(encoding="utf-8") == "source"
    assert target.read_text(encoding="utf-8") == "target"


def test_stale_source_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    target = tmp_path / "target.txt"
    source.write_text("before", encoding="utf-8")
    snapshot = SnapshotToken.capture(source)
    source.write_text("after and changed", encoding="utf-8")
    with pytest.raises(OSError, match="Stale source"):
        FileOperationEngine(lambda journal: None).execute_renames(
            "plan", [RenameRequest(source, target, snapshot)]
        )


def test_folder_creation_is_contained_and_journaled(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    journals = []
    engine = FileOperationEngine(journals.append)
    engine.execute_folder_creates("plan", [FolderCreateRequest(root / "Personal", root)])
    assert (root / "Personal").is_dir()
    assert journals[-1].state == "completed"


def test_same_volume_move_preserves_name_and_is_verified(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    destination = tmp_path / "destination"
    source_root.mkdir()
    destination.mkdir()
    source = source_root / "document.pdf"
    target = destination / source.name
    source.write_bytes(b"synthetic")
    journals = []
    engine = FileOperationEngine(journals.append)
    completed = engine.execute_moves(
        "plan", [move_request(source, target, source_root, destination)]
    )
    assert target.read_bytes() == b"synthetic"
    assert not source.exists()
    assert journals[-1].state == "completed"
    engine.execute_undo("undo_plan", completed)
    assert source.read_bytes() == b"synthetic"
    assert not target.exists()


def test_cross_volume_move_quarantines_verified_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "source"
    destination = tmp_path / "destination"
    source_root.mkdir()
    destination.mkdir()
    source = source_root / "statement.pdf"
    target = destination / source.name
    source.write_bytes(b"verified content")
    monkeypatch.setattr(operations_module, "_same_volume", lambda source, target: False)
    journals = []
    engine = FileOperationEngine(journals.append)
    completed = engine.execute_moves(
        "plan", [move_request(source, target, source_root, destination)]
    )
    assert target.read_bytes() == b"verified content"
    assert not source.exists()
    quarantined = list((source_root / ".AIOrganizer-Quarantine").rglob("statement.pdf"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == b"verified content"
    assert journals[-1].state == "completed"
    engine.execute_undo("undo_plan", completed)
    assert source.read_bytes() == b"verified content"
    assert not target.exists()


def test_cross_volume_hash_failure_keeps_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "source"
    destination = tmp_path / "destination"
    source_root.mkdir()
    destination.mkdir()
    source = source_root / "data.bin"
    target = destination / source.name
    source.write_bytes(b"original")
    monkeypatch.setattr(operations_module, "_same_volume", lambda source, target: False)

    def corrupt_copy(source: Path, target: Path) -> None:
        target.write_bytes(b"corrupt")

    monkeypatch.setattr(operations_module, "_copy", corrupt_copy)
    journals = []
    with pytest.raises(OSError, match="hash verification"):
        FileOperationEngine(journals.append).execute_moves(
            "plan", [move_request(source, target, source_root, destination)]
        )
    assert source.read_bytes() == b"original"
    assert not target.exists()
    assert journals[-1].state == "rolled_back"


def test_move_target_cannot_escape_destination_root(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    destination = tmp_path / "destination"
    outside = tmp_path / "outside"
    source_root.mkdir()
    destination.mkdir()
    outside.mkdir()
    source = source_root / "data.bin"
    source.write_bytes(b"original")
    with pytest.raises(PermissionError, match="target escapes"):
        FileOperationEngine(lambda journal: None).execute_moves(
            "plan",
            [move_request(source, outside / source.name, source_root, destination)],
        )


def test_wrong_volume_identity_stops_move(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    destination = tmp_path / "destination"
    source_root.mkdir()
    destination.mkdir()
    source = source_root / "data.bin"
    source.write_bytes(b"original")
    request = move_request(source, destination / source.name, source_root, destination)
    request = MoveRequest(
        request.source,
        request.target,
        request.snapshot,
        request.source_root,
        request.destination_root,
        "wrong-device",
        request.destination_volume_id,
    )
    with pytest.raises(OSError, match="Source volume identity changed"):
        FileOperationEngine(lambda journal: None).execute_moves("plan", [request])


def test_project_snapshot_detects_internal_change(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    module = project / "module.py"
    module.write_text("before", encoding="utf-8")
    snapshot = SnapshotToken.capture(project)
    module.write_text("after with changed length", encoding="utf-8")
    assert snapshot.validate()
