from __future__ import annotations

import json
import platform
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_organizer.domain.models import utc_now


@dataclass(frozen=True, slots=True)
class DefenderHistoryResult:
    available: bool
    checked_at: str
    detections_by_path: dict[str, list[dict[str, Any]]]
    error: str = ""


class DefenderStatusScanner:
    """Read Microsoft Defender history and correlate it to known local paths.

    This deliberately does not trigger a scan or alter Defender configuration.
    """

    def history_for_paths(
        self,
        paths: list[Path],
        progress: Callable[[int, int, str], None] | None = None,
    ) -> DefenderHistoryResult:
        checked_at = utc_now()
        if platform.system() != "Windows":
            return DefenderHistoryResult(False, checked_at, {}, "Available only on Windows")
        candidates = {str(path.resolve(strict=False)).casefold(): str(path) for path in paths}
        if not candidates:
            return DefenderHistoryResult(True, checked_at, {})
        if progress:
            progress(0, 0, "Reading Microsoft Defender detection history…")
        script = r"""
$ErrorActionPreference = 'Stop'
$catalog = @{}
Get-MpThreat | ForEach-Object { $catalog[[string]$_.ThreatID] = $_ }
$rows = @(Get-MpThreatDetection | ForEach-Object {
    $threat = $catalog[[string]$_.ThreatID]
    [PSCustomObject]@{
        DetectionID = [string]$_.DetectionID
        ThreatID = [string]$_.ThreatID
        ThreatName = [string]$threat.ThreatName
        SeverityID = [int]$threat.SeverityID
        CategoryID = [int]$threat.CategoryID
        Resources = @($_.Resources)
        InitialDetectionTime = if ($_.InitialDetectionTime) { $_.InitialDetectionTime.ToString('o') } else { '' }
        LastStatusChangeTime = if ($_.LastThreatStatusChangeTime) { $_.LastThreatStatusChangeTime.ToString('o') } else { '' }
        RemediationTime = if ($_.RemediationTime) { $_.RemediationTime.ToString('o') } else { '' }
        ThreatStatusID = [int]$_.ThreatStatusID
        CleaningActionID = [int]$_.CleaningActionID
        ActionSuccess = [bool]$_.ActionSuccess
    }
})
ConvertTo-Json -InputObject $rows -Depth 5 -Compress
"""
        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    script,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            rows = json.loads(result.stdout or "[]")
            if isinstance(rows, dict):
                rows = [rows]
            correlations = correlate_defender_resources(candidates, rows, progress)
            return DefenderHistoryResult(True, checked_at, correlations)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
            detail = getattr(error, "stderr", "") or str(error)
            return DefenderHistoryResult(False, checked_at, {}, str(detail).strip()[:2_000])


def correlate_defender_resources(
    candidates: dict[str, str],
    rows: list[dict[str, Any]],
    progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {path: [] for path in candidates.values()}
    total = len(rows)
    if progress:
        progress(0, total, f"Correlating {total:,} Defender history record(s)…")
    for row_index, row in enumerate(rows, start=1):
        resources = row.get("Resources", [])
        if isinstance(resources, str):
            resources = [resources]
        folded_resources = "\n".join(str(value).casefold() for value in resources)
        public = {
            "detection_id": str(row.get("DetectionID", "")),
            "threat_id": str(row.get("ThreatID", "")),
            "threat_name": str(row.get("ThreatName", "")),
            "severity_id": int(row.get("SeverityID", 0) or 0),
            "category_id": int(row.get("CategoryID", 0) or 0),
            "resources": [str(value) for value in resources],
            "initial_detection_time": str(row.get("InitialDetectionTime", "")),
            "last_status_change_time": str(row.get("LastStatusChangeTime", "")),
            "remediation_time": str(row.get("RemediationTime", "")),
            "threat_status_id": int(row.get("ThreatStatusID", 0) or 0),
            "cleaning_action_id": int(row.get("CleaningActionID", 0) or 0),
            "action_success": bool(row.get("ActionSuccess", False)),
        }
        for normalized, display_path in candidates.items():
            if normalized in folded_resources:
                result[display_path].append(public)
        if progress and (row_index == total or row_index % 25 == 0):
            progress(
                row_index,
                total,
                f"Correlating Defender record {row_index:,} of {total:,}…",
            )
    return result
