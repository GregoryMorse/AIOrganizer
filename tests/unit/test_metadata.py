from __future__ import annotations

import logging
import platform
import sqlite3
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

from ai_organizer.adapters.filesystem import FileSystemInventory, MetadataIndexer
from ai_organizer.adapters.filesystem.metadata import (
    _executable_metadata,
    metadata_cache_compatible,
)
from ai_organizer.adapters.persistence import WorkspaceStore


def test_pdf_parser_warnings_become_bounded_file_health_metadata(
    monkeypatch, capsys, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    class Reader:
        def __init__(self, path: Path, strict: bool = False) -> None:
            assert strict is False
            self.is_encrypted = False
            self.pages = [object()]
            self.metadata = {"/Title": "Recoverable"}
            logging.getLogger("pypdf._reader").warning("invalid pdf header: b' %%PDF'")
            logging.getLogger("pypdf._reader").warning(
                "Ignoring wrong pointing object 38 0 (offset 0)"
            )

    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=Reader))
    root = tmp_path / "root"
    root.mkdir()
    path = root / "recoverable.pdf"
    path.write_bytes(b"not read by fake parser")
    item = FileSystemInventory().scan("root", root, [])[0]

    metadata = MetadataIndexer().extract(path, item)

    assert metadata["file_health_status"] == "warning"
    assert metadata["file_health_issue_count"] == 2
    assert {value["code"] for value in metadata["file_health_issues"]} == {
        "pdf_nonstandard_header",
        "pdf_broken_object_reference",
    }
    assert capsys.readouterr().err == ""


def test_legacy_pdf_cache_refreshes_once_for_file_health_contract(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    path = root / "legacy.pdf"
    path.write_bytes(b"legacy")
    item = FileSystemInventory().scan("root", root, [])[0]

    assert metadata_cache_compatible(item, {"page_count": 1}) is False
    assert (
        metadata_cache_compatible(
            item,
            {"page_count": 1, "file_health_status": "no_issues_observed"},
        )
        is True
    )


def test_text_metadata_is_cached_by_filesystem_fingerprint(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    text = root / "notes.tex"
    text.write_text("one\ntwo\nthree", encoding="utf-8")
    item = FileSystemInventory().scan("root", root, [])[0]
    metadata = MetadataIndexer().extract(text, item)
    store = WorkspaceStore.create(tmp_path / "metadata.aioworkspace", "Metadata")

    cached = store.save_cached_metadata(item, metadata)

    assert cached["line_count"] == 3
    assert store.cached_metadata(item)["line_count"] == 3
    assert store.metadata_cache_stats()["fresh"] == 1
    store.clear_metadata_cache()
    assert store.cached_metadata(item) is None
    assert store.metadata_cache_stats()["records"] == 0
    store.close()


def test_zip_member_metadata_is_stored_separately_and_paginated(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    archive_path = root / "bundle.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("folder/readme.txt", "hello\n")
        archive.writestr("folder/data.bin", b"\0" * 2_048)
    item = FileSystemInventory().scan("root", root, [])[0]
    metadata = MetadataIndexer().extract(archive_path, item)
    store = WorkspaceStore.create(tmp_path / "archive.aioworkspace", "Archive")

    cached = store.save_cached_metadata(item, metadata)
    members = store.list_archive_members("root", "bundle.zip", glob="**/*.txt")

    assert cached["archive_entry_count"] == 2
    assert cached["archive_members_stored"] == 2
    assert "archive_members" not in cached
    assert members["total"] == 1
    assert members["members"][0]["path"] == "folder/readme.txt"
    assert members["members"][0]["uncompressed_size"] == 6
    assert members["members"][0]["compressed_size"] > 0
    store.close()


def test_large_text_uses_bounded_line_count_sampling(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    text = root / "large.log"
    text.write_bytes(b"line contents\n" * 700_000)
    item = FileSystemInventory().scan("root", root, [])[0]

    metadata = MetadataIndexer().extract(text, item)

    assert metadata["line_count_estimated"] is True
    assert metadata["line_count_sampled_bytes"] < text.stat().st_size
    assert metadata["line_count"] > 600_000


def test_cache_age_does_not_expire_unchanged_metadata(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    document = root / "document.txt"
    document.write_text("unchanged", encoding="utf-8")
    item = FileSystemInventory().scan("root", root, [])[0]
    store = WorkspaceStore.create(tmp_path / "durable.aioworkspace", "Durable")
    store.save_cached_metadata(item, {"marker": "preserved"})
    store.connection.execute("UPDATE metadata_cache SET updated_at='2000-01-01T00:00:00+00:00'")
    store.connection.commit()

    cached = store.cached_metadata(item)

    assert cached is not None
    assert cached["marker"] == "preserved"
    assert cached["_cache"]["validated_by"] == "size+modified_ns"
    columns = {row["name"] for row in store.connection.execute("PRAGMA table_info(metadata_cache)")}
    assert "expires_at" not in columns
    store.close()


def test_legacy_ttl_column_is_removed_without_losing_metadata(tmp_path: Path) -> None:
    workspace = tmp_path / "legacy.aioworkspace"
    connection = sqlite3.connect(workspace)
    connection.executescript(
        """
        CREATE TABLE metadata_cache(
            root_id TEXT NOT NULL, relative_path TEXT NOT NULL, fingerprint TEXT NOT NULL,
            payload TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            PRIMARY KEY(root_id, relative_path)
        );
        CREATE INDEX ix_metadata_cache_expiry ON metadata_cache(expires_at);
        INSERT INTO metadata_cache VALUES(
            'root','document.txt','9:10','{"marker":"preserved"}',
            '2020-01-01T00:00:00+00:00','2020-01-01T01:00:00+00:00'
        );
        PRAGMA user_version=6;
        """
    )
    connection.close()

    store = WorkspaceStore(workspace)

    columns = {row["name"] for row in store.connection.execute("PRAGMA table_info(metadata_cache)")}
    assert "expires_at" not in columns
    assert (
        store.metadata_cache_records()[("root", "document.txt")]["payload"]["marker"] == "preserved"
    )
    store.close()


def test_executable_metadata_includes_native_header_properties() -> None:
    metadata = _executable_metadata(Path(sys.executable))

    expected = "pe" if platform.system() == "Windows" else "elf"
    assert metadata["binary_format"] == expected
    assert metadata.get("machine") or metadata.get("machine_id")
    if platform.system() == "Windows":
        assert metadata["pe_kind"] in {"PE32", "PE32+"}
        assert metadata["fixed_file_version"]


def test_os_metadata_merge_is_visible_in_cache_and_latest_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    executable = root / "tool.exe"
    executable.write_bytes(b"MZ")
    item = FileSystemInventory().scan("root", root, [])[0]
    store = WorkspaceStore.create(tmp_path / "defender.aioworkspace", "Defender")
    store.save_cached_metadata(item, {"binary_format": "pe"})
    store.save_snapshot("snapshot", "root", [item])

    store.merge_cached_metadata_batch(
        {
            ("root", "tool.exe"): {
                "windows_defender": {"status": "detected_in_history", "detection_count": 1}
            }
        }
    )

    assert store.cached_metadata(item)["windows_defender"]["detection_count"] == 1
    assert store.list_items()[0]["metadata"]["windows_defender"]["status"] == "detected_in_history"
    store.close()
