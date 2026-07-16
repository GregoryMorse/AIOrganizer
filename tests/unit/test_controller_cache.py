from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ai_organizer.desktop.controller import WorkspaceController
from ai_organizer.domain.models import FolderRole, SourceRoot
from ai_organizer.domain.recurrence import Cadence, RecurrenceSeries, SeriesObservation


def test_action_revalidation_rejects_external_change(tmp_path: Path) -> None:
    source_path = tmp_path / "source"
    source_path.mkdir()
    document = source_path / "document.txt"
    document.write_text("before", encoding="utf-8")
    info = document.stat()
    controller = WorkspaceController()
    source = SourceRoot(source_path, "Source", id="root")
    controller.sources[source.id] = source
    controller.items = [
        {
            "id": "item",
            "root_id": "root",
            "relative_path": "document.txt",
            "size": info.st_size,
            "modified_ns": info.st_mtime_ns,
            "metadata": {"_cache": {"validated_by": "size+modified_ns"}},
        }
    ]
    document.write_text("changed outside the application", encoding="utf-8")

    with pytest.raises(RuntimeError, match="changed outside"):
        controller._validate_selected_inventory([{"item_id": "item"}])


def test_recurrence_observation_rebinds_after_rescan_and_missing_file_opens_gap(
    tmp_path: Path,
) -> None:
    controller = WorkspaceController()
    controller.create_workspace(tmp_path / "series.aioworkspace", "Series")
    assert controller.store is not None
    controller.items = [
        {
            "id": "new-inventory-id",
            "root_id": "root",
            "relative_path": "Acme Statement 2026-01.pdf",
            "size": 100,
            "modified_ns": 200,
        }
    ]
    controller.store.save_recurrence_series(
        RecurrenceSeries(
            "Acme",
            "Acme",
            "Statement",
            "",
            Cadence.MONTHLY,
            "2026-01-01",
            "2026-01-01",
            0,
            (
                SeriesObservation(
                    "old-inventory-id",
                    "2026-01-01",
                    0.9,
                    ("reviewed",),
                    "root",
                    "Acme Statement 2026-01.pdf",
                    "100:200",
                ),
            ),
            "fingerprint",
            id="series",
        )
    )
    present = controller.recurrence_gap_rows("series", as_of=date(2026, 3, 1))[0]
    assert str(present["status"]) == "present_verified"
    assert present["item_ids"] == ("new-inventory-id",)

    controller.items = []
    missing = controller.recurrence_gap_rows("series", as_of=date(2026, 3, 1))[0]
    assert str(missing["status"]) == "missing"
    controller.close()


def test_ai_context_is_persisted_per_working_view(tmp_path: Path) -> None:
    controller = WorkspaceController()
    controller.create_workspace(tmp_path / "context.aioworkspace", "Context")

    controller.set_ai_context("folder", "deepseek", "deepseek-v4-flash")
    controller.set_ai_context("updates", "deepseek", "deepseek-v4-pro")

    assert controller.ai_context("folder") == ("deepseek", "deepseek-v4-flash")
    assert controller.ai_context("updates") == ("deepseek", "deepseek-v4-pro")
    controller.close()


def test_assigning_source_root_policy_updates_parent_and_inherited_children(
    tmp_path: Path,
) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    controller = WorkspaceController()
    controller.create_workspace(tmp_path / "policy.aioworkspace", "Policy")
    source = controller.add_source(root, roles={FolderRole.INBOX})

    controller.assign_folder_policy(
        source.id,
        "",
        {"download-category"},
        {FolderRole.DOWNLOADS, FolderRole.DESTINATION},
    )

    assert controller.sources[source.id].category_ids == {"download-category"}
    assert controller.sources[source.id].roles == {
        FolderRole.INBOX,
        FolderRole.DOWNLOADS,
        FolderRole.DESTINATION,
    }
    payload = next(
        value for value in controller.store.list_source_payloads() if value["id"] == source.id
    )
    assert set(payload["roles"]) == {"inbox", "downloads", "destination"}
    controller.close()


def test_new_source_is_minimal_until_classification_is_approved(tmp_path: Path) -> None:
    root = tmp_path / "unclassified"
    root.mkdir()
    controller = WorkspaceController()
    controller.create_workspace(tmp_path / "minimal.aioworkspace", "Minimal")

    source = controller.add_source(root)

    assert source.roles == set()
    assert source.category_ids == set()
    assert source.tag_ids == set()
    assert not controller.source_is_classified(source.id)
    assert not controller.source_is_operational(source.id)

    assert controller.store is not None
    category_id = str(controller.store.list_category_payloads()[0]["id"])
    tag_id = str(controller.store.list_tag_definition_payloads()[0]["id"])
    controller.set_source_classification(
        source.id,
        {category_id},
        {tag_id},
        {FolderRole.ARCHIVE},
    )

    assert controller.source_is_classified(source.id)
    assert controller.source_is_operational(source.id)
    assert source.category_ids == {category_id}
    assert source.tag_ids == {tag_id}
    assert source.roles == {FolderRole.ARCHIVE}
    controller.close()


def test_excluded_source_is_classified_but_not_operational(tmp_path: Path) -> None:
    root = tmp_path / "excluded"
    root.mkdir()
    controller = WorkspaceController()
    controller.create_workspace(tmp_path / "excluded.aioworkspace", "Excluded")

    source = controller.add_source(root, roles={FolderRole.EXCLUDED})

    assert controller.source_is_classified(source.id)
    assert not controller.source_is_operational(source.id)
    controller.close()


def test_legacy_source_depth_override_does_not_limit_folder_plan_policy(tmp_path: Path) -> None:
    root = tmp_path / "legacy-depth"
    root.mkdir()
    controller = WorkspaceController()
    controller.create_workspace(tmp_path / "depth.aioworkspace", "Depth")

    source = controller.add_source(root, max_hierarchy_depth=1)

    assert controller.folder_depth_limit(source.id) == 3
    controller.close()
