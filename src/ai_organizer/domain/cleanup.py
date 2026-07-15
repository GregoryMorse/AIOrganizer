from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CleanupKind(StrEnum):
    EMPTY_FOLDER = "empty_folder"
    BUILD_ARTIFACT = "build_artifact"
    ABANDONED_PARTIAL = "abandoned_partial"


class CleanupDestination(StrEnum):
    QUARANTINE = "aiorganizer_quarantine"


@dataclass(frozen=True, slots=True)
class CleanupCandidate:
    item_id: str
    root_id: str
    relative_path: str
    kind: CleanupKind
    total_size: int
    item_count: int
    derivation: str
    regeneration_evidence: tuple[str, ...]
    exclusions: tuple[str, ...]
    destination: CleanupDestination = CleanupDestination.QUARANTINE
    selected_by_default: bool = False

    @property
    def ready(self) -> bool:
        if self.kind == CleanupKind.BUILD_ARTIFACT:
            return bool(self.regeneration_evidence)
        return True
