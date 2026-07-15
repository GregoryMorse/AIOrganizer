from __future__ import annotations

from pathlib import Path

from ai_organizer.adapters.persistence import WorkspaceStore
from ai_organizer.domain.semantic import SemanticRecord, SoftwarePackage, software_package_id


def test_update_hint_survives_software_version_change(tmp_path: Path) -> None:
    store = WorkspaceStore.create(tmp_path / "semantic.aioworkspace", "Semantic")
    package_id = software_package_id("Example App", "Example Corp")
    first = SoftwarePackage(package_id, "Example App", "Example Corp", "1.0", "test")
    second = SoftwarePackage(package_id, "Example App", "Example Corp", "2.0", "test")
    store.save_software_inventory([first])
    store.save_semantic_record(
        SemanticRecord(
            "software",
            package_id,
            "update_hint",
            {"official_url": "https://example.test/download"},
            source_fingerprint=first.identity_fingerprint,
        )
    )

    store.save_software_inventory([second])
    changed = store.mark_semantic_stale("software", package_id, second.identity_fingerprint)

    assert first.version_fingerprint != second.version_fingerprint
    assert changed == 0
    assert store.get_semantic_record("software", package_id, "update_hint")["status"] == "current"
    store.close()


def test_semantic_record_is_marked_stale_not_deleted(tmp_path: Path) -> None:
    store = WorkspaceStore.create(tmp_path / "stale.aioworkspace", "Stale")
    store.save_semantic_record(
        SemanticRecord("file", "root:notes.txt", "meaning", {"topic": "notes"}, "old")
    )

    assert store.mark_semantic_stale("file", "root:notes.txt", "new") == 1

    record = store.get_semantic_record("file", "root:notes.txt", "meaning")
    assert record["status"] == "stale"
    assert record["facts"] == {"topic": "notes"}
    store.close()


def test_version_change_stales_assessment_but_preserves_update_hint(tmp_path: Path) -> None:
    store = WorkspaceStore.create(tmp_path / "versions.aioworkspace", "Versions")
    package_id = software_package_id("Example App", "Example Corp")
    first = SoftwarePackage(package_id, "Example App", "Example Corp", "1.0", "test")
    second = SoftwarePackage(package_id, "Example App", "Example Corp", "2.0", "test")
    store.save_semantic_record(
        SemanticRecord(
            "software", package_id, "update_assessment", {"latest_version": "2.0"}, first.version_fingerprint
        )
    )
    store.save_semantic_record(
        SemanticRecord(
            "software", package_id, "update_hint", {"official_page_url": "https://example.test"}, first.identity_fingerprint
        )
    )

    changed = store.mark_semantic_stale(
        "software", package_id, second.version_fingerprint, namespace="update_assessment"
    )

    assert changed == 1
    assert store.get_semantic_record("software", package_id, "update_assessment")["status"] == "stale"
    assert store.get_semantic_record("software", package_id, "update_hint")["status"] == "current"
    store.close()
