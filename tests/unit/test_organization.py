from __future__ import annotations

from ai_organizer.adapters.persistence import WorkspaceStore
from ai_organizer.desktop.controller import WorkspaceController
from ai_organizer.domain.models import (
    CategoryAssignment,
    FolderRole,
    TagAssignment,
)
from ai_organizer.domain.organization import (
    FolderDepthPolicy,
    general_organization_profile,
    general_source_presets,
    recommend_folder_depth,
)


def test_general_profile_has_referentially_sound_multi_axis_vocabulary() -> None:
    profile = general_organization_profile()
    category_ids = {value.id for value in profile.categories}
    tag_ids = {value.id for value in profile.tags}

    assert len(category_ids) == len(profile.categories)
    assert len(tag_ids) == len(profile.tags)
    assert {value.facet.value for value in profile.tags} >= {
        "content",
        "lifecycle",
        "state",
        "origin",
        "technology",
        "audience",
    }
    assert all(
        value.parent_id is None or value.parent_id in category_ids
        for value in profile.categories
    )
    assert all(value.default_tag_ids <= tag_ids for value in profile.categories)
    assert any(value.suggest_as_folder for value in profile.categories)
    assert len(general_source_presets()) >= 9


def test_adaptive_depth_is_shallow_for_small_sources_and_bounded_for_massive_ones() -> None:
    policy = FolderDepthPolicy(2, 3, True)

    assert recommend_folder_depth(20, 4, policy) == 1
    assert recommend_folder_depth(2_000, 100, policy) == 2
    assert recommend_folder_depth(1_000_000, 100_000, policy) == 3
    assert recommend_folder_depth(1_000_000, 100_000, FolderDepthPolicy(1, 2, False)) == 1


def test_profile_install_is_idempotent_and_preserves_existing_workspace_policy(tmp_path) -> None:  # type: ignore[no-untyped-def]
    controller = WorkspaceController()
    controller.store = WorkspaceStore.create(tmp_path / "profile.aioworkspace", "Profile")

    first = controller.install_general_organization_profile()
    category_count = len(controller.store.list_category_payloads())
    tag_count = len(controller.store.list_tag_definition_payloads())
    second = controller.install_general_organization_profile()

    assert first[0] == category_count
    assert first[1] == tag_count
    assert second == (0, 0)
    assert controller.folder_depth_policy() == FolderDepthPolicy(2, 3, True)
    assert controller.latest_prompt_text("workspace:general")
    controller.close()


def test_tag_definitions_and_assignments_round_trip_in_workspace(tmp_path) -> None:  # type: ignore[no-untyped-def]
    controller = WorkspaceController()
    controller.store = WorkspaceStore.create(tmp_path / "tags.aioworkspace", "Tags")
    controller.install_general_organization_profile()
    tag = next(
        value
        for value in controller.store.list_tag_definition_payloads()
        if value["key"] == "migration-candidate"
    )
    assignment = TagAssignment("folder", "root:legacy", str(tag["id"]), source="user")
    controller.store.save_tag_assignment(assignment)
    controller.store.save_assignment(
        CategoryAssignment(
            tmp_path / "legacy",
            set(),
            {FolderRole.INBOX},
            tag_ids={str(tag["id"])},
        )
    )

    assert controller.store.list_tag_assignment_payloads("folder", "root:legacy")[0][
        "tag_id"
    ] == tag["id"]
    assert tag["id"] in controller.store.list_assignment_payloads()[0]["tag_ids"]
    controller.close()
