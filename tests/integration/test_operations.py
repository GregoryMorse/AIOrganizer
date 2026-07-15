from __future__ import annotations

from pathlib import Path

import pytest

from ai_organizer.adapters.filesystem import (
    CleanupRequest,
    FileOperationEngine,
    FolderCreateRequest,
    Journal,
    MoveRequest,
    RenameRequest,
    SnapshotToken,
)
from ai_organizer.adapters.filesystem import operations as operations_module
from ai_organizer.adapters.persistence.workspace import WorkspaceStore


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


def test_interrupted_swap_recovers_after_store_restart(tmp_path: Path) -> None:
    workspace_path = tmp_path / "recovery.aioworkspace"
    store = WorkspaceStore.create(workspace_path, "Recovery")
    left = tmp_path / "left.txt"
    right = tmp_path / "right.txt"
    left.write_text("left", encoding="utf-8")
    right.write_text("right", encoding="utf-8")
    left_hash = SnapshotToken.capture(left).sha256
    right_hash = SnapshotToken.capture(right).sha256
    staging = tmp_path / ".aiorganizer-tmp-interrupted"
    left.rename(staging)
    right.rename(left)
    staging.rename(right)
    journal = Journal(
        "plan",
        state="executing",
        operations=[
            {
                "kind": "rename",
                "source": str(left),
                "target": str(right),
                "snapshot": left_hash,
                "state": "verified",
            },
            {
                "kind": "rename",
                "source": str(right),
                "target": str(left),
                "snapshot": right_hash,
                "state": "verified",
            },
        ],
    )
    store.save_journal(
        journal.id,
        journal.plan_id,
        journal.state,
        operations_module.journal_to_dict(journal),
    )
    store.close()

    reopened = WorkspaceStore(workspace_path)
    payload = reopened.incomplete_journals()[0]
    recovered = Journal(
        payload["plan_id"],
        id=payload["id"],
        state=payload["state"],
        operations=payload["operations"],
        updated_at=payload["updated_at"],
    )
    engine = FileOperationEngine(
        lambda value: reopened.save_journal(
            value.id,
            value.plan_id,
            value.state,
            operations_module.journal_to_dict(value),
        )
    )
    engine.recover_incomplete(recovered)

    assert left.read_text(encoding="utf-8") == "left"
    assert right.read_text(encoding="utf-8") == "right"
    assert reopened.incomplete_journals() == []
    reopened.close()


