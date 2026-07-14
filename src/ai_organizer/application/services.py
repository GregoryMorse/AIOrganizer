from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ai_organizer.domain.models import ItemSnapshot, SourceRoot, new_id

from .ports import InventoryPort


@dataclass(frozen=True, slots=True)
class InventoryRun:
    id: str
    root_id: str
    items: tuple[ItemSnapshot, ...]


class InventoryService:
    def __init__(self, scanner: InventoryPort) -> None:
        self._scanner = scanner

    def scan_root(self, root: SourceRoot) -> InventoryRun:
        capabilities = self._scanner.capabilities(root.path)
        if not capabilities.reachable:
            raise FileNotFoundError(root.path)
        root.capabilities = capabilities
        items = self._scanner.scan(root.id, root.path, root.exclusions)
        return InventoryRun(new_id("snapshot"), root.id, tuple(items))

    @staticmethod
    def validate_non_overlapping(roots: Sequence[SourceRoot]) -> None:
        resolved = [(root, root.path.resolve(strict=False)) for root in roots]
        for index, (left, left_path) in enumerate(resolved):
            for right, right_path in resolved[index + 1 :]:
                overlap_allowed = False
                if left_path in right_path.parents:
                    overlap_allowed = _excluded_nested(left, right_path.relative_to(left_path))
                elif right_path in left_path.parents:
                    overlap_allowed = _excluded_nested(right, left_path.relative_to(right_path))
                if (
                    left_path == right_path
                    or left_path in right_path.parents
                    or right_path in left_path.parents
                ) and not overlap_allowed:
                    raise ValueError(
                        f"Source roots overlap: {left.name} ({left_path}) and {right.name} ({right_path})"
                    )


def _excluded_nested(parent: SourceRoot, relative: Path) -> bool:
    path = str(relative).replace("\\", "/")
    return any(
        fnmatch.fnmatch(path.casefold(), pattern.casefold())
        or fnmatch.fnmatch(f"{path}/", pattern.casefold().rstrip("*") + "*")
        for pattern in parent.exclusions
    )
