from __future__ import annotations

from ai_organizer.domain.moves import MoveCandidate, ProjectedMoveValidator


def candidate(item_id: str, folder: str = "Archive", filename: str = "report.pdf"):
    return MoveCandidate(item_id, "source", "destination", filename, folder, filename)


def validate(candidates: list[MoveCandidate], **overrides):
    arguments = {
        "existing_paths": {"destination": set()},
        "projected_folders": {"destination": {"Archive"}},
        "protected_project_paths": {"destination": set()},
        "case_sensitive_roots": {"destination": False},
    }
    arguments.update(overrides)
    return ProjectedMoveValidator().validate(candidates, **arguments)


def test_duplicate_and_existing_targets_are_blocked() -> None:
    duplicate = validate([candidate("one"), candidate("two", filename="REPORT.pdf")])
    assert all(row.status == "blocked" for row in duplicate.values())
    assert any("Multiple selected" in issue for issue in duplicate["one"].issues)

    existing = validate(
        [candidate("one")],
        existing_paths={"destination": {"archive/REPORT.pdf"}},
    )
    assert any("already exists" in issue for issue in existing["one"].issues)


def test_missing_or_absolute_destination_folder_is_blocked() -> None:
    missing = validate([candidate("one", "Missing")])
    assert any("absent" in issue for issue in missing["one"].issues)
    absolute = validate([candidate("one", "C:/Windows")])
    assert any("relative" in issue for issue in absolute["one"].issues)


def test_project_boundaries_block_sources_and_destinations() -> None:
    source = candidate("source")
    source = MoveCandidate(
        source.item_id,
        source.source_root_id,
        source.destination_root_id,
        source.source_relative_path,
        source.destination_folder,
        source.filename,
        inside_protected_project=True,
    )
    rows = validate([source])
    assert any("from inside" in issue for issue in rows["source"].issues)

    target = validate(
        [candidate("target", "Code/project")],
        projected_folders={"destination": {"Code", "Code/project"}},
        protected_project_paths={"destination": {"Code"}},
    )
    assert any("target inside" in issue for issue in target["target"].issues)


def test_arbitrary_directory_move_is_blocked_but_project_root_is_atomic() -> None:
    folder = candidate("folder", filename="folder")
    folder = MoveCandidate(
        folder.item_id,
        folder.source_root_id,
        folder.destination_root_id,
        folder.source_relative_path,
        folder.destination_folder,
        folder.filename,
        is_directory=True,
    )
    assert any("Arbitrary" in issue for issue in validate([folder])["folder"].issues)

    project = MoveCandidate(
        "project", "source", "destination", "repo", "Archive", "repo", True, True
    )
    assert validate([project])["project"].status == "ready"
