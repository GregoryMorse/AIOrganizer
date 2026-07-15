from __future__ import annotations

import sqlite3
from pathlib import Path

from ai_organizer.adapters.persistence import WorkspaceStore
from ai_organizer.domain.models import CloudPolicy, SourceRoot
from ai_organizer.domain.prompts import PromptLayerKind, PromptRevision
from ai_organizer.domain.recurrence import (
    Cadence,
    GapStatus,
    RecurrenceException,
    RecurrenceSeries,
    SeriesObservation,
)


def test_workspace_create_reopen_and_save_as(tmp_path: Path) -> None:
    path = tmp_path / "example.aioworkspace"
    store = WorkspaceStore.create(path, "Example")
    source_path = tmp_path / "source"
    source_path.mkdir()
    source = SourceRoot(source_path, "Source", cloud_policy=CloudPolicy.NONE)
    store.save_source(source)
    revision = PromptRevision("view:rename", PromptLayerKind.VIEW, "Prefer dates")
    store.save_prompt_revision(revision)
    copied = tmp_path / "copy.aioworkspace"
    store.save_as(copied)
    store.close()

    reopened = WorkspaceStore(path)
    assert reopened.get_meta("name") == "Example"
    assert reopened.list_source_payloads()[0]["cloud_policy"] == "none"
    assert reopened.latest_prompt("view:rename")["id"] == revision.id
    reopened.close()
    copy_store = WorkspaceStore(copied)
    assert copy_store.workspace_id
    copy_store.close()


def test_v1_workspace_is_backed_up_before_migration(tmp_path: Path) -> None:
    path = tmp_path / "legacy.aioworkspace"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        PRAGMA user_version=1;
        """
    )
    connection.close()
    store = WorkspaceStore(path)
    assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 11
    store.close()
    assert list(tmp_path.glob("legacy.aioworkspace.backup-*"))


def test_reviewed_recurrence_series_and_exception_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "recurrence.aioworkspace"
    store = WorkspaceStore.create(path, "Recurrence")
    series = RecurrenceSeries(
        "Statements",
        "Acme",
        "Statement",
        "••••1234",
        Cadence.MONTHLY,
        "2026-01-01",
        None,
        10,
        (SeriesObservation("item", "2026-01-01", 0.9, ("reviewed",)),),
        "fingerprint",
        id="series",
    )
    store.save_recurrence_series(series)
    store.save_recurrence_exception(
        RecurrenceException("series", "2026-02-01", GapStatus.SKIPPED, "Not applicable")
    )
    store.close()

    reopened = WorkspaceStore(path)
    assert reopened.list_recurrence_series()[0]["name"] == "Statements"
    exception = reopened.list_recurrence_exceptions("series")[0]
    assert exception["status"] == "intentionally_skipped"
    assert exception["reason"] == "Not applicable"
    reopened.close()
