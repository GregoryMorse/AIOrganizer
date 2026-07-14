from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from .models import (
    CategoryAssignment,
    CategoryDefinition,
    CloudPolicy,
    FolderRole,
    Sensitivity,
)


@dataclass(frozen=True, slots=True)
class EffectivePolicy:
    category_ids: frozenset[str]
    roles: frozenset[FolderRole]
    cloud_policy: CloudPolicy
    sensitivity: Sensitivity
    max_hierarchy_depth: int
    allowed_destination_category_ids: frozenset[str]
    policy_revision: int


@dataclass(frozen=True, slots=True)
class DestinationCandidate:
    path: Path
    category_ids: frozenset[str]
    roles: frozenset[FolderRole]
    writable: bool = True
    reachable: bool = True
    protected_project_internal: bool = False
    priority: int = 0
    existing_consistency: float = 0.0


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    path: Path | None
    score: float
    rationale: str
    blocked_reasons: tuple[str, ...] = field(default_factory=tuple)


class CategoryResolver:
    _cloud_rank: ClassVar[dict[CloudPolicy, int]] = {
        CloudPolicy.NONE: 0,
        CloudPolicy.INHERIT: 1,
        CloudPolicy.TEXT_AND_IMAGES: 2,
    }
    _sensitivity_rank: ClassVar[dict[Sensitivity, int]] = {
        Sensitivity.NORMAL: 0,
        Sensitivity.CONFIDENTIAL: 1,
        Sensitivity.RESTRICTED: 2,
    }

    def effective_policy(
        self,
        path: Path,
        assignments: Iterable[CategoryAssignment],
        categories: dict[str, CategoryDefinition],
        root_cloud_policy: CloudPolicy,
    ) -> EffectivePolicy:
        resolved = path.resolve(strict=False)
        applicable = [
            a
            for a in assignments
            if a.approved
            and (
                resolved == a.path.resolve(strict=False)
                or a.path.resolve(strict=False) in resolved.parents
            )
        ]
        applicable.sort(key=lambda assignment: len(assignment.path.parts))
        category_ids: set[str] = set()
        roles: set[FolderRole] = set()
        revision = 0
        for assignment in applicable:
            category_ids.update(assignment.category_ids)
            if assignment.override_roles:
                roles = set(assignment.roles)
            else:
                roles.update(assignment.roles)
            revision = max(revision, assignment.revision)
        definitions = [categories[cid] for cid in category_ids if cid in categories]
        sensitivity = max(
            (definition.sensitivity for definition in definitions),
            key=lambda value: self._sensitivity_rank[value],
            default=Sensitivity.NORMAL,
        )
        explicit_cloud = [
            definition.cloud_policy
            for definition in definitions
            if definition.cloud_policy != CloudPolicy.INHERIT
        ]
        cloud = root_cloud_policy
        if explicit_cloud:
            cloud = min(
                [*explicit_cloud, root_cloud_policy], key=lambda value: self._cloud_rank[value]
            )
        depth = min((definition.max_hierarchy_depth for definition in definitions), default=4)
        restrictions = [
            definition.allowed_destination_category_ids
            for definition in definitions
            if definition.allowed_destination_category_ids
        ]
        allowed = (
            set.intersection(*(set(values) for values in restrictions)) if restrictions else set()
        )
        revision = max(
            [revision, *(definition.revision for definition in definitions)], default=revision
        )
        return EffectivePolicy(
            frozenset(category_ids),
            frozenset(roles),
            cloud,
            sensitivity,
            depth,
            frozenset(allowed),
            revision,
        )


class DestinationRouter:
    def rank(
        self,
        item_policy: EffectivePolicy,
        candidates: Iterable[DestinationCandidate],
    ) -> list[RoutingDecision]:
        results: list[RoutingDecision] = []
        for candidate in candidates:
            reasons: list[str] = []
            if not candidate.reachable:
                reasons.append("destination is unreachable")
            if not candidate.writable:
                reasons.append("destination is not writable")
            if not candidate.roles.intersection({FolderRole.DESTINATION, FolderRole.ARCHIVE}):
                reasons.append("destination lacks Destination or Archive role")
            if FolderRole.EXCLUDED in candidate.roles:
                reasons.append("destination is excluded")
            if candidate.protected_project_internal:
                reasons.append("destination is inside a protected project")
            if (
                item_policy.allowed_destination_category_ids
                and not candidate.category_ids.intersection(
                    item_policy.allowed_destination_category_ids
                )
            ):
                reasons.append("destination category is not allowed")
            if (
                item_policy.sensitivity == Sensitivity.RESTRICTED
                and FolderRole.PROTECTED not in candidate.roles
            ):
                reasons.append("restricted content requires a Protected destination")
            if reasons:
                results.append(RoutingDecision(None, 0, "Ineligible destination", tuple(reasons)))
                continue
            exact = len(item_policy.category_ids.intersection(candidate.category_ids))
            score = exact * 100 + candidate.priority * 10 + candidate.existing_consistency * 20
            results.append(
                RoutingDecision(
                    candidate.path,
                    score,
                    f"{exact} category matches; priority {candidate.priority}",
                )
            )
        return sorted(results, key=lambda decision: decision.score, reverse=True)
