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
    LOCAL_ONLY = "local_only"
    METADATA_ONLY = "metadata_only"
    CLOUD_TEXT = "cloud_text"
    TEXT_AND_IMAGES = "text_and_images"


class Sensitivity(StrEnum):
    NORMAL = "normal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class FolderRole(StrEnum):
    INBOX = "inbox"
    DOWNLOADS = "downloads"
    DESTINATION = "destination"
    ARCHIVE = "archive"
    PROTECTED = "protected"
    EXCLUDED = "excluded"


class TagFacet(StrEnum):
    CONTENT = "content"
    LIFECYCLE = "lifecycle"
    STATE = "state"
    ORIGIN = "origin"
    TECHNOLOGY = "technology"
    AUDIENCE = "audience"


class ProposalKind(StrEnum):
    RENAME = "rename"
    FOLDER = "folder"
    MOVE = "move"
    FINDING = "finding"
    CLEANUP = "cleanup"


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
    tag_ids: set[str] = field(default_factory=set)
    cloud_policy: CloudPolicy = CloudPolicy.NONE
    exclusions: list[str] = field(default_factory=list)
    max_hierarchy_depth: int | None = None
    capabilities: RootCapabilities | None = None
    policy_revision: int = 1


@dataclass(slots=True)
class CategoryDefinition:
    name: str
    id: str = field(default_factory=lambda: new_id("cat"))
    parent_id: str | None = None
    description: str = ""
    semantic_key: str = ""
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
    max_hierarchy_depth: int = 3
    permitted_kinds: set[str] = field(default_factory=set)
    default_tag_ids: set[str] = field(default_factory=set)
    suggest_as_folder: bool = False
    revision: int = 1


@dataclass(slots=True)
class TagDefinition:
    name: str
    facet: TagFacet
    id: str = field(default_factory=lambda: new_id("tag"))
    key: str = ""
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    guidance: str = ""
    applies_to: set[str] = field(default_factory=lambda: {"file", "folder", "software", "email"})
    mutually_exclusive_within_facet: bool = False
    revision: int = 1


@dataclass(slots=True)
class TagAssignment:
    entity_kind: str
    entity_key: str
    tag_id: str
    id: str = field(default_factory=lambda: new_id("tag_assignment"))
    source: str = "user"
    confidence: float = 1.0
    approved: bool = True
    source_fingerprint: str = ""
    revision: int = 1


@dataclass(slots=True)
class CategoryAssignment:
    path: Path
    category_ids: set[str]
    roles: set[FolderRole]
    tag_ids: set[str] = field(default_factory=set)
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
    created_ns: int | None
    file_id: str | None
    mime_type: str
    name: str = ""
    extension: str = ""
    parent_path: str = ""
    is_dir: bool = False
    is_placeholder: bool = False
    is_project_root: bool = False
    inside_protected_project: bool = False
    protected_project_path: str = ""
    project_markers: tuple[str, ...] = ()
    has_build_outputs: bool = False
    has_virtual_environment: bool = False
    has_nested_repositories: bool = False
    child_file_count: int = 0
    child_folder_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
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
    confidence_route: str = "needs_review"
    content_classes: list[str] = field(default_factory=lambda: ["metadata"])
    extractor_version: str = "1"


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
