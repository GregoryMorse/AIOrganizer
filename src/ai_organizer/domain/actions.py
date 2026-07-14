from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar

from .models import ActionOutputMode, new_id, utc_now


class FilterOperator(StrEnum):
    EQUALS = "equals"
    IN = "in"
    CONTAINS = "contains"
    GLOB = "glob"
    GREATER_THAN = "greater_than"
    LESS_THAN = "less_than"
    EXISTS = "exists"


@dataclass(frozen=True, slots=True)
class ActionFilter:
    field: str
    operator: FilterOperator
    value: Any = None


@dataclass(slots=True)
class ActionPreset:
    name: str
    description: str
    filters: list[ActionFilter]
    guidance: str
    id: str = field(default_factory=lambda: new_id("action"))
    security_oriented: bool = False
    default_output: ActionOutputMode = ActionOutputMode.FINDINGS
    max_results: int = 500
    allowed_destination_category_ids: set[str] = field(default_factory=set)
    builtin: bool = False
    revision: int = 1


@dataclass(frozen=True, slots=True)
class Finding:
    item_id: str
    title: str
    rationale: str
    confidence: float
    severity: str = "info"
    id: str = field(default_factory=lambda: new_id("finding"))


@dataclass(frozen=True, slots=True)
class FindingSet:
    action_run_id: str
    findings: tuple[Finding, ...]
    id: str = field(default_factory=lambda: new_id("finding_set"))


@dataclass(frozen=True, slots=True)
class ActionRun:
    preset_id: str
    preset_revision: int
    output_mode: ActionOutputMode
    scope: str
    max_results: int
    provider: str = "local"
    model: str = "deterministic"
    prompt_hash: str = ""
    id: str = field(default_factory=lambda: new_id("action_run"))
    created_at: str = field(default_factory=utc_now)


class ActionEngine:
    ALLOWED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "category_ids",
            "roles",
            "root_id",
            "mime_type",
            "extension",
            "size",
            "modified_at",
            "language",
            "sensitivity",
            "confidence",
            "project_status",
            "relative_path",
            "evidence_kinds",
            "secret_like",
        }
    )

    def validate(self, preset: ActionPreset) -> None:
        if not preset.name.strip() or len(preset.name) > 120:
            raise ValueError("Action name is required and limited to 120 characters")
        if len(preset.guidance) > 8_000:
            raise ValueError("Action guidance exceeds 8,000 characters")
        for action_filter in preset.filters:
            if action_filter.field not in self.ALLOWED_FIELDS:
                raise ValueError(f"Unsupported filter field: {action_filter.field}")
            if callable(action_filter.value):
                raise ValueError("Executable filter values are forbidden")
            serialized = str(action_filter.value)
            if re.search(
                r"(\{\{|<script\b|file://|powershell(?:\.exe)?\b|/bin/(?:sh|bash)\b)",
                serialized,
                re.IGNORECASE,
            ):
                raise ValueError("Executable or filesystem expressions are forbidden")

    def evaluate(
        self,
        preset: ActionPreset,
        items: Iterable[dict[str, Any]],
        run: ActionRun,
    ) -> FindingSet:
        self.validate(preset)
        if run.scope not in {
            "workspace",
            "selected roots",
            "selected folders",
            "current selection",
        }:
            raise ValueError("Unsupported action scope")
        findings: list[Finding] = []
        limit = min(run.max_results, preset.max_results, 5_000)
        for item in items:
            if all(self._matches(item, condition) for condition in preset.filters):
                findings.append(
                    Finding(
                        item_id=str(item["id"]),
                        title=preset.name,
                        rationale=preset.description,
                        confidence=float(item.get("confidence", 0.75)),
                        severity="warning" if preset.security_oriented else "info",
                    )
                )
                if len(findings) >= limit:
                    break
        return FindingSet(run.id, tuple(findings))

    def _matches(self, item: dict[str, Any], condition: ActionFilter) -> bool:
        actual = item.get(condition.field)
        expected = condition.value
        match condition.operator:
            case FilterOperator.EXISTS:
                return actual is not None
            case FilterOperator.EQUALS:
                return bool(actual == expected)
            case FilterOperator.IN:
                if isinstance(actual, (set, list, tuple)):
                    return bool(set(actual).intersection(set(expected)))
                return bool(actual in expected)
            case FilterOperator.CONTAINS:
                return str(expected).casefold() in str(actual).casefold()
            case FilterOperator.GLOB:
                return fnmatch.fnmatch(str(actual).casefold(), str(expected).casefold())
            case FilterOperator.GREATER_THAN:
                return bool(actual is not None and actual > expected)
            case FilterOperator.LESS_THAN:
                return bool(actual is not None and actual < expected)
        return False


