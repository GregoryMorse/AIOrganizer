from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath


class HierarchyAction(StrEnum):
    UNCHANGED = "unchanged"
    CREATE = "create"
    RENAME = "rename"
    DESCENDANT = "projected_descendant"


@dataclass(frozen=True, slots=True)
class HierarchyChange:
    projected_path: str
    current_path: str | None = None
    category_id: str | None = None


@dataclass(frozen=True, slots=True)
class UnionHierarchyRow:
    root_id: str
    current_path: str | None
    projected_path: str | None
    action: HierarchyAction
    status: str
    depth: int
    issues: tuple[str, ...] = ()
    category_id: str | None = None
    ghost: bool = False


@dataclass(frozen=True, slots=True)
class HierarchyProjection:
    rows: tuple[UnionHierarchyRow, ...]

    @property
    def ready(self) -> bool:
        return not any(row.issues for row in self.rows)

    @property
    def issues(self) -> tuple[str, ...]:
        return tuple(issue for row in self.rows for issue in row.issues)


class UnionHierarchyPlanner:
    """Build one aligned logical row set for current and projected folder trees."""

    def project(
        self,
        root_id: str,
        current_paths: set[str],
        changes: list[HierarchyChange],
        *,
        case_sensitive: bool,
        windows_rules: bool,
    ) -> HierarchyProjection:
        current = {_normalize(path) for path in current_paths}
        rename_changes = [change for change in changes if change.current_path]
        create_changes = [change for change in changes if not change.current_path]
        rows: list[UnionHierarchyRow] = []
        rename_map: dict[str, HierarchyChange] = {}
        global_issues: list[str] = []
        for change in rename_changes:
            source = _normalize(change.current_path or "")
            if source not in current:
                global_issues.append(f"Rename source is not in the current hierarchy: {source}")
                continue
            target = _normalize(change.projected_path)
            if PurePosixPath(source).parent != PurePosixPath(target).parent:
                global_issues.append(f"Folder rename may not reparent {source} to {target}")
                continue
            rename_map[source] = HierarchyChange(target, source, change.category_id)
        rename_sources = sorted(rename_map, key=lambda value: len(PurePosixPath(value).parts))
        for index, source in enumerate(rename_sources):
            for other in rename_sources[index + 1 :]:
                if PurePosixPath(source) in PurePosixPath(other).parents:
                    global_issues.append(
                        f"Nested folder renames must be split into separate plans: {source}, {other}"
                    )

        projected_by_current: dict[str, str] = {}
        for path in current:
            matching = [source for source in rename_sources if source == path or PurePosixPath(source) in PurePosixPath(path).parents]
            if matching:
                source = max(matching, key=lambda value: len(PurePosixPath(value).parts))
                relative = PurePosixPath(path).relative_to(PurePosixPath(source))
                projected_path = PurePosixPath(rename_map[source].projected_path) / relative
                projected_by_current[path] = projected_path.as_posix()
            else:
                projected_by_current[path] = path

        projected_keys: dict[str, list[str]] = {}
        for source, projected_value in projected_by_current.items():
            projected_keys.setdefault(_key(projected_value, case_sensitive), []).append(source)
        for change in create_changes:
            created_path = _normalize(change.projected_path)
            projected_keys.setdefault(_key(created_path, case_sensitive), []).append("<new>")

        collisions = {
            key: values for key, values in projected_keys.items() if len(values) > 1
        }
        explicit_creates = {_normalize(change.projected_path) for change in create_changes}
        future_paths = set(projected_by_current.values()) | explicit_creates

        for source in sorted(current, key=_sort_path):
            projected_value = projected_by_current[source]
            exact_change = rename_map.get(source)
            action = (
                HierarchyAction.RENAME
                if exact_change
                else HierarchyAction.DESCENDANT
                if projected_value != source
                else HierarchyAction.UNCHANGED
            )
            issues = _path_issues(projected_value, windows_rules)
            if _key(projected_value, case_sensitive) in collisions:
                issues.append(f"Projected folder collision: {projected_value}")
            rows.append(
                UnionHierarchyRow(
                    root_id,
                    source,
                    projected_value,
                    action,
                    "blocked" if issues else "ready" if action != HierarchyAction.UNCHANGED else "aligned",
                    len(PurePosixPath(projected_value).parts),
                    tuple(issues),
                    exact_change.category_id if exact_change else None,
                )
            )

        for change in sorted(create_changes, key=lambda value: _sort_path(value.projected_path)):
            created_path = _normalize(change.projected_path)
            issues = _path_issues(created_path, windows_rules)
            parent = PurePosixPath(created_path).parent.as_posix()
            if parent != "." and parent not in future_paths:
                issues.append(f"Projected parent is absent from the plan: {parent}")
            if _key(created_path, case_sensitive) in collisions:
                issues.append(f"Projected folder collision: {created_path}")
            rows.append(
                UnionHierarchyRow(
                    root_id,
                    None,
                    created_path,
                    HierarchyAction.CREATE,
                    "blocked" if issues else "ready",
                    len(PurePosixPath(created_path).parts),
                    tuple(issues),
                    change.category_id,
                    True,
                )
            )

        if global_issues:
            rows.append(
                UnionHierarchyRow(
                    root_id,
                    None,
                    None,
                    HierarchyAction.UNCHANGED,
                    "blocked",
                    0,
                    tuple(global_issues),
                )
            )
        rows.sort(key=lambda row: _sort_path(row.projected_path or row.current_path or ""))
        return HierarchyProjection(tuple(rows))


_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_WINDOWS_INVALID = re.compile(r'[<>:"|?*]')


def _path_issues(path: str, windows_rules: bool) -> list[str]:
    issues: list[str] = []
    parts = PurePosixPath(path).parts
    if not parts or path in {"", "."} or any(part in {"", ".", ".."} for part in parts):
        return [f"Invalid relative folder path: {path!r}"]
    for part in parts:
        if len(part) > 255:
            issues.append(f"Folder component exceeds 255 characters: {part[:40]}")
        if windows_rules:
            stem = part.rstrip(". ").split(".", 1)[0].upper()
            if _WINDOWS_INVALID.search(part) or part.endswith((".", " ")):
                issues.append(f"Folder name is invalid on Windows: {part}")
            if stem in _WINDOWS_RESERVED:
                issues.append(f"Folder name is reserved on Windows: {part}")
    maximum = 240 if windows_rules else 4_096
    if len(path) > maximum:
        issues.append(f"Projected relative path exceeds the {maximum}-character policy")
    return issues


def _normalize(path: str) -> str:
    value = path.replace("\\", "/").strip()
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise ValueError(f"Invalid relative hierarchy path: {path!r}")
    value = value.rstrip("/")
    candidate = PurePosixPath(value)
    if not value or candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise ValueError(f"Invalid relative hierarchy path: {path!r}")
    return candidate.as_posix()


def _key(path: str, case_sensitive: bool) -> str:
    return path if case_sensitive else path.casefold()


def _sort_path(path: str) -> tuple[int, str]:
    normalized = path.replace("\\", "/")
    return len(PurePosixPath(normalized).parts), normalized.casefold()
