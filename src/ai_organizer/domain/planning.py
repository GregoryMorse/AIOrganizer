from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .models import FrozenPlan, ProposalSet, ProposalStatus, new_id


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    message: str
    item_id: str | None = None


@dataclass(frozen=True, slots=True)
class ValidationReport:
    issues: tuple[ValidationIssue, ...]

    @property
    def ready(self) -> bool:
        return not self.issues


class RenamePlanner:
    def validate(self, proposals: ProposalSet, root: Path) -> ValidationReport:
        issues: list[ValidationIssue] = []
        targets: dict[str, str] = {}
        root_resolved = root.resolve(strict=False)
        for item in proposals.items:
            if item.status != ProposalStatus.ACCEPTED:
                continue
            target = (root / item.proposed_value).resolve(strict=False)
            if root_resolved != target and root_resolved not in target.parents:
                issues.append(ValidationIssue("root_escape", "Target escapes source root", item.id))
                continue
            key = str(target).casefold()
            if key in targets:
                issues.append(
                    ValidationIssue("duplicate_target", "Two proposals share a target", item.id)
                )
            targets[key] = item.id
            if target.exists() and item.current_value.casefold() != item.proposed_value.casefold():
                issues.append(ValidationIssue("target_exists", "Target already exists", item.id))
            if item.issues:
                issues.append(ValidationIssue("proposal_issue", "; ".join(item.issues), item.id))
        return ValidationReport(tuple(issues))

    def freeze(
        self, proposals: ProposalSet, root: Path, category_policy_revision: int
    ) -> FrozenPlan:
        report = self.validate(proposals, root)
        if not report.ready:
            raise ValueError("Cannot freeze an invalid proposal set")
        operations = tuple(
            {
                "kind": str(item.kind),
                "item_id": item.item_id,
                "source": str(root / item.current_value),
                "target": str(root / item.proposed_value),
            }
            for item in proposals.items
            if item.status == ProposalStatus.ACCEPTED
        )
        return FrozenPlan(
            new_id("plan"),
            proposals.id,
            proposals.revision,
            proposals.prompt_hash,
            category_policy_revision,
            operations,
        )
