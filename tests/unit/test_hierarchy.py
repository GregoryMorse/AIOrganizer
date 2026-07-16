from __future__ import annotations

from ai_organizer.domain.hierarchy import (
    HierarchyAction,
    HierarchyChange,
    UnionHierarchyPlanner,
)


def project(current: set[str], changes: list[HierarchyChange], *, case_sensitive: bool = True):
    return UnionHierarchyPlanner().project(
        "root",
        current,
        changes,
        case_sensitive=case_sensitive,
        windows_rules=False,
    )


def test_parent_rename_projects_descendants_in_aligned_rows() -> None:
    projection = project(
        {"Work", "Work/Reports", "Personal"},
        [HierarchyChange("Projects", "Work")],
    )
    rows = {row.current_path: row for row in projection.rows}
    assert projection.ready
    assert rows["Work"].projected_path == "Projects"
    assert rows["Work"].action == HierarchyAction.RENAME
    assert rows["Work/Reports"].projected_path == "Projects/Reports"
    assert rows["Work/Reports"].action == HierarchyAction.DESCENDANT
    assert rows["Personal"].projected_path == "Personal"


def test_nested_creates_are_ghost_rows_and_parent_first_ready() -> None:
    projection = project(
        set(),
        [HierarchyChange("Archive/2026"), HierarchyChange("Archive")],
    )
    assert projection.ready
    assert [row.projected_path for row in projection.rows] == ["Archive", "Archive/2026"]
    assert all(row.ghost and row.action == HierarchyAction.CREATE for row in projection.rows)


def test_case_insensitive_projected_collision_is_blocked() -> None:
    projection = project(
        {"Inbox", "Archive"},
        [HierarchyChange("archive", "Inbox")],
        case_sensitive=False,
    )
    assert not projection.ready
    assert any("collision" in issue.casefold() for issue in projection.issues)


def test_folder_rename_cannot_reparent_or_overlap_another_rename() -> None:
    reparent = project(
        {"Work", "Work/Reports"},
        [HierarchyChange("Archive/Work", "Work")],
    )
    assert not reparent.ready
    assert any("may not reparent" in issue for issue in reparent.issues)

    nested = project(
        {"Work", "Work/Reports"},
        [
            HierarchyChange("Projects", "Work"),
            HierarchyChange("Work/Summaries", "Work/Reports"),
        ],
    )
    assert not nested.ready
    assert any("Nested folder renames" in issue for issue in nested.issues)


def test_windows_reserved_folder_name_is_blocked() -> None:
    projection = UnionHierarchyPlanner().project(
        "root",
        set(),
        [HierarchyChange("CON")],
        case_sensitive=False,
        windows_rules=True,
    )
    assert not projection.ready
    assert any("reserved" in issue for issue in projection.issues)
