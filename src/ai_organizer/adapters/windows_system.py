from __future__ import annotations

import json
import platform
import subprocess
from typing import Any


class WindowsSystemInspector:
    """Read-only Windows inventory and update checks.

    The PowerShell snippets are constants and never interpolate user-controlled text.
    """

    def installed_drivers(self) -> list[dict[str, Any]]:
        return self._run_json(
            """
            @(Get-CimInstance Win32_PnPSignedDriver -ErrorAction Stop | ForEach-Object {
                [pscustomobject]@{
                    device_name = [string]$_.DeviceName
                    device_class = [string]$_.DeviceClass
                    manufacturer = [string]$_.Manufacturer
                    provider = [string]$_.DriverProviderName
                    version = [string]$_.DriverVersion
                    driver_date = [string]$_.DriverDate
                    inf_name = [string]$_.InfName
                    signed = [bool]$_.IsSigned
                    signer = [string]$_.Signer
                    status = [string]$_.Status
                    device_id = [string]$_.DeviceID
                }
            }) | ConvertTo-Json -Depth 4 -Compress
            """
        )

    def pending_updates(self, update_type: str) -> list[dict[str, Any]]:
        if update_type not in {"Software", "Driver"}:
            raise ValueError("Windows Update type must be Software or Driver")
        script = _UPDATE_SCRIPT.replace("__UPDATE_TYPE__", update_type)
        return self._run_json(script, timeout=300)

    def health(self) -> list[dict[str, Any]]:
        return self._run_json(
            """
            $rows = @()
            try {
                $rows += @(Get-PhysicalDisk -ErrorAction Stop | ForEach-Object {
                    [pscustomobject]@{
                        record_kind = 'Physical disk'
                        name = [string]$_.FriendlyName
                        health_status = [string]$_.HealthStatus
                        operational_status = [string]($_.OperationalStatus -join ', ')
                        media_type = [string]$_.MediaType
                        bus_type = [string]$_.BusType
                        size = [uint64]$_.Size
                        size_remaining = $null
                        drive_letter = ''
                        file_system = ''
                        fragmentation_status = 'Not analyzed'
                    }
                })
            } catch {}
            try {
                $rows += @(Get-Volume -ErrorAction Stop | ForEach-Object {
                    [pscustomobject]@{
                        record_kind = 'Volume'
                        name = [string]$_.FileSystemLabel
                        health_status = [string]$_.HealthStatus
                        operational_status = [string]$_.OperationalStatus
                        media_type = ''
                        bus_type = ''
                        size = [uint64]$_.Size
                        size_remaining = [uint64]$_.SizeRemaining
                        drive_letter = [string]$_.DriveLetter
                        file_system = [string]$_.FileSystem
                        fragmentation_status = 'Not analyzed'
                    }
                })
            } catch {}
            @($rows) | ConvertTo-Json -Depth 4 -Compress
            """
        )

    def analyze_fragmentation(self, drive_letter: str) -> str:
        letter = drive_letter.strip().rstrip(":").upper()
        if len(letter) != 1 or not letter.isascii() or not letter.isalpha():
            raise ValueError("Select a volume with a valid drive letter")
        try:
            result = subprocess.run(
                ["defrag.exe", f"{letter}:", "/A", "/U", "/V"],
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError(f"Windows fragmentation analysis failed: {error}") from error
        output = "\n".join(
            value.strip() for value in (result.stdout, result.stderr) if value.strip()
        )
        if result.returncode != 0:
            raise RuntimeError(output or f"defrag.exe returned {result.returncode}")
        return output or "Analysis completed without a textual report."

    @staticmethod
    def supported() -> bool:
        return platform.system() == "Windows"

    def _run_json(self, script: str, *, timeout: int = 120) -> list[dict[str, Any]]:
        if not self.supported():
            raise RuntimeError("System mode checks are currently supported on Windows only")
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
                timeout=timeout,
                encoding="utf-8-sig",
                errors="replace",
            )
            value = json.loads(result.stdout or "[]")
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Windows system assessment failed: {error}") from error
        rows = value if isinstance(value, list) else [value]
        return [dict(row) for row in rows if isinstance(row, dict)]


_UPDATE_SCRIPT = """
$session = New-Object -ComObject Microsoft.Update.Session
$searcher = $session.CreateUpdateSearcher()
$result = $searcher.Search("IsInstalled=0 and IsHidden=0 and Type='__UPDATE_TYPE__'")
$rows = @()
for ($index = 0; $index -lt $result.Updates.Count; $index++) {
    $update = $result.Updates.Item($index)
    $rows += [pscustomobject]@{
        update_type = '__UPDATE_TYPE__'
        title = [string]$update.Title
        description = [string]$update.Description
        severity = [string]$update.MsrcSeverity
        kb_articles = [string](@($update.KBArticleIDs) -join ', ')
        categories = [string](@($update.Categories | ForEach-Object { $_.Name }) -join ', ')
        reboot_required = [bool]$update.RebootRequired
        downloaded = [bool]$update.IsDownloaded
        eula_accepted = [bool]$update.EulaAccepted
        update_id = [string]$update.Identity.UpdateID
        revision = [int]$update.Identity.RevisionNumber
    }
}
@($rows) | ConvertTo-Json -Depth 5 -Compress
"""
