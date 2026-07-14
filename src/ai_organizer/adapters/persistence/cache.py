from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from platformdirs import user_cache_path


class WorkspaceCache:
    """External derived-data cache; workspace databases contain references only."""

    def __init__(self, workspace_id: str, base: Path | None = None) -> None:
        if not workspace_id.startswith("workspace_"):
            raise ValueError("Invalid workspace identifier")
        self.root = (base or user_cache_path("AIOrganizer")) / "workspaces" / workspace_id
        self.root.mkdir(parents=True, exist_ok=True)

    def artifact_path(self, item_id: str, evidence_hash: str, kind: str, suffix: str) -> Path:
        if not item_id.startswith("item_") or not evidence_hash:
            raise ValueError("Cache keys must use opaque item and evidence identifiers")
        safe_kind = "".join(
            character for character in kind if character.isalnum() or character in "-_"
        )
        if not safe_kind:
            raise ValueError("Invalid cache artifact kind")
        key = hashlib.sha256(f"{item_id}:{evidence_hash}:{safe_kind}".encode()).hexdigest()
        folder = self.root / key[:2] / key[2:]
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{safe_kind}{suffix}"

    def clear(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
