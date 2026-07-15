from __future__ import annotations

import fnmatch
import json
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from ai_organizer.domain.actions import ActionPreset, ActionRun, FindingSet
from ai_organizer.domain.email import (
    AccountSecurityEvidence,
    EmailAccount,
    EmailProposal,
    MailAttachmentSnapshot,
    MailFolderSnapshot,
    MailMessageSnapshot,
)
from ai_organizer.domain.evidence import SelectionScope
from ai_organizer.domain.models import (
    CategoryAssignment,
    CategoryDefinition,
    Evidence,
    ItemSnapshot,
    ProposalSet,
    SourceRoot,
    TagAssignment,
    TagDefinition,
    utc_now,
)
from ai_organizer.domain.prompts import PromptRevision, redact_sensitive
from ai_organizer.domain.recurrence import RecurrenceException, RecurrenceSeries
from ai_organizer.domain.semantic import SemanticRecord, SoftwarePackage

SCHEMA_VERSION = 11


class WorkspaceStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve(strict=False)
        self._ensure_local_extension()
        # Modal background jobs serialize workspace use but execute off the Qt UI thread.
        # SQLite itself is built in serialized mode; application workflows never overlap writes.
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self._migrate()

    @classmethod
    def create(cls, path: Path, name: str) -> WorkspaceStore:
        if path.suffix != ".aioworkspace":
            path = path.with_suffix(".aioworkspace")
        if path.exists():
            raise FileExistsError(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        store = cls(path)
        store.set_meta("workspace_id", f"workspace_{uuid4().hex}")
        store.set_meta("name", name)
        store.set_meta("created_at", utc_now())
        return store

    def _ensure_local_extension(self) -> None:
        if self.path.suffix != ".aioworkspace":
            raise ValueError("Workspace files must use the .aioworkspace extension")
        if str(self.path).startswith(("\\\\", "//")):
            raise ValueError("Workspace databases must be local because SQLite WAL is enabled")

    def _migrate(self) -> None:
        current = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise RuntimeError("Workspace was created by a newer AIOrganizer version")
        if current and current < SCHEMA_VERSION:
            backup = self.path.with_name(
                f"{self.path.name}.backup-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
            )
            shutil.copy2(self.path, backup)
        if current < 1:
            self.connection.executescript(
                """
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE sources(id TEXT PRIMARY KEY, payload TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE categories(id TEXT PRIMARY KEY, payload TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE assignments(id TEXT PRIMARY KEY, payload TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE prompt_revisions(id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, kind TEXT NOT NULL,
                    text TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE action_presets(id TEXT PRIMARY KEY, payload TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE snapshots(id TEXT PRIMARY KEY, root_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    status TEXT NOT NULL);
                CREATE TABLE items(id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, root_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(snapshot_id, root_id, relative_path));
                CREATE INDEX ix_items_snapshot ON items(snapshot_id);
                CREATE TABLE evidence(id TEXT PRIMARY KEY, item_id TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE proposal_sets(id TEXT PRIMARY KEY, kind TEXT NOT NULL, revision INTEGER NOT NULL,
                    payload TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE frozen_plans(id TEXT PRIMARY KEY, proposal_set_id TEXT NOT NULL,
                    state TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE journals(id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, state TEXT NOT NULL,
                    payload TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE activity(id INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at TEXT NOT NULL,
                    kind TEXT NOT NULL, summary TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE connector_sources(id TEXT PRIMARY KEY, kind TEXT NOT NULL, display_name TEXT NOT NULL,
                    payload TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0);
                PRAGMA user_version=1;
                """
            )
            self.connection.commit()
        if current < 2:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS action_runs(
                    id TEXT PRIMARY KEY, preset_id TEXT NOT NULL, payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS finding_sets(
                    id TEXT PRIMARY KEY, action_run_id TEXT NOT NULL, payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                PRAGMA user_version=2;
                """
            )
            self.connection.commit()
        if current < 3:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata_cache(
                    root_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(root_id, relative_path)
                );
                PRAGMA user_version=3;
                """
            )
            self.connection.commit()
        if current < 4:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS semantic_records(
                    entity_kind TEXT NOT NULL,
                    entity_key TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    PRIMARY KEY(entity_kind, entity_key, namespace)
                );
                CREATE INDEX IF NOT EXISTS ix_semantic_status ON semantic_records(status, namespace);
                CREATE TABLE IF NOT EXISTS software_inventory(
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    version_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    scanned_at TEXT NOT NULL
                );
                PRAGMA user_version=4;
                """
            )
            self.connection.commit()
        if current < 5:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS archive_members(
                    root_id TEXT NOT NULL,
                    archive_relative_path TEXT NOT NULL,
                    member_index INTEGER NOT NULL,
                    member_path TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(root_id, archive_relative_path, member_index)
                );
                CREATE INDEX IF NOT EXISTS ix_archive_members_archive
                    ON archive_members(root_id, archive_relative_path, member_path);
                PRAGMA user_version=5;
                """
            )
            self.connection.commit()
        if current < 6:
            rows = self.connection.execute(
                "SELECT root_id,relative_path,payload FROM metadata_cache"
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload"])
                if "size" in payload and "modified_ns" in payload:
                    self.connection.execute(
                        "UPDATE metadata_cache SET fingerprint=? "
                        "WHERE root_id=? AND relative_path=?",
                        (
                            f"{payload['size']}:{payload['modified_ns']}",
                            row["root_id"],
                            row["relative_path"],
                        ),
                    )
            self.connection.execute("PRAGMA user_version=6")
            self.connection.commit()
        if current < 7:
            self.connection.executescript(
                """
                DROP INDEX IF EXISTS ix_metadata_cache_expiry;
                CREATE TABLE metadata_cache_v7(
                    root_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(root_id, relative_path)
                );
                INSERT OR REPLACE INTO metadata_cache_v7(
                    root_id,relative_path,fingerprint,payload,updated_at
                ) SELECT root_id,relative_path,fingerprint,payload,updated_at FROM metadata_cache;
                DROP TABLE metadata_cache;
                ALTER TABLE metadata_cache_v7 RENAME TO metadata_cache;
                PRAGMA user_version=7;
                """
            )
            self.connection.commit()
        if current < 8:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS selection_scopes(
                    id TEXT PRIMARY KEY,
                    proposal_set_id TEXT,
                    item_ids TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_selection_scopes_active
                    ON selection_scopes(status, expires_at);
                CREATE TABLE IF NOT EXISTS mcp_idempotency(
                    idempotency_key TEXT PRIMARY KEY,
                    tool_name TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    response TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mcp_audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    client_identity TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    revision_before INTEGER,
                    revision_after INTEGER,
                    affected_item_ids TEXT NOT NULL,
                    result TEXT NOT NULL
                );
                PRAGMA user_version=8;
                """
            )
            self.connection.commit()
        if current < 9:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS recurrence_series(
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recurrence_exceptions(
                    series_id TEXT NOT NULL,
                    period_start TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(series_id, period_start),
                    FOREIGN KEY(series_id) REFERENCES recurrence_series(id) ON DELETE CASCADE
                );
                PRAGMA user_version=9;
                """
            )
            self.connection.commit()
        if current < 10:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS email_accounts(
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    active INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ix_email_one_active
                    ON email_accounts(active) WHERE active=1;
                CREATE TABLE IF NOT EXISTS mail_folders(
                    account_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(account_id,id)
                );
                CREATE TABLE IF NOT EXISTS mail_messages(
                    account_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    folder_id TEXT NOT NULL,
                    change_key TEXT NOT NULL,
                    etag TEXT NOT NULL,
                    removed INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(account_id,id)
                );
                CREATE INDEX IF NOT EXISTS ix_mail_messages_folder
                    ON mail_messages(account_id,folder_id,updated_at);
                CREATE TABLE IF NOT EXISTS mail_attachments(
                    account_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(account_id,message_id,id)
                );
                CREATE TABLE IF NOT EXISTS mail_delta_tokens(
                    account_id TEXT NOT NULL,
                    folder_id TEXT NOT NULL,
                    delta_link TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(account_id,folder_id)
                );
                CREATE TABLE IF NOT EXISTS email_proposals(
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_security_evidence(
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_account_security_evidence_account
                    ON account_security_evidence(account_id);
                PRAGMA user_version=10;
                """
            )
            self.connection.commit()
        if current < 11:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tag_definitions(
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    revision INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tag_assignments(
                    entity_kind TEXT NOT NULL,
                    entity_key TEXT NOT NULL,
                    tag_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    PRIMARY KEY(entity_kind,entity_key,tag_id)
                );
                CREATE INDEX IF NOT EXISTS ix_tag_assignments_tag
                    ON tag_assignments(tag_id,entity_kind);
                PRAGMA user_version=11;
                """
            )
            self.connection.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def close(self) -> None:
        self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.connection.close()

    def save_as(self, target: Path) -> None:
        if target.suffix != ".aioworkspace":
            target = target.with_suffix(".aioworkspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        destination = sqlite3.connect(target)
        try:
            self.connection.backup(destination)
        finally:
            destination.close()

    def set_meta(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.connection.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    @property
    def workspace_id(self) -> str:
        return self.get_meta("workspace_id")

    def save_source(self, source: SourceRoot) -> None:
        payload = _json(source)
        self.connection.execute(
            "INSERT INTO sources VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, revision=excluded.revision",
            (source.id, payload, source.policy_revision),
        )
        self.connection.commit()

    def list_source_payloads(self) -> list[dict[str, Any]]:
        return [
            json.loads(row[0]) for row in self.connection.execute("SELECT payload FROM sources")
        ]

    def save_connector_source(
        self,
        connector_id: str,
        kind: str,
        display_name: str,
        payload: dict[str, Any],
        enabled: bool,
    ) -> None:
        self.connection.execute(
            "INSERT INTO connector_sources VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,display_name=excluded.display_name,"
            "payload=excluded.payload,enabled=excluded.enabled",
            (connector_id, kind, display_name, json.dumps(payload), int(enabled)),
        )
        self.connection.commit()

    def list_connector_sources(self) -> list[dict[str, Any]]:
        return [
            {
                "id": row["id"],
                "kind": row["kind"],
                "display_name": row["display_name"],
                "payload": json.loads(row["payload"]),
                "enabled": bool(row["enabled"]),
            }
            for row in self.connection.execute(
                "SELECT * FROM connector_sources ORDER BY kind,display_name"
            )
        ]

    def save_category(self, category: CategoryDefinition) -> None:
        self._save_payload("categories", category.id, category, category.revision)

    def list_category_payloads(self) -> list[dict[str, Any]]:
        return [
            json.loads(row[0]) for row in self.connection.execute("SELECT payload FROM categories")
        ]

    def save_tag_definition(self, tag: TagDefinition) -> None:
        self._save_payload("tag_definitions", tag.id, tag, tag.revision)

    def list_tag_definition_payloads(self) -> list[dict[str, Any]]:
        return [
            json.loads(row[0])
            for row in self.connection.execute(
                "SELECT payload FROM tag_definitions ORDER BY id"
            )
        ]

    def save_tag_assignment(self, assignment: TagAssignment) -> None:
        self.connection.execute(
            "INSERT INTO tag_assignments VALUES(?,?,?,?,?) "
            "ON CONFLICT(entity_kind,entity_key,tag_id) DO UPDATE SET "
            "payload=excluded.payload,revision=excluded.revision",
            (
                assignment.entity_kind,
                assignment.entity_key,
                assignment.tag_id,
                _json(assignment),
                assignment.revision,
            ),
        )
        self.connection.commit()

    def list_tag_assignment_payloads(
        self, entity_kind: str | None = None, entity_key: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[str] = []
        if entity_kind:
            clauses.append("entity_kind=?")
            parameters.append(entity_kind)
        if entity_key:
            clauses.append("entity_key=?")
            parameters.append(entity_key)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return [
            json.loads(row[0])
            for row in self.connection.execute(
                "SELECT payload FROM tag_assignments" + where
                + " ORDER BY entity_kind,entity_key,tag_id",
                parameters,
            )
        ]

    def delete_tag_assignments(
        self, entity_kind: str, entity_keys: set[str], tag_ids: set[str]
    ) -> int:
        if not entity_keys or not tag_ids:
            return 0
        key_slots = ",".join("?" for _ in entity_keys)
        tag_slots = ",".join("?" for _ in tag_ids)
        cursor = self.connection.execute(
            f"DELETE FROM tag_assignments WHERE entity_kind=? "
            f"AND entity_key IN ({key_slots}) AND tag_id IN ({tag_slots})",
            [entity_kind, *sorted(entity_keys), *sorted(tag_ids)],
        )
        self.connection.commit()
        return int(cursor.rowcount)

    def save_assignment(self, assignment: CategoryAssignment) -> None:
        self._save_payload("assignments", assignment.id, assignment, assignment.revision)

    def list_assignment_payloads(self) -> list[dict[str, Any]]:
        return [
            json.loads(row[0]) for row in self.connection.execute("SELECT payload FROM assignments")
        ]

    def save_prompt_revision(self, revision: PromptRevision) -> None:
        self.connection.execute(
            "INSERT INTO prompt_revisions VALUES(?,?,?,?,?)",
            (
                revision.id,
                revision.profile_id,
                str(revision.kind),
                revision.text,
                revision.created_at,
            ),
        )
        self.connection.commit()

    def latest_prompt(self, profile_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM prompt_revisions WHERE profile_id=? ORDER BY created_at DESC LIMIT 1",
            (profile_id,),
        ).fetchone()

    def save_action(self, action: ActionPreset) -> None:
        self._save_payload("action_presets", action.id, action, action.revision)

    def list_action_payloads(self) -> list[dict[str, Any]]:
        return [
            json.loads(row[0])
            for row in self.connection.execute("SELECT payload FROM action_presets")
        ]

    def save_action_run(self, run: ActionRun, findings: FindingSet) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO action_runs VALUES(?,?,?,?)",
                (run.id, run.preset_id, _json(run), run.created_at),
            )
            connection.execute(
                "INSERT INTO finding_sets VALUES(?,?,?,?)",
                (findings.id, run.id, _json(findings), utc_now()),
            )

    def save_snapshot(self, snapshot_id: str, root_id: str, items: list[ItemSnapshot]) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO snapshots VALUES(?,?,?,?)",
                (snapshot_id, root_id, utc_now(), "complete"),
            )
            connection.executemany(
                "INSERT INTO items VALUES(?,?,?,?,?)",
                [
                    (item.id, snapshot_id, item.root_id, item.relative_path, _json(item))
                    for item in items
                ],
            )

    def list_items(self, snapshot_id: str | None = None) -> list[dict[str, Any]]:
        if snapshot_id:
            rows = self.connection.execute(
                "SELECT payload FROM items WHERE snapshot_id=? ORDER BY relative_path",
                (snapshot_id,),
            )
        else:
            rows = self.connection.execute(
                """
                SELECT i.payload
                FROM items i
                JOIN snapshots s ON s.id=i.snapshot_id
                WHERE s.rowid=(
                    SELECT MAX(s2.rowid) FROM snapshots s2 WHERE s2.root_id=s.root_id
                )
                ORDER BY i.relative_path
                """
            )
        return [json.loads(row[0]) for row in rows]

    def cached_metadata(self, item: ItemSnapshot) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT fingerprint,payload,updated_at FROM metadata_cache "
            "WHERE root_id=? AND relative_path=?",
            (item.root_id, item.relative_path),
        ).fetchone()
        if not row or row["fingerprint"] != _metadata_fingerprint(item):
            return None
        payload = json.loads(row["payload"])
        payload["_cache"] = {
            "updated_at": row["updated_at"],
            "fresh": True,
            "validated_by": "size+modified_ns",
        }
        return payload

    def metadata_cache_records(self) -> dict[tuple[str, str], dict[str, Any]]:
        """Return a thread-safe value snapshot; no SQLite objects escape this method."""
        rows = self.connection.execute(
            "SELECT root_id,relative_path,fingerprint,payload,updated_at "
            "FROM metadata_cache"
        )
        return {
            (str(row["root_id"]), str(row["relative_path"])): {
                "fingerprint": str(row["fingerprint"]),
                "payload": json.loads(row["payload"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        }

    def save_cached_metadata(
        self, item: ItemSnapshot, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return self.save_cached_metadata_batch([(item, payload)])[
            (item.root_id, item.relative_path)
        ]

    def save_cached_metadata_batch(
        self,
        values: list[tuple[ItemSnapshot, dict[str, Any]]],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        if not values:
            return {}
        updated_at = datetime.now(UTC).isoformat()
        saved: dict[tuple[str, str], dict[str, Any]] = {}
        with self.transaction() as connection:
            for item, payload in values:
                members = list(payload.get("archive_members", []))
                stored = {
                    key: value
                    for key, value in payload.items()
                    if key not in {"_cache", "archive_members"}
                }
                if members:
                    stored["archive_members_stored"] = len(members)
                connection.execute(
                    "INSERT INTO metadata_cache VALUES(?,?,?,?,?) "
                    "ON CONFLICT(root_id,relative_path) DO UPDATE SET "
                    "fingerprint=excluded.fingerprint,payload=excluded.payload,"
                    "updated_at=excluded.updated_at",
                    (
                        item.root_id,
                        item.relative_path,
                        _metadata_fingerprint(item),
                        json.dumps(stored, ensure_ascii=False, sort_keys=True),
                        updated_at,
                    ),
                )
                connection.execute(
                    "DELETE FROM archive_members WHERE root_id=? AND archive_relative_path=?",
                    (item.root_id, item.relative_path),
                )
                connection.executemany(
                    "INSERT INTO archive_members VALUES(?,?,?,?,?)",
                    [
                        (
                            item.root_id,
                            item.relative_path,
                            index,
                            str(member.get("path", "")),
                            json.dumps(member, ensure_ascii=False, sort_keys=True),
                        )
                        for index, member in enumerate(members)
                        if str(member.get("path", ""))
                    ],
                )
                saved[(item.root_id, item.relative_path)] = {
                    **stored,
                    "_cache": {
                        "updated_at": updated_at,
                        "fresh": True,
                        "validated_by": "size+modified_ns",
                    },
                }
        return saved

    def merge_cached_metadata_batch(
        self,
        values: dict[tuple[str, str], dict[str, Any]],
    ) -> None:
        """Merge OS-derived facts into both the durable cache and latest visible snapshots."""
        if not values:
            return
        with self.transaction() as connection:
            for (root_id, relative_path), additions in values.items():
                cached = connection.execute(
                    "SELECT payload FROM metadata_cache WHERE root_id=? AND relative_path=?",
                    (root_id, relative_path),
                ).fetchone()
                if cached:
                    payload = json.loads(cached["payload"])
                    payload.update(additions)
                    connection.execute(
                        "UPDATE metadata_cache SET payload=? WHERE root_id=? AND relative_path=?",
                        (
                            json.dumps(payload, ensure_ascii=False, sort_keys=True),
                            root_id,
                            relative_path,
                        ),
                    )
                rows = connection.execute(
                    """
                    SELECT i.id,i.payload FROM items i
                    JOIN snapshots s ON s.id=i.snapshot_id
                    WHERE i.root_id=? AND i.relative_path=? AND s.rowid=(
                        SELECT MAX(s2.rowid) FROM snapshots s2 WHERE s2.root_id=s.root_id
                    )
                    """,
                    (root_id, relative_path),
                ).fetchall()
                for row in rows:
                    item = json.loads(row["payload"])
                    metadata = dict(item.get("metadata", {}))
                    metadata.update(additions)
                    item["metadata"] = metadata
                    connection.execute(
                        "UPDATE items SET payload=? WHERE id=?",
                        (json.dumps(item, ensure_ascii=False, sort_keys=True), row["id"]),
                    )

    def list_archive_members(
        self,
        root_id: str,
        relative_path: str,
        *,
        glob: str = "**",
        offset: int = 0,
        limit: int = 250,
    ) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT member_path,payload FROM archive_members "
            "WHERE root_id=? AND archive_relative_path=? ORDER BY member_path",
            (root_id, relative_path),
        )
        pattern = (glob or "**").replace("\\", "/").casefold()
        matches = [
            json.loads(row["payload"])
            for row in rows
            if pattern == "**"
            or fnmatch.fnmatchcase(str(row["member_path"]).replace("\\", "/").casefold(), pattern)
        ]
        start = max(0, offset)
        bounded = max(1, min(1_000, limit))
        return {
            "members": matches[start : start + bounded],
            "total": len(matches),
            "offset": start,
            "limit": bounded,
            "has_more": start + bounded < len(matches),
            "glob": glob,
        }

    def clear_metadata_cache(self) -> int:
        """Explicit destructive cache clearing retained for maintenance/tests, never routine scans."""
        count = int(self.connection.execute("SELECT COUNT(*) FROM metadata_cache").fetchone()[0])
        self.connection.execute("DELETE FROM metadata_cache")
        self.connection.execute("DELETE FROM archive_members")
        self.connection.commit()
        return count

    def prune_metadata_cache(self, root_id: str, relative_paths: set[str]) -> int:
        rows = self.connection.execute(
            "SELECT relative_path FROM metadata_cache WHERE root_id=?", (root_id,)
        ).fetchall()
        stale = [row["relative_path"] for row in rows if row["relative_path"] not in relative_paths]
        # Historical records are retained. They are simply unreachable from the current snapshot.
        return len(stale)

    def metadata_cache_stats(self) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT COUNT(*) AS records FROM metadata_cache"
        ).fetchone()
        archive_members = int(
            self.connection.execute("SELECT COUNT(*) FROM archive_members").fetchone()[0]
        )
        return {
            "records": int(row["records"] or 0),
            "fresh": int(row["records"] or 0),
            "expired": 0,
            "archive_members": archive_members,
            "validation": "size+modified_ns",
        }

    def save_semantic_record(self, record: SemanticRecord) -> None:
        self.connection.execute(
            "INSERT INTO semantic_records VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(entity_kind,entity_key,namespace) DO UPDATE SET "
            "payload=excluded.payload,source_fingerprint=excluded.source_fingerprint,"
            "status=excluded.status,updated_at=excluded.updated_at,checked_at=excluded.checked_at",
            (
                record.entity_kind,
                record.entity_key,
                record.namespace,
                _json(record),
                record.source_fingerprint,
                record.status,
                record.updated_at,
                record.checked_at,
            ),
        )
        self.connection.commit()

    def get_semantic_record(
        self, entity_kind: str, entity_key: str, namespace: str
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT payload,status,source_fingerprint FROM semantic_records "
            "WHERE entity_kind=? AND entity_key=? AND namespace=?",
            (entity_kind, entity_key, namespace),
        ).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"])
        payload["status"] = row["status"]
        payload["source_fingerprint"] = row["source_fingerprint"]
        return payload

    def list_semantic_records(
        self, entity_kind: str | None = None, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[str] = []
        if entity_kind:
            clauses.append("entity_kind=?")
            values.append(entity_kind)
        if namespace:
            clauses.append("namespace=?")
            values.append(namespace)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"SELECT payload,status,source_fingerprint FROM semantic_records{where} "
            "ORDER BY updated_at DESC",
            values,
        )
        result = []
        for row in rows:
            payload = json.loads(row["payload"])
            payload["status"] = row["status"]
            payload["source_fingerprint"] = row["source_fingerprint"]
            result.append(payload)
        return result

    def mark_semantic_stale(
        self,
        entity_kind: str,
        entity_key: str,
        source_fingerprint: str,
        namespace: str | None = None,
    ) -> int:
        namespace_clause = " AND namespace=?" if namespace else ""
        values = [utc_now(), entity_kind, entity_key, source_fingerprint]
        if namespace:
            values.append(namespace)
        cursor = self.connection.execute(
            "UPDATE semantic_records SET status='stale',checked_at=? "
            "WHERE entity_kind=? AND entity_key=? AND source_fingerprint<>? AND status='current'"
            + namespace_clause,
            values,
        )
        self.connection.commit()
        return cursor.rowcount

    def mark_semantic_stale_batch(
        self,
        values: list[tuple[str, str, str]],
        namespace: str | None = None,
    ) -> int:
        if not values:
            return 0
        namespace_clause = " AND namespace=?" if namespace else ""
        changed = 0
        checked_at = utc_now()
        with self.transaction() as connection:
            for entity_kind, entity_key, source_fingerprint in values:
                parameters = [checked_at, entity_kind, entity_key, source_fingerprint]
                if namespace:
                    parameters.append(namespace)
                cursor = connection.execute(
                    "UPDATE semantic_records SET status='stale',checked_at=? "
                    "WHERE entity_kind=? AND entity_key=? AND source_fingerprint<>? "
                    "AND status='current'" + namespace_clause,
                    parameters,
                )
                changed += cursor.rowcount
        return changed

    def save_software_inventory(self, packages: list[SoftwarePackage]) -> None:
        scanned_at = utc_now()
        current_ids = {package.id for package in packages}
        with self.transaction() as connection:
            connection.execute("UPDATE software_inventory SET status='missing'")
            for package in packages:
                connection.execute(
                    "INSERT INTO software_inventory VALUES(?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,"
                    "version_fingerprint=excluded.version_fingerprint,status='installed',"
                    "scanned_at=excluded.scanned_at",
                    (
                        package.id,
                        _json(package),
                        package.version_fingerprint,
                        "installed",
                        scanned_at,
                    ),
                )
            if not current_ids:
                connection.execute("UPDATE software_inventory SET status='missing'")

    def list_software_inventory(self, installed_only: bool = True) -> list[dict[str, Any]]:
        where = " WHERE status='installed'" if installed_only else ""
        rows = self.connection.execute(
            f"SELECT payload,status,scanned_at FROM software_inventory{where} ORDER BY id"
        )
        result = []
        for row in rows:
            payload = json.loads(row["payload"])
            payload["status"] = row["status"]
            payload["scanned_at"] = row["scanned_at"]
            result.append(payload)
        return result

    def save_evidence(self, evidence: Evidence) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO evidence VALUES(?,?,?)",
            (evidence.id, evidence.item_id, _json(evidence)),
        )
        self.connection.commit()

    def save_recurrence_series(self, series: RecurrenceSeries) -> None:
        series.validate()
        self.connection.execute(
            """
            INSERT INTO recurrence_series(id,payload,revision,status,updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                payload=excluded.payload,
                revision=excluded.revision,
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (series.id, _json(series), series.revision, series.status, utc_now()),
        )
        self.connection.commit()

    def save_email_account(self, account: EmailAccount) -> None:
        with self.transaction() as connection:
            if account.active:
                connection.execute("UPDATE email_accounts SET active=0")
            connection.execute(
                """
                INSERT INTO email_accounts(id,payload,revision,active,updated_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    payload=excluded.payload,revision=excluded.revision,
                    active=excluded.active,updated_at=excluded.updated_at
                """,
                (account.id, _json(account), account.revision, int(account.active), utc_now()),
            )

    def list_email_accounts(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payload,active FROM email_accounts ORDER BY active DESC,updated_at DESC"
        )
        values = []
        for row in rows:
            payload = json.loads(row["payload"])
            payload["active"] = bool(row["active"])
            values.append(payload)
        return values

    def save_mail_folders(self, folders: list[MailFolderSnapshot]) -> None:
        with self.transaction() as connection:
            for folder in folders:
                connection.execute(
                    """
                    INSERT INTO mail_folders(account_id,id,payload,updated_at) VALUES(?,?,?,?)
                    ON CONFLICT(account_id,id) DO UPDATE SET
                        payload=excluded.payload,updated_at=excluded.updated_at
                    """,
                    (folder.account_id, folder.id, _json(folder), utc_now()),
                )

    def list_mail_folders(self, account_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payload FROM mail_folders WHERE account_id=? ORDER BY payload",
            (account_id,),
        )
        return [json.loads(row[0]) for row in rows]

    def apply_mail_delta(self, messages: list[MailMessageSnapshot]) -> None:
        with self.transaction() as connection:
            for message in messages:
                if not message.id:
                    continue
                connection.execute(
                    """
                    INSERT INTO mail_messages(
                        account_id,id,folder_id,change_key,etag,removed,payload,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(account_id,id) DO UPDATE SET
                        folder_id=excluded.folder_id,change_key=excluded.change_key,
                        etag=excluded.etag,removed=excluded.removed,
                        payload=excluded.payload,updated_at=excluded.updated_at
                    """,
                    (
                        message.account_id,
                        message.id,
                        message.folder_id,
                        message.change_key,
                        message.etag,
                        int(message.removed),
                        _json(message),
                        utc_now(),
                    ),
                )

    def list_mail_messages(
        self, account_id: str, folder_id: str = "", *, include_removed: bool = False
    ) -> list[dict[str, Any]]:
        conditions = ["account_id=?"]
        parameters: list[Any] = [account_id]
        if folder_id:
            conditions.append("folder_id=?")
            parameters.append(folder_id)
        if not include_removed:
            conditions.append("removed=0")
        rows = self.connection.execute(
            "SELECT payload FROM mail_messages WHERE "
            + " AND ".join(conditions)
            + " ORDER BY updated_at DESC",
            parameters,
        )
        return [json.loads(row[0]) for row in rows]

    def save_mail_attachments(self, attachments: list[MailAttachmentSnapshot]) -> None:
        with self.transaction() as connection:
            for attachment in attachments:
                connection.execute(
                    """
                    INSERT INTO mail_attachments(account_id,message_id,id,payload,updated_at)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(account_id,message_id,id) DO UPDATE SET
                        payload=excluded.payload,updated_at=excluded.updated_at
                    """,
                    (
                        attachment.account_id,
                        attachment.message_id,
                        attachment.id,
                        _json(attachment),
                        utc_now(),
                    ),
                )

    def list_mail_attachments(self, account_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payload FROM mail_attachments WHERE account_id=? ORDER BY updated_at DESC",
            (account_id,),
        )
        return [json.loads(row[0]) for row in rows]

    def save_mail_delta_token(self, account_id: str, folder_id: str, delta_link: str) -> None:
        if not delta_link:
            return
        self.connection.execute(
            """
            INSERT INTO mail_delta_tokens(account_id,folder_id,delta_link,updated_at)
            VALUES(?,?,?,?)
            ON CONFLICT(account_id,folder_id) DO UPDATE SET
                delta_link=excluded.delta_link,updated_at=excluded.updated_at
            """,
            (account_id, folder_id, delta_link, utc_now()),
        )
        self.connection.commit()

    def mail_delta_token(self, account_id: str, folder_id: str) -> str:
        row = self.connection.execute(
            "SELECT delta_link FROM mail_delta_tokens WHERE account_id=? AND folder_id=?",
            (account_id, folder_id),
        ).fetchone()
        return str(row[0]) if row else ""

    def save_email_proposal(self, proposal: EmailProposal) -> None:
        proposal.validate()
        self.connection.execute(
            """
            INSERT INTO email_proposals(
                id,account_id,kind,status,revision,payload,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status,revision=excluded.revision,
                payload=excluded.payload,updated_at=excluded.updated_at
            """,
            (
                proposal.id,
                proposal.account_id,
                proposal.kind.value,
                proposal.status.value,
                proposal.revision,
                _json(proposal),
                proposal.created_at,
                utc_now(),
            ),
        )
        self.connection.commit()

    def list_email_proposals(self, account_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payload FROM email_proposals WHERE account_id=? ORDER BY created_at DESC",
            (account_id,),
        )
        return [json.loads(row[0]) for row in rows]

    def delete_email_proposals(self, proposal_ids: set[str]) -> int:
        if not proposal_ids:
            return 0
        placeholders = ",".join("?" for _ in proposal_ids)
        cursor = self.connection.execute(
            f"DELETE FROM email_proposals WHERE id IN ({placeholders})",
            sorted(proposal_ids),
        )
        self.connection.commit()
        return cursor.rowcount

    def save_account_security_evidence(
        self, account_id: str, evidence: list[AccountSecurityEvidence]
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM account_security_evidence WHERE account_id=?", (account_id,)
            )
            connection.executemany(
                "INSERT INTO account_security_evidence VALUES(?,?,?,?)",
                [(value.id, account_id, _json(value), utc_now()) for value in evidence],
            )

    def list_account_security_evidence(self, account_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payload FROM account_security_evidence WHERE account_id=? ORDER BY updated_at DESC",
            (account_id,),
        )
        return [json.loads(row[0]) for row in rows]

    def list_recurrence_series(self, reviewed_only: bool = True) -> list[dict[str, Any]]:
        where = " WHERE status='reviewed'" if reviewed_only else ""
        rows = self.connection.execute(
            f"SELECT payload FROM recurrence_series{where} ORDER BY updated_at DESC"
        )
        return [json.loads(row[0]) for row in rows]

    def save_recurrence_exception(self, exception: RecurrenceException) -> None:
        self.connection.execute(
            """
            INSERT INTO recurrence_exceptions VALUES(?,?,?,?,?)
            ON CONFLICT(series_id,period_start) DO UPDATE SET
                status=excluded.status,
                reason=excluded.reason,
                updated_at=excluded.updated_at
            """,
            (
                exception.series_id,
                exception.period_start,
                exception.status.value,
                exception.reason,
                exception.updated_at,
            ),
        )
        self.connection.commit()

    def delete_recurrence_exception(self, series_id: str, period_start: str) -> None:
        self.connection.execute(
            "DELETE FROM recurrence_exceptions WHERE series_id=? AND period_start=?",
            (series_id, period_start),
        )
        self.connection.commit()

    def list_recurrence_exceptions(self, series_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT series_id,period_start,status,reason,updated_at
            FROM recurrence_exceptions
            WHERE series_id=? ORDER BY period_start
            """,
            (series_id,),
        )
        return [dict(row) for row in rows]

    def list_evidence_payloads(
        self,
        item_ids: set[str],
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        if not item_ids:
            return {"evidence": [], "total": 0, "offset": max(0, offset), "has_more": False}
        placeholders = ",".join("?" for _ in item_ids)
        rows = list(
            self.connection.execute(
                f"SELECT payload FROM evidence WHERE item_id IN ({placeholders}) "
                "ORDER BY rowid DESC",
                sorted(item_ids),
            )
        )
        start = max(0, offset)
        bounded = max(1, min(250, limit))
        return {
            "evidence": [json.loads(row["payload"]) for row in rows[start : start + bounded]],
            "total": len(rows),
            "offset": start,
            "has_more": start + bounded < len(rows),
        }

    def create_selection_scope(self, scope: SelectionScope) -> None:
        known_items = {str(item["id"]) for item in self.list_items()}
        if not scope.item_ids or len(scope.item_ids) > 250:
            raise ValueError("Selection scopes contain between 1 and 250 items")
        if not set(scope.item_ids).issubset(known_items):
            raise ValueError("Selection scope contains an unknown inventory item")
        self.connection.execute(
            "INSERT INTO selection_scopes VALUES(?,?,?,?,?,?)",
            (
                scope.id,
                scope.proposal_set_id,
                json.dumps(list(scope.item_ids)),
                scope.created_at,
                scope.expires_at,
                scope.status,
            ),
        )
        self.connection.commit()

    def get_selection_scope(self, scope_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM selection_scopes WHERE id=?", (scope_id,)
        ).fetchone()
        if not row:
            return None
        active = row["status"] == "active" and datetime.fromisoformat(row["expires_at"]) > datetime.now(UTC)
        if not active and row["status"] == "active":
            self.connection.execute(
                "UPDATE selection_scopes SET status='expired' WHERE id=?", (scope_id,)
            )
            self.connection.commit()
        return {
            "id": row["id"],
            "proposal_set_id": row["proposal_set_id"],
            "item_ids": json.loads(row["item_ids"]),
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "status": "active" if active else "expired",
        }

    def latest_selection_scope(self) -> dict[str, Any] | None:
        rows = self.connection.execute(
            "SELECT id FROM selection_scopes ORDER BY rowid DESC LIMIT 100"
        )
        for row in rows:
            scope = self.get_selection_scope(str(row["id"]))
            if scope and scope["status"] == "active":
                return scope
        return None

    def attach_scope_to_proposal(self, scope_id: str, proposal_set_id: str) -> None:
        cursor = self.connection.execute(
            "UPDATE selection_scopes SET proposal_set_id=? WHERE id=? AND status='active'",
            (proposal_set_id, scope_id),
        )
        self.connection.commit()
        if cursor.rowcount != 1:
            raise ValueError("Selection scope is unavailable")

    def idempotent_response(
        self, tool_name: str, idempotency_key: str, request_hash: str
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT tool_name,request_hash,response FROM mcp_idempotency WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if not row:
            return None
        if row["tool_name"] != tool_name or row["request_hash"] != request_hash:
            raise ValueError("Idempotency key was already used for a different request")
        return json.loads(row["response"])

    def save_idempotent_response(
        self,
        tool_name: str,
        idempotency_key: str,
        request_hash: str,
        response: dict[str, Any],
    ) -> None:
        self.connection.execute(
            "INSERT INTO mcp_idempotency VALUES(?,?,?,?,?)",
            (
                idempotency_key,
                tool_name,
                request_hash,
                json.dumps(response, ensure_ascii=False, sort_keys=True),
                utc_now(),
            ),
        )
        self.connection.commit()

    def record_mcp_audit(
        self,
        tool_name: str,
        request_id: str,
        affected_item_ids: list[str],
        result: str,
        *,
        revision_before: int | None = None,
        revision_after: int | None = None,
        client_identity: str = "unavailable",
    ) -> None:
        self.connection.execute(
            "INSERT INTO mcp_audit(occurred_at,client_identity,tool_name,request_id,"
            "revision_before,revision_after,affected_item_ids,result) VALUES(?,?,?,?,?,?,?,?)",
            (
                utc_now(),
                client_identity[:200],
                tool_name[:100],
                request_id[:200],
                revision_before,
                revision_after,
                json.dumps(affected_item_ids[:250]),
                result[:500],
            ),
        )
        self.connection.commit()

    def save_proposal_set(self, proposal_set: ProposalSet) -> None:
        self.connection.execute(
            "INSERT INTO proposal_sets VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,payload=excluded.payload",
            (
                proposal_set.id,
                str(proposal_set.kind),
                proposal_set.revision,
                _json(proposal_set),
                proposal_set.created_at,
            ),
        )
        self.connection.commit()

    def save_frozen_plan(self, plan: Any) -> None:
        self.connection.execute(
            "INSERT INTO frozen_plans VALUES(?,?,?,?,?)",
            (plan.id, plan.proposal_set_id, str(plan.state), _json(plan), plan.created_at),
        )
        self.connection.commit()

    def get_proposal_payload(self, proposal_set_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT payload FROM proposal_sets WHERE id=?", (proposal_set_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def mark_proposals_stale(self, kinds: set[str], reason: str) -> int:
        rows = list(self.connection.execute("SELECT id,kind,payload FROM proposal_sets"))
        changed = 0
        with self.transaction() as connection:
            for row in rows:
                if row["kind"] not in kinds:
                    continue
                payload = json.loads(row["payload"])
                for item in payload.get("items", []):
                    item["status"] = "stale"
                    item.setdefault("issues", []).append(reason)
                payload["stale_reason"] = reason
                connection.execute(
                    "UPDATE proposal_sets SET payload=? WHERE id=?",
                    (json.dumps(payload, sort_keys=True), row["id"]),
                )
                connection.execute(
                    "UPDATE frozen_plans SET state='stale' WHERE proposal_set_id=? AND state NOT IN ('completed','verified')",
                    (row["id"],),
                )
                changed += 1
        return changed

    def save_journal(
        self, journal_id: str, plan_id: str, state: str, payload: dict[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO journals VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,payload=excluded.payload,updated_at=excluded.updated_at",
            (journal_id, plan_id, state, json.dumps(payload), utc_now()),
        )
        self.connection.commit()

    def incomplete_journals(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payload FROM journals WHERE state NOT IN ('completed','rolled_back','verified','undone')"
        )
        return [json.loads(row[0]) for row in rows]

    def latest_completed_journal(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT payload FROM journals
            WHERE state='completed' AND plan_id NOT LIKE 'undo_%'
            ORDER BY rowid DESC LIMIT 1
            """
        ).fetchone()
        return json.loads(row[0]) if row else None

    def latest_completed_cleanup_journal(self) -> dict[str, Any] | None:
        rows = self.connection.execute(
            """
            SELECT payload FROM journals
            WHERE state='completed'
              AND plan_id NOT LIKE 'restore_%'
              AND plan_id NOT LIKE 'undo_%'
            ORDER BY rowid DESC
            """
        )
        for row in rows:
            payload = json.loads(row[0])
            operations = payload.get("operations", [])
            if operations and all(
                operation.get("kind") == "cleanup"
                for operation in operations
                if operation.get("kind")
            ):
                return payload
        return None

    def completed_move_source_folders(self) -> set[Path]:
        rows = self.connection.execute(
            """
            SELECT payload FROM journals
            WHERE state='completed'
              AND plan_id NOT LIKE 'undo_%'
              AND plan_id NOT LIKE 'restore_%'
            """
        )
        folders: set[Path] = set()
        for row in rows:
            payload = json.loads(row[0])
            for operation in payload.get("operations", []):
                if operation.get("kind") == "move" and operation.get("source"):
                    folders.add(Path(str(operation["source"])).parent.resolve(strict=False))
        return folders

    def activity(self, kind: str, summary: str, details: dict[str, Any] | None = None) -> None:
        redacted_details = redact_sensitive(json.dumps(details or {}, sort_keys=True))
        self.connection.execute(
            "INSERT INTO activity(occurred_at,kind,summary,details) VALUES(?,?,?,?)",
            (utc_now(), kind, redact_sensitive(summary), redacted_details),
        )
        self.connection.commit()

    def list_activity(self, limit: int = 500) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT occurred_at,kind,summary,details FROM activity ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        )

    def _save_payload(self, table: str, object_id: str, value: Any, revision: int) -> None:
        if table not in {"categories", "assignments", "action_presets", "tag_definitions"}:
            raise ValueError("Invalid payload table")
        self.connection.execute(
            f"INSERT INTO {table} VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,revision=excluded.revision",
            (object_id, _json(value), revision),
        )
        self.connection.commit()


def _metadata_fingerprint(item: ItemSnapshot) -> str:
    return f"{item.size}:{item.modified_ns}"


def _json(value: Any) -> str:
    return json.dumps(asdict(value), default=_default, sort_keys=True, ensure_ascii=False)


def _default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return sorted(str(item) for item in value)
    if hasattr(value, "value"):
        return value.value
    raise TypeError(type(value).__name__)
