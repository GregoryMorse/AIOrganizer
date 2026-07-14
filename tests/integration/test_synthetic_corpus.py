from __future__ import annotations

import time
from pathlib import Path

import pytest

from ai_organizer.adapters.filesystem.inventory import FileSystemInventory
from ai_organizer.cli.synthetic_corpus import generate


@pytest.mark.integration
def test_full_phase_zero_corpus_is_responsive(tmp_path) -> None:  # type: ignore[no-untyped-def]
    corpus = tmp_path / "corpus"
    generate(corpus, file_count=10_000, pdf_count=500)
    started = time.perf_counter()
    items = FileSystemInventory().scan("synthetic", corpus, ())
    elapsed = time.perf_counter() - started

    assert len(items) >= 10_000
    assert sum(Path(item.relative_path).suffix.casefold() == ".pdf" for item in items) >= 500
    assert sum(item.is_project_root for item in items) >= 20
    assert elapsed < 30, f"Synthetic-corpus inventory took {elapsed:.2f} seconds"
