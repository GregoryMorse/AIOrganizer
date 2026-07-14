from __future__ import annotations

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
from ai_organizer.domain.models import (
    CategoryAssignment,
    CategoryDefinition,
    Evidence,
    ItemSnapshot,
    ProposalSet,
    SourceRoot,
    utc_now,
)
from ai_organizer.domain.prompts import PromptRevision, redact_sensitive

SCHEMA_VERSION = 2


class WorkspaceStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve(strict=False)
        self._ensure_local_extension()
        self.connection = sqlite3.connect(self.path)
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

    def save_category(self, category: CategoryDefinition) -> None:
        self._save_payload("categories", category.id, category, category.revision)

    def list_category_payloads(self) -> list[dict[str, Any]]:
        return [
            json.loads(row[0]) for row in self.connection.execute("SELECT payload FROM categories")
        ]

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

    def save_evidence(self, evidence: Evidence) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO evidence VALUES(?,?,?)",
            (evidence.id, evidence.item_id, _json(evidence)),
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
        if table not in {"categories", "assignments", "action_presets"}:
            raise ValueError("Invalid payload table")
        self.connection.execute(
            f"INSERT INTO {table} VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,revision=excluded.revision",
            (object_id, _json(value), revision),
        )
        self.connection.commit()


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
