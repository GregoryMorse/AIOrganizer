from __future__ import annotations

import json
import os
from pathlib import Path

from platformdirs import user_state_path


def active_workspace_file() -> Path:
    override = os.getenv("AIORGANIZER_STATE_DIR", "").strip()
    root = Path(override) if override else user_state_path("AIOrganizer", "AIOrganizer")
    return root / "active-workspace.json"


def publish_active_workspace(path: Path) -> None:
    """Publish the last opened workspace for automatically configured local MCP hosts."""
    target = active_workspace_file()
    payload = json.dumps(
        {"workspace": str(path.resolve(strict=False))}, ensure_ascii=False, indent=2
    )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(target)
    except OSError:
        # The desktop workspace remains usable when a locked-down host blocks this convenience file.
        return


def read_active_workspace() -> Path | None:
    try:
        payload = json.loads(active_workspace_file().read_text(encoding="utf-8"))
        path = Path(str(payload.get("workspace", ""))).resolve(strict=False)
        return path if path.suffix == ".aioworkspace" and path.is_file() else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
