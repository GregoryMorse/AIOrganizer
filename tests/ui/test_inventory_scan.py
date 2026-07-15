from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QDialog

from ai_organizer.desktop.inventory_scan import InventoryScanDialog
from ai_organizer.domain.models import SourceRoot


@pytest.mark.ui
def test_inventory_scan_dialog_completes_background_scan(qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    source_path = tmp_path / "Downloads"
    source_path.mkdir()
    (source_path / "notes.txt").write_text("one\ntwo\n", encoding="utf-8")
    dialog = InventoryScanDialog((SourceRoot(source_path, "Downloads", id="root"),), {})
    qtbot.addWidget(dialog)

    code = dialog.start()

    assert code == QDialog.DialogCode.Accepted
    assert dialog.result_value is not None
    assert len(dialog.result_value.runs[0].items) == 1
    assert dialog.result_value.runs[0].items[0].metadata["line_count"] == 2
