from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from ai_organizer.domain.models import Evidence, ItemSnapshot, RootCapabilities
from ai_organizer.domain.prompts import CompiledPrompt


class InventoryPort(Protocol):
    def capabilities(self, root: Path) -> RootCapabilities: ...

    def scan(self, root_id: str, root: Path, exclusions: Sequence[str]) -> list[ItemSnapshot]: ...


class ExtractionPort(Protocol):
    def extract(self, path: Path, item: ItemSnapshot) -> Evidence: ...


class AnalysisResultPort(Protocol):
    findings: list[dict[str, object]]
    usage: dict[str, int]


class AnalysisProvider(Protocol):
    name: str

    def estimate(self, prompt: CompiledPrompt) -> dict[str, int]: ...

    def analyze(self, prompt: CompiledPrompt) -> AnalysisResultPort: ...
