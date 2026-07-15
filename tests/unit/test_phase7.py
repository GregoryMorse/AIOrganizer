from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_organizer.adapters.persistence import WorkspaceStore
from ai_organizer.application.export_service import (
    DIAGNOSTIC_FORMAT,
    EXPORT_FORMAT,
    WorkspaceExportService,
    verify_export_bundle,
)
from ai_organizer.application.outlook_handoff import OutlookHandoffService
from ai_organizer.application.plugin_contracts import load_provider_manifest
from ai_organizer.domain.models import SourceRoot


def test_backup_and_exports_are_atomic_verifiable_and_privacy_separated(tmp_path: Path) -> None:
    store = WorkspaceStore.create(tmp_path / "source.aioworkspace", "Private workspace")
    source_path = tmp_path / "Private Documents"
    source_path.mkdir()
    store.save_source(SourceRoot(source_path, "Private Documents"))
    store.activity("test", "Opened password=hunter2 at private-name.pdf")
    service = WorkspaceExportService(store)

    backup = service.backup(tmp_path / "backup")
    review = service.export_review_bundle(tmp_path / "review")
    diagnostic = service.export_diagnostic_bundle(tmp_path / "diagnostic")

    assert backup.suffix == ".aioworkspace"
    assert verify_export_bundle(review)["format"] == EXPORT_FORMAT
    assert verify_export_bundle(diagnostic)["format"] == DIAGNOSTIC_FORMAT
    with zipfile.ZipFile(review) as archive:
        for name in archive.namelist():
            json.loads(archive.read(name))
        assert json.loads(archive.read("sources.json"))[0]["path"] == str(source_path)
        assert "hunter2" not in archive.read("activity.json").decode()
    with zipfile.ZipFile(diagnostic) as archive:
        combined = b"".join(archive.read(name) for name in archive.namelist())
        assert b"Private Documents" not in combined
        assert b"private-name.pdf" not in combined
        assert b"hunter2" not in combined
    assert not list(tmp_path.glob(".*.partial"))
    store.close()


def test_outlook_handoff_is_metadata_only_bounded_and_sanitized(tmp_path: Path) -> None:
    store = WorkspaceStore.create(tmp_path / "handoff.aioworkspace", "Handoff")
    payload = {
        "schema": "aiorganizer.outlook-selection/v1",
        "exportedAt": "2026-07-15T12:00:00Z",
        "source": "office-js-outlook-taskpane",
        "item": {
            "item_id": "opaque-outlook-id",
            "item_type": "message",
            "subject": "Reset code: 123456 https://example.invalid/reset?token=secret",
            "sender": {"name": "Example", "address": "security@example.com"},
            "received_at": "2026-07-15T11:00:00Z",
            "attachments": [
                {
                    "id": "attachment",
                    "name": "Statement-2026-07.pdf",
                    "mime_type": "application/pdf",
                    "size": 100,
                    "is_inline": False,
                }
            ],
        },
    }
    path = tmp_path / "handoff.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    handoff_id = OutlookHandoffService(store).import_file(path)
    record = store.get_semantic_record("email", handoff_id, "outlook_handoff_v1")

    assert record is not None
    facts = record["facts"]
    assert facts["body_included"] is False
    assert facts["mailbox_write_authority"] is False
    assert "123456" not in facts["item"]["subject"]
    assert "https://" not in facts["item"]["subject"]
    assert facts["item"]["attachments"][0]["name"] == "Statement-2026-07.pdf"
    store.close()


def test_outlook_handoff_rejects_body_or_unknown_authority_fields(tmp_path: Path) -> None:
    store = WorkspaceStore.create(tmp_path / "rejected.aioworkspace", "Rejected")
    path = tmp_path / "unsafe.json"
    path.write_text(
        json.dumps(
            {
                "schema": "aiorganizer.outlook-selection/v1",
                "exportedAt": "2026-07-15T12:00:00Z",
                "source": "office-js-outlook-taskpane",
                "item": {
                    "item_id": "id",
                    "item_type": "message",
                    "body": "This field is forbidden",
                },
                "apply": True,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        OutlookHandoffService(store).import_file(path)
    assert store.list_semantic_records("email", "outlook_handoff_v1") == []
    store.close()


def test_provider_manifest_cannot_claim_mutation_authority(tmp_path: Path) -> None:
    path = tmp_path / "provider.json"
    manifest = {
        "schema": "aiorganizer.provider-plugin/v1",
        "plugin_id": "example.provider",
        "name": "Example provider",
        "version": "1.0.0",
        "entry_point": "example_provider:Provider",
        "execution": "in_process",
        "network_access": True,
        "cloud_provider": True,
        "content_classes": ["metadata"],
        "capabilities": ["estimate", "analyze"],
        "mutation_authority": False,
    }
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert load_provider_manifest(path).plugin_id == "example.provider"
    manifest["mutation_authority"] = True
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_provider_manifest(path)


def test_outlook_companion_has_read_item_only_and_no_body_api() -> None:
    root = Path(__file__).parents[2] / "outlook-addin"
    manifest = (root / "manifest.xml").read_text(encoding="utf-8")
    script = (root / "taskpane.js").read_text(encoding="utf-8")
    assert "<Permissions>ReadItem</Permissions>" in manifest
    assert "ReadWriteMailbox" not in manifest
    assert "ItemSend" not in manifest
    assert "LaunchEvent" not in manifest
    assert ".body" not in script
    assert "getAttachmentContentAsync" not in script
    assert "makeEwsRequestAsync" not in script
