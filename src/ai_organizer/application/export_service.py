from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_organizer.adapters.persistence import WorkspaceStore
from ai_organizer.domain.prompts import redact_sensitive

EXPORT_FORMAT = "aiorganizer.review-export/v1"
DIAGNOSTIC_FORMAT = "aiorganizer.diagnostic-export/v1"


class WorkspaceExportService:
    def __init__(self, store: WorkspaceStore) -> None:
        self.store = store

    def backup(self, destination: Path) -> Path:
        target = destination.with_suffix(".aioworkspace")
        if target.resolve(strict=False) == self.store.path:
            raise ValueError("Choose a different path for the workspace backup")
        self.store.save_as(target)
        return target

    def export_review_bundle(self, destination: Path) -> Path:
        target = destination.with_suffix(".zip")
        documents = {
            "workspace.json": {
                "workspace_id": self.store.workspace_id,
                "name": self.store.get_meta("name"),
                "exported_at": _now(),
                "privacy_notice": (
                    "This user-requested review export can contain source paths, filenames, "
                    "proposal rationales, and operation history. It contains no provider or Microsoft tokens."
                ),
            },
            "sources.json": self.store.list_source_payloads(),
            "categories.json": self.store.list_category_payloads(),
            "tags.json": self.store.list_tag_definition_payloads(),
            "tag-assignments.json": self.store.list_tag_assignment_payloads(),
            "assignments.json": self.store.list_assignment_payloads(),
            "guidance.json": self._rows(
                "SELECT id,profile_id,kind,text,created_at FROM prompt_revisions ORDER BY created_at"
            ),
            "actions.json": self.store.list_action_payloads(),
            "recurrences.json": self.store.list_recurrence_series(reviewed_only=False),
            "proposal_sets.json": self._payload_rows("proposal_sets"),
            "email_proposals.json": self._payload_rows("email_proposals"),
            "operation_journals.json": self._payload_rows("journals"),
            "activity.json": [dict(row) for row in self.store.list_activity(limit=10_000)],
        }
        return _write_bundle(target, EXPORT_FORMAT, documents)

    def export_diagnostic_bundle(self, destination: Path) -> Path:
        target = destination.with_suffix(".zip")
        account_count = int(
            self.store.connection.execute("SELECT COUNT(*) FROM email_accounts").fetchone()[0]
        )
        documents = {
            "diagnostics.json": {
                "workspace": {
                    "workspace_id": self.store.workspace_id,
                    "schema_version": int(
                        self.store.connection.execute("PRAGMA user_version").fetchone()[0]
                    ),
                    "source_count": len(self.store.list_source_payloads()),
                    "category_count": len(self.store.list_category_payloads()),
                    "tag_count": len(self.store.list_tag_definition_payloads()),
                    "inventory_item_count": len(self.store.list_items()),
                    "email_account_count": account_count,
                    "metadata_cache": self.store.metadata_cache_stats(),
                },
                "runtime": {
                    "python": sys.version.split()[0],
                    "platform": platform.platform(),
                    "implementation": platform.python_implementation(),
                    "development_source_run": os.getenv("AIORGANIZER_PACKAGED", "0") != "1",
                },
                "privacy_notice": (
                    "No document text, email metadata, filenames, source paths, credentials, "
                    "semantic facts, proposals, or operation payloads are included."
                ),
            },
            "activity.json": [
                {
                    "occurred_at": row["occurred_at"],
                    "kind": row["kind"],
                }
                for row in self.store.list_activity(limit=500)
            ],
        }
        return _write_bundle(target, DIAGNOSTIC_FORMAT, documents)

    def _payload_rows(self, table: str) -> list[dict[str, Any]]:
        allowed = {"proposal_sets", "email_proposals", "journals"}
        if table not in allowed:
            raise ValueError("Unsupported export table")
        rows = self.store.connection.execute(f"SELECT payload FROM {table} ORDER BY rowid")
        return [json.loads(row[0]) for row in rows]

    def _rows(self, query: str) -> list[dict[str, Any]]:
        cursor = self.store.connection.execute(query)
        return [dict(row) for row in cursor]


def _write_bundle(target: Path, bundle_format: str, documents: dict[str, Any]) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.partial")
    encoded: dict[str, bytes] = {}
    for name, value in documents.items():
        text = json.dumps(
            _redact_value(value), ensure_ascii=False, indent=2, sort_keys=True
        )
        encoded[name] = text.encode("utf-8")
    manifest = {
        "format": bundle_format,
        "created_at": _now(),
        "files": {
            name: {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
            for name, content in sorted(encoded.items())
        },
    }
    try:
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in sorted(encoded.items()):
                archive.writestr(name, content)
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
            )
        partial.replace(target)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return target


def verify_export_bundle(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        manifest: dict[str, Any] = json.loads(archive.read("manifest.json"))
        if manifest.get("format") not in {EXPORT_FORMAT, DIAGNOSTIC_FORMAT}:
            raise ValueError("Unsupported AIOrganizer export format")
        for name, expected in manifest.get("files", {}).items():
            content = archive.read(name)
            if hashlib.sha256(content).hexdigest() != expected.get("sha256"):
                raise ValueError(f"Export checksum mismatch: {name}")
    return manifest


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_sensitive(value)
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value
