from __future__ import annotations

import subprocess

import pytest

from ai_organizer.adapters.windows_system import WindowsSystemInspector


def test_pending_driver_updates_uses_read_only_wua_search(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: list[str] = []

    def fake_run(command, **_kwargs):  # type: ignore[no-untyped-def]
        captured.extend(str(value) for value in command)
        return subprocess.CompletedProcess(command, 0, '[{"title":"Driver update"}]', "")

    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.setattr("subprocess.run", fake_run)

    rows = WindowsSystemInspector().pending_updates("Driver")

    assert rows == [{"title": "Driver update"}]
    script = captured[-1]
    assert "IsInstalled=0 and IsHidden=0 and Type='Driver'" in script
    assert ".Install(" not in script
    assert ".Download(" not in script


def test_fragmentation_analysis_validates_drive_letter(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("platform.system", lambda: "Windows")
    with pytest.raises(ValueError, match="valid drive letter"):
        WindowsSystemInspector().analyze_fragmentation("C & format")


def test_non_windows_check_fails_with_clear_scope(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("platform.system", lambda: "Linux")
    with pytest.raises(RuntimeError, match="Windows only"):
        WindowsSystemInspector().health()
