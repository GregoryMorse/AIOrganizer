from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def ensure_codex_mcp_registration() -> str:
    """Register the stable local MCP launcher once when Codex is installed.

    The server resolves the active workspace through the pointer maintained by the desktop app,
    so this registration does not need rewriting when the user switches workspaces.
    """
    if os.getenv("AIORGANIZER_AUTO_REGISTER_MCP", "1").casefold() in {"0", "false", "no"}:
        return "disabled"
    codex = shutil.which("codex")
    if not codex:
        return "codex_not_found"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        command = _source_mcp_command()
        existing = subprocess.run(
            [codex, "mcp", "get", "aiorganizer", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=flags,
        )
        if existing.returncode == 0:
            try:
                transport = json.loads(existing.stdout).get("transport", {})
                configured = [str(transport.get("command", "")), *transport.get("args", [])]
            except (json.JSONDecodeError, TypeError):
                configured = []
            if configured == command:
                return "already_registered"
            subprocess.run(
                [codex, "mcp", "remove", "aiorganizer"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=flags,
            )
        added = subprocess.run(
            [codex, "mcp", "add", "aiorganizer", "--", *command],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=flags,
        )
        return "registered" if added.returncode == 0 else "registration_failed"
    except (OSError, subprocess.SubprocessError):
        return "registration_failed"


def _source_mcp_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--mcp"]
    root = Path(__file__).resolve().parents[3]
    return [sys.executable, str(root / "dev.py"), "--mcp"]
