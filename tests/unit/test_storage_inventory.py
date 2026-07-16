from __future__ import annotations

from pathlib import Path

import pytest

from ai_organizer.adapters import storage_inventory as storage_module
from ai_organizer.adapters.storage_inventory import StorageInventory
from ai_organizer.application.inventory_query import InventoryQueryService


def test_volume_capacity_source_coverage_and_bounded_directory_listing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    volume = tmp_path / "volume"
    volume.mkdir()
    (volume / "Documents").mkdir()
    (volume / "Downloads").mkdir()
    (volume / ".hidden").mkdir()
    (volume / "readme.txt").write_text("metadata only", encoding="utf-8")
    monkeypatch.setattr(
        storage_module,
        "_platform_volumes",
        lambda: [{"mount_point": str(volume), "kind": "fixed", "label": "Data"}],
    )
    service = StorageInventory()

    volumes = service.list_volumes([{"id": "documents", "path": str(volume / "Documents")}])
    result = service.list_directory(volumes[0]["volume_id"], limit=2)

    assert volumes[0]["total_bytes"] > 0
    assert volumes[0]["free_bytes"] >= 0
    assert volumes[0]["configured_root_ids"] == ["documents"]
    assert result["total"] == 3
    assert result["has_more"] is True
    assert result["content_read"] is False
    assert all(value["name"] != ".hidden" for value in result["entries"])
    with pytest.raises(ValueError, match="relative"):
        service.list_directory(volumes[0]["volume_id"], "../escape")


def test_inventory_query_exposes_storage_tools_with_source_coverage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        storage_module,
        "_platform_volumes",
        lambda: [{"mount_point": str(tmp_path), "kind": "fixed", "label": "Test"}],
    )
    query = InventoryQueryService(
        [], [{"id": "root", "path": str(tmp_path / "source"), "name": "Source"}]
    )

    result = query.storage_volumes()

    assert result["total"] == 1
    assert result["uncovered_volume_count"] == 0
    assert result["volumes"][0]["configured_source_count"] == 1