def test_recovery_can_resume_from_its_own_staging_path(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    target = tmp_path / "target.txt"
    recovery_temp = tmp_path / ".aiorganizer-recover-interrupted"
    recovery_temp.write_text("content", encoding="utf-8")
    journal = Journal(
        "plan",
        state="recovery_required",
        operations=[
            {
                "kind": "move",
                "source": str(source),
                "target": str(target),
                "snapshot": SnapshotToken.capture(recovery_temp).sha256,
                "recovery_temp": str(recovery_temp),
                "state": "recovery_staged",
            }
        ],
    )
    FileOperationEngine(lambda value: None).recover_incomplete(journal)
    assert source.read_text(encoding="utf-8") == "content"
    assert journal.state == "rolled_back"


def test_multi_move_plan_remains_undoable_after_store_restart(tmp_path: Path) -> None:
    workspace_path = tmp_path / "undo.aioworkspace"
    store = WorkspaceStore.create(workspace_path, "Undo")
    source_root = tmp_path / "source"
    destination = tmp_path / "destination"
    source_root.mkdir()
    destination.mkdir()
    first = source_root / "first.txt"
    second = source_root / "second.txt"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")

    def persist(value: Journal) -> None:
        store.save_journal(
            value.id,
            value.plan_id,
            value.state,
            operations_module.journal_to_dict(value),
        )

    FileOperationEngine(persist).execute_moves(
        "plan",
        [
            move_request(first, destination / first.name, source_root, destination),
            move_request(second, destination / second.name, source_root, destination),
        ],
    )
    store.close()

    reopened = WorkspaceStore(workspace_path)
    payload = reopened.latest_completed_journal()
    assert payload is not None
    completed = Journal(
        payload["plan_id"],
        id=payload["id"],
        state=payload["state"],
        operations=payload["operations"],
        updated_at=payload["updated_at"],
    )
    FileOperationEngine(
        lambda value: reopened.save_journal(
            value.id,
            value.plan_id,
            value.state,
            operations_module.journal_to_dict(value),
        )
    ).execute_undo("undo", completed)

    assert first.read_text(encoding="utf-8") == "first"
    assert second.read_text(encoding="utf-8") == "second"
    assert not (destination / first.name).exists()
    assert not (destination / second.name).exists()
    reopened.close()


def test_locked_rename_failure_rolls_back_staged_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    left = tmp_path / "left.txt"
    right = tmp_path / "right.txt"
    left.write_text("left", encoding="utf-8")
    right.write_text("right", encoding="utf-8")
    real_rename = operations_module.os.rename
    calls = 0

    def fail_once(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("simulated file lock")
        real_rename(source, target)

    monkeypatch.setattr(operations_module.os, "rename", fail_once)
    journals = []
    with pytest.raises(PermissionError, match="file lock"):
        FileOperationEngine(journals.append).execute_renames(
            "plan",
            [
                RenameRequest(left, left.with_name("new-left.txt"), SnapshotToken.capture(left)),
                RenameRequest(
                    right, right.with_name("new-right.txt"), SnapshotToken.capture(right)
                ),
            ],
        )
    assert left.read_text(encoding="utf-8") == "left"
    assert right.read_text(encoding="utf-8") == "right"
    assert journals[-1].state == "rolled_back"


def test_cleanup_moves_to_restorable_quarantine(tmp_path: Path) -> None:
    root = tmp_path / "root"
    artifact = root / "project" / "target"
    artifact.mkdir(parents=True)
    (artifact / "output.bin").write_bytes(b"generated")
    journals = []
    engine = FileOperationEngine(journals.append)
    completed = engine.execute_cleanup(
        "cleanup_plan",
        [
            CleanupRequest(
                artifact,
                root,
                SnapshotToken.capture(artifact),
                "build_artifact",
            )
        ],
    )
    quarantine = Path(str(completed.operations[0]["target"]))
    assert not artifact.exists()
    assert (quarantine / "output.bin").read_bytes() == b"generated"
    assert ".AIOrganizer-Cleanup-Quarantine" in quarantine.parts

    engine.execute_undo("restore", completed)
    assert (artifact / "output.bin").read_bytes() == b"generated"
    assert not quarantine.exists()


def test_cleanup_restore_never_overwrites_newer_path(tmp_path: Path) -> None:
    root = tmp_path / "root"
    artifact = root / "cache"
    artifact.mkdir(parents=True)
    completed = FileOperationEngine(lambda value: None).execute_cleanup(
        "cleanup_plan",
        [CleanupRequest(artifact, root, SnapshotToken.capture(artifact), "build_artifact")],
    )
    artifact.mkdir()
    (artifact / "new.txt").write_text("new", encoding="utf-8")
    with pytest.raises(OSError, match="preconditions changed"):
        FileOperationEngine(lambda value: None).execute_undo("restore", completed)
    assert (artifact / "new.txt").read_text(encoding="utf-8") == "new"
    assert Path(str(completed.operations[0]["target"])).exists()


def test_interrupted_cleanup_is_recovered_to_original_path(tmp_path: Path) -> None:
    root = tmp_path / "root"
    source = root / "cache"
    source.mkdir(parents=True)
    (source / "generated.bin").write_bytes(b"generated")
    snapshot = SnapshotToken.capture(source)
    target = root / ".AIOrganizer-Cleanup-Quarantine" / "journal" / "cache"
    target.parent.mkdir(parents=True)
    source.rename(target)
    journal = Journal(
        "cleanup_plan",
        state="executing",
        operations=[
            {
                "kind": "cleanup",
                "source": str(source),
                "target": str(target),
                "snapshot": snapshot.sha256,
                "state": "verified",
            }
        ],
    )

    FileOperationEngine(lambda value: None).recover_incomplete(journal)

    assert (source / "generated.bin").read_bytes() == b"generated"
    assert not target.exists()
    assert journal.state == "rolled_back"


def test_cross_volume_copy_failure_removes_partial_and_keeps_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "source"
    destination = tmp_path / "destination"
    source_root.mkdir()
    destination.mkdir()
    source = source_root / "large.bin"
    target = destination / source.name
    source.write_bytes(b"original")
    monkeypatch.setattr(operations_module, "_same_volume", lambda source, target: False)

    def disk_full(source_path: Path, partial: Path) -> None:
        partial.write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(operations_module, "_copy", disk_full)
    journals = []
    with pytest.raises(OSError, match="disk full"):
        FileOperationEngine(journals.append).execute_moves(
            "plan", [move_request(source, target, source_root, destination)]
        )
    assert source.read_bytes() == b"original"
    assert not target.exists()
    assert not list(destination.glob(".aiorganizer-partial-*"))
    assert journals[-1].state == "rolled_back"


def test_cross_volume_move_rejects_insufficient_space_before_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "source"
    destination = tmp_path / "destination"
    source_root.mkdir()
    destination.mkdir()
    source = source_root / "large.bin"
    source.write_bytes(b"content")
    monkeypatch.setattr(operations_module, "_same_volume", lambda source, target: False)
    monkeypatch.setattr(
        operations_module.shutil,
        "disk_usage",
        lambda path: type("Usage", (), {"free": 0})(),
    )
    with pytest.raises(OSError, match="Insufficient destination space"):
        FileOperationEngine(lambda value: None).execute_moves(
            "plan",
            [move_request(source, destination / source.name, source_root, destination)],
        )
    assert source.read_bytes() == b"content"
    assert not (destination / source.name).exists()
