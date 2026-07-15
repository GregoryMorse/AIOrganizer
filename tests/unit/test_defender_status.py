from __future__ import annotations

from ai_organizer.adapters.defender_status import correlate_defender_resources


def test_correlates_defender_file_resource_case_insensitively() -> None:
    path = r"C:\Users\Example\Downloads\old-tool.exe"
    rows = [
        {
            "DetectionID": "detection",
            "ThreatID": "42",
            "ThreatName": "PUA:Win32/Example",
            "SeverityID": 2,
            "Resources": [r"file:_C:\USERS\EXAMPLE\DOWNLOADS\OLD-TOOL.EXE"],
            "ActionSuccess": True,
        }
    ]

    result = correlate_defender_resources({path.casefold(): path}, rows)

    assert result[path][0]["threat_name"] == "PUA:Win32/Example"
    assert result[path][0]["action_success"] is True


def test_defender_correlation_reports_batch_progress() -> None:
    updates: list[tuple[int, int, str]] = []
    correlate_defender_resources(
        {r"c:\downloads\tool.exe": r"C:\Downloads\tool.exe"},
        [{"Resources": []}, {"Resources": []}],
        lambda completed, total, message: updates.append((completed, total, message)),
    )

    assert updates[0][0:2] == (0, 2)
    assert updates[-1][0:2] == (2, 2)
