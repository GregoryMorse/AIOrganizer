from __future__ import annotations

import os
from pathlib import Path


def load_development_env(path: Path | None = None) -> Path | None:
    """Load a local .env without replacing explicitly configured environment values."""
    candidate = path or _project_env_path()
    if not candidate.is_file():
        return None
    for raw_line in candidate.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if not name or not name.replace("_", "").isalnum() or name[0].isdigit():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(name, value)
    return candidate


def _project_env_path() -> Path:
    working = Path.cwd() / ".env"
    if working.is_file():
        return working
    return Path(__file__).resolve().parents[3] / ".env"
