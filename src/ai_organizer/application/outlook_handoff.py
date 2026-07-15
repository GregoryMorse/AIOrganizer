from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ai_organizer.adapters.persistence import WorkspaceStore
from ai_organizer.domain.email import sanitized_preview
from ai_organizer.domain.semantic import SemanticRecord, semantic_fingerprint


class HandoffSender(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(default="", max_length=320)
    address: str = Field(default="", max_length=320)


class HandoffAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1, max_length=2_000)
    name: str = Field(default="", max_length=512)
    mime_type: str = Field(default="", max_length=255)
    size: int = Field(default=0, ge=0, le=10_000_000_000)
    is_inline: bool = False


class HandoffItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    item_id: str = Field(min_length=1, max_length=4_000)
    item_type: Literal["message", "appointment"]
    subject: str = Field(default="", max_length=1_000)
    sender: HandoffSender = Field(default_factory=HandoffSender)
    received_at: str = Field(default="", max_length=100)
    attachments: list[HandoffAttachment] = Field(default_factory=list, max_length=250)


class OutlookHandoff(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_name: Literal["aiorganizer.outlook-selection/v1"] = Field(alias="schema")
    exported_at: str = Field(alias="exportedAt", max_length=100)
    source: Literal["office-js-outlook-taskpane"]
    item: HandoffItem


class OutlookHandoffService:
    def __init__(self, store: WorkspaceStore) -> None:
        self.store = store

    def import_file(self, path: Path) -> str:
        if path.stat().st_size > 1_000_000:
            raise ValueError("Outlook handoff files are limited to 1 MB")
        raw = json.loads(path.read_text(encoding="utf-8"))
        handoff = OutlookHandoff.model_validate(raw)
        facts = _sanitized_facts(handoff)
        key = f"outlook_handoff_{semantic_fingerprint({'item': handoff.item.item_id, 'exported': handoff.exported_at})[:24]}"
        self.store.save_semantic_record(
            SemanticRecord(
                "email",
                key,
                "outlook_handoff_v1",
                facts,
                semantic_fingerprint(facts),
                1.0,
                "user_imported_office_js_metadata",
            )
        )
        self.store.activity(
            "outlook.handoff_imported",
            "Imported one Outlook selection metadata handoff",
            {"handoff_id": key, "attachments": len(handoff.item.attachments)},
        )
        return key

    def list_handoffs(self) -> list[dict[str, Any]]:
        return self.store.list_semantic_records("email", "outlook_handoff_v1")


def _sanitized_facts(handoff: OutlookHandoff) -> dict[str, Any]:
    return {
        "schema": handoff.schema_name,
        "exported_at": handoff.exported_at,
        "source": handoff.source,
        "item": {
            "item_id": handoff.item.item_id,
            "item_type": handoff.item.item_type,
            "subject": sanitized_preview(handoff.item.subject, 300),
            "sender": {
                "name": sanitized_preview(handoff.item.sender.name, 180),
                "address": handoff.item.sender.address,
            },
            "received_at": handoff.item.received_at,
            "attachments": [
                {
                    "id": value.id,
                    "name": sanitized_preview(value.name, 255),
                    "mime_type": value.mime_type,
                    "size": value.size,
                    "is_inline": value.is_inline,
                }
                for value in handoff.item.attachments
            ],
        },
        "body_included": False,
        "mailbox_write_authority": False,
    }
