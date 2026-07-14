from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class CloudPolicy(StrEnum):
    INHERIT = "inherit"
    NONE = "none"
    TEXT_AND_IMAGES = "text_and_images"


class Sensitivity(StrEnum):
    NORMAL = "normal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class FolderRole(StrEnum):
    INBOX = "inbox"
    DESTINATION = "destination"
    ARCHIVE = "archive"
    PROTECTED = "protected"
    EXCLUDED = "excluded"


class ProposalKind(StrEnum):
    RENAME = "rename"
    FOLDER = "folder"
    MOVE = "move"
    FINDING = "finding"


class ProposalStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EDITED = "edited"
    NEEDS_REVIEW = "needs_review"
    BLOCKED = "blocked"
    STALE = "stale"


class PlanState(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    EXECUTING = "executing"
    VERIFIED = "verified"
    PARTIALLY_FAILED = "partially_failed"
    ROLLED_BACK = "rolled_back"
    COMPLETED = "completed"


class ActionOutputMode(StrEnum):
    FINDINGS = "findings"
    MOVE_PROPOSALS = "move_proposals"


@dataclass(frozen=True, slots=True)
class RootCapabilities:
    reachable: bool
    writable: bool
    case_sensitive: bool
    supports_rename: bool
    stable_file_id: bool
    volume_id: str
    is_network: bool = False
    is_removable: bool = False
    supports_placeholders: bool = False


@dataclass(slots=True)
class SourceRoot:
    path: Path
    name: str
    id: str = field(default_factory=lambda: new_id("root"))
    roles: set[FolderRole] = field(default_factory=set)
    category_ids: set[str] = field(default_factory=set)
    cloud_policy: CloudPolicy = CloudPolicy.NONE
    exclusions: list[str] = field(default_factory=list)
    capabilities: RootCapabilities | None = None
    policy_revision: int = 1


@dataclass(slots=True)
class CategoryDefinition:
    name: str
    id: str = field(default_factory=lambda: new_id("cat"))
    parent_id: str | None = None
    description: str = ""
    examples: list[str] = field(default_factory=list)
    guidance: str = ""
    sensitivity: Sensitivity = Sensitivity.NORMAL
    cloud_policy: CloudPolicy = CloudPolicy.INHERIT
    default_naming_profile: str | None = None
    allowed_destination_category_ids: set[str] = field(default_factory=set)
    allowed_roles: set[FolderRole] = field(
        default_factory=lambda: {FolderRole.DESTINATION, FolderRole.ARCHIVE}
    )
    preferred_destinations: dict[str, int] = field(default_factory=dict)
    max_hierarchy_depth: int = 4
    permitted_kinds: set[str] = field(default_factory=set)
    revision: int = 1


@dataclass(slots=True)
class CategoryAssignment:
    path: Path
    category_ids: set[str]
    roles: set[FolderRole]
    id: str = field(default_factory=lambda: new_id("assign"))
    inherited: bool = True
    override_roles: bool = False
    approved: bool = True
    revision: int = 1


@dataclass(frozen=True, slots=True)
class ItemSnapshot:
    id: str
    root_id: str
    relative_path: str
    size: int
    modified_ns: int
    file_id: str | None
    mime_type: str
    is_dir: bool = False
    is_placeholder: bool = False
    is_project_root: bool = False
    project_markers: tuple[str, ...] = ()
    has_build_outputs: bool = False
    has_virtual_environment: bool = False
    has_nested_repositories: bool = False
    sha256: str | None = None


@dataclass(slots=True)
class Evidence:
    item_id: str
    kind: str
    summary: str
    id: str = field(default_factory=lambda: new_id("evidence"))
    language_candidates: list[tuple[str, float]] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    provenance: str = "local"
    created_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class ProposalItem:
    item_id: str
    proposed_value: str
    kind: ProposalKind
    id: str = field(default_factory=lambda: new_id("proposal_item"))
    current_value: str = ""
    status: ProposalStatus = ProposalStatus.PROPOSED
    confidence: float = 0.0
    rationale: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProposalSet:
    kind: ProposalKind
    snapshot_id: str
    id: str = field(default_factory=lambda: new_id("proposal_set"))
    revision: int = 1
    prompt_hash: str = ""
    category_policy_revision: int = 0
    provider: str = "local"
    model: str = ""
    action_run_id: str | None = None
    items: list[ProposalItem] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class FrozenPlan:
    id: str
    proposal_set_id: str
    proposal_revision: int
    prompt_hash: str
    category_policy_revision: int
    operations: tuple[dict[str, Any], ...]
    state: PlanState = PlanState.READY
    created_at: str = field(default_factory=utc_now)