def builtin_actions() -> list[ActionPreset]:
    return [
        ActionPreset(
            "Find misplaced personal information",
            "Personal information located outside approved personal/protected destinations.",
            [ActionFilter("sensitivity", FilterOperator.IN, ["confidential", "restricted"])],
            "Identify likely personal material and explain the evidence without revealing secrets.",
            security_oriented=True,
            builtin=True,
        ),
        ActionPreset(
            "Find sensitive files in unprotected roots",
            "Sensitive evidence in roots that do not carry the Protected role.",
            [ActionFilter("secret_like", FilterOperator.EQUALS, True)],
            "Find secret-like material. Redact values and report only type and location evidence.",
            security_oriented=True,
            builtin=True,
        ),
        ActionPreset(
            "Find code projects outside Code destinations",
            "Detected project roots not assigned to an approved Code destination.",
            [ActionFilter("project_status", FilterOperator.EQUALS, "project_root")],
            "Treat each project as one atomic bundle and do not propose internal file moves.",
            builtin=True,
        ),
        ActionPreset(
            "Find research material outside Research destinations",
            "Research-like material whose current category conflicts with its location.",
            [ActionFilter("category_ids", FilterOperator.IN, ["Research"])],
            "Prefer stable research destinations and preserve established project groupings.",
            builtin=True,
        ),
        ActionPreset(
            "Find financial and tax documents",
            "Financial/tax material outside protected personal destinations.",
            [ActionFilter("relative_path", FilterOperator.GLOB, "*tax*")],
            "Classify conservatively and mask account or taxpayer identifiers.",
            security_oriented=True,
            builtin=True,
        ),
        ActionPreset(
            "Find files stranded in inbox roots",
            "Items in roots assigned the Inbox role.",
            [ActionFilter("roles", FilterOperator.IN, ["inbox"])],
            "Suggest only eligible configured destinations.",
            builtin=True,
        ),
        ActionPreset(
            "Find likely credentials or private keys",
            "Secret-bearing files requiring immediate review.",
            [ActionFilter("secret_like", FilterOperator.EQUALS, True)],
            "Never reproduce secret values. Return redacted finding types only.",
            security_oriented=True,
            builtin=True,
        ),
        ActionPreset(
            "Find uncategorized items",
            "Items with no approved category assignment.",
            [ActionFilter("category_ids", FilterOperator.EQUALS, [])],
            "Suggest categories first; do not route until category assignments are approved.",
            builtin=True,
        ),
        ActionPreset(
            "Find category-location conflicts",
            "Items whose evidence category conflicts with their effective folder category.",
            [ActionFilter("confidence", FilterOperator.GREATER_THAN, 0.75)],
            "Explain the conflict and rank only policy-eligible destinations.",
            builtin=True,
        ),
        ActionPreset(
            "Find likely inactive archives",
            "Old archive-like items outside Archive destinations.",
            [ActionFilter("relative_path", FilterOperator.GLOB, "*archive*")],
            "Prefer Archive-role destinations and preserve bundle integrity.",
            builtin=True,
        ),
    ]
