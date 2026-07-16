from __future__ import annotations

import json
import os
import platform
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from ai_organizer.adapters.defender_status import DefenderStatusScanner
from ai_organizer.adapters.filesystem import (
    CleanupAnalyzer,
    CleanupRequest,
    FileOperationEngine,
    FileSystemInventory,
    FolderCreateRequest,
    Journal,
    MetadataIndexer,
    MoveRequest,
    RenameRequest,
    SnapshotToken,
    content_fingerprint,
    journal_to_dict,
    metadata_cache_compatible,
    metadata_fingerprint,
)
from ai_organizer.adapters.persistence import WorkspaceStore
from ai_organizer.adapters.secrets import SecretStore
from ai_organizer.adapters.software_inventory import SoftwareInventory
from ai_organizer.application.services import InventoryService
from ai_organizer.bootstrap.workspace_locator import publish_active_workspace
from ai_organizer.domain.actions import ActionRun, FindingSet, builtin_actions
from ai_organizer.domain.cleanup import CleanupKind
from ai_organizer.domain.evidence import EvidenceClass, ProviderRequestPreview, SelectionScope
from ai_organizer.domain.hierarchy import HierarchyChange, UnionHierarchyPlanner
from ai_organizer.domain.models import (
    CategoryAssignment,
    CategoryDefinition,
    CloudPolicy,
    FolderRole,
    FrozenPlan,
    PlanState,
    ProposalItem,
    ProposalKind,
    ProposalSet,
    ProposalStatus,
    Sensitivity,
    SourceRoot,
    TagAssignment,
    new_id,
)
from ai_organizer.domain.moves import MoveCandidate, ProjectedMoveValidator
from ai_organizer.domain.naming import valid_filename_proposal
from ai_organizer.domain.organization import (
    FolderDepthPolicy,
    general_organization_profile,
    recommend_folder_depth,
)
from ai_organizer.domain.prompts import (
    CompiledPrompt,
    PromptCompiler,
    PromptLayerKind,
    PromptRevision,
)
from ai_organizer.domain.recurrence import (
    Cadence,
    GapMatrix,
    GapStatus,
    RecurrenceException,
    RecurrenceSeries,
    SeriesCandidateBuilder,
    SeriesObservation,
    rebind_observations,
    recurrence_series_from_payload,
)
from ai_organizer.domain.semantic import SemanticRecord, semantic_fingerprint
from ai_organizer.domain.updates import UpdateAssessment


class WorkspaceController(QObject):
    workspace_changed = Signal()
    inventory_changed = Signal()
    activity_changed = Signal()
    prompt_changed = Signal(str)
    software_changed = Signal()
    recurrence_changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.store: WorkspaceStore | None = None
        self.scanner = FileSystemInventory()
        self.cleanup_analyzer = CleanupAnalyzer()
        self.metadata_indexer = MetadataIndexer()
        self.software_scanner = SoftwareInventory()
        self.defender_scanner = DefenderStatusScanner()
        self.series_builder = SeriesCandidateBuilder()
        self.sources: dict[str, SourceRoot] = {}
        self.items: list[dict[str, Any]] = []
        self.selected_item_ids: set[str] = set()
        self.selected_root_ids: set[str] = set()
        self.selected_folders: set[tuple[str, str]] = set()
        self.software_packages: list[dict[str, Any]] = []

    def create_workspace(self, path: Path, name: str) -> None:
        self.close()
        self.store = WorkspaceStore.create(path, name)
        self.install_general_organization_profile()
        for action in builtin_actions():
            self.store.save_action(action)
        self.store.activity("workspace.created", f"Created workspace {name}")
        publish_active_workspace(self.store.path)
        self.workspace_changed.emit()

    def open_workspace(self, path: Path) -> None:
        self.close()
        self.store = WorkspaceStore(path)
        self._load_sources()
        self.items = self.store.list_items()
        self.software_packages = self.store.list_software_inventory()
        publish_active_workspace(self.store.path)
        self.workspace_changed.emit()
        self.inventory_changed.emit()

    def close(self) -> None:
        if self.store:
            self.store.close()
        self.store = None
        self.sources.clear()
        self.items = []
        self.selected_item_ids.clear()
        self.selected_root_ids.clear()
        self.selected_folders.clear()
        self.software_packages = []

    def add_source(
        self,
        path: Path,
        roles: set[FolderRole] | None = None,
        cloud_policy: CloudPolicy = CloudPolicy.NONE,
        category_ids: set[str] | None = None,
        tag_ids: set[str] | None = None,
        exclusions: list[str] | None = None,
        max_hierarchy_depth: int | None = None,
    ) -> SourceRoot:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        candidate = SourceRoot(
            path.resolve(strict=True),
            path.name or str(path),
            roles=set() if roles is None else set(roles),
            cloud_policy=cloud_policy,
            category_ids=category_ids or set(),
            tag_ids=tag_ids or set(),
            exclusions=exclusions or [],
            max_hierarchy_depth=max_hierarchy_depth,
        )
        InventoryService.validate_non_overlapping([*self.sources.values(), candidate])
        candidate.capabilities = self.scanner.capabilities(candidate.path)
        self.sources[candidate.id] = candidate
        self.store.save_source(candidate)
        self.store.activity("source.added", f"Added source {candidate.name}")
        self.workspace_changed.emit()
        return candidate

    def source_is_classified(self, root_id: str) -> bool:
        source = self.sources.get(root_id)
        return bool(source and (source.roles or source.category_ids or source.tag_ids))

    def source_is_operational(self, root_id: str) -> bool:
        """Return whether a reviewed source may be offered to operational tools."""
        source = self.sources.get(root_id)
        return bool(
            source
            and self.source_is_classified(root_id)
            and FolderRole.EXCLUDED not in source.roles
        )

    def set_source_classification(
        self,
        root_id: str,
        category_ids: set[str],
        tag_ids: set[str],
        roles: set[FolderRole],
    ) -> None:
        """Replace one source's approved classification after explicit user review."""
        if not self.store:
            raise RuntimeError("Open a workspace first")
        source = self.sources.get(root_id)
        if source is None:
            raise ValueError("Unknown source root")
        known_categories = {str(value["id"]) for value in self.store.list_category_payloads()}
        known_tags = {str(value["id"]) for value in self.store.list_tag_definition_payloads()}
        if not category_ids <= known_categories:
            raise ValueError("Source classification contains an unknown category")
        if not tag_ids <= known_tags:
            raise ValueError("Source classification contains an unknown tag")
        if not category_ids and not tag_ids and not roles:
            raise ValueError("Source classification cannot be empty")
        source.category_ids = set(category_ids)
        source.tag_ids = set(tag_ids)
        source.roles = set(roles)
        source.policy_revision += 1
        self.store.save_source(source)
        self.store.mark_proposals_stale(
            {"folder", "move", "rename", "finding"},
            "Source classification changed",
        )
        self.store.activity(
            "source.classified",
            f"Approved categories, tags, and routing roles for {source.name}",
        )
        self.workspace_changed.emit()
        self.activity_changed.emit()

    def assign_folder_policy(
        self,
        root_id: str,
        relative_path: str,
        category_ids: set[str],
        roles: set[FolderRole],
        tag_ids: set[str] | None = None,
        *,
        override_roles: bool = False,
    ) -> None:
        """Assign policy to a source root itself or to one of its descendants."""
        if not self.store:
            raise RuntimeError("Open a workspace first")
        source = self.sources.get(root_id)
        if source is None:
            raise ValueError("Unknown source root")
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Folder policy path must remain inside its source")
        normalized = "" if relative.as_posix() == "." else relative.as_posix()
        approved_tags = set(tag_ids or ())
        if not normalized:
            source.category_ids.update(category_ids)
            source.tag_ids.update(approved_tags)
            if override_roles:
                source.roles = set(roles)
            else:
                source.roles.update(roles)
            source.policy_revision += 1
            self.store.save_source(source)
        else:
            self.store.save_assignment(
                CategoryAssignment(
                    source.path / normalized,
                    category_ids,
                    roles,
                    tag_ids=approved_tags,
                    override_roles=override_roles,
                )
            )
        self.store.mark_proposals_stale(
            {"folder", "move", "finding"}, "Folder assignment revision changed"
        )
        self.store.activity(
            "category.assignment", f"Assigned approved policy to {source.name}/{normalized}"
        )
        self.activity_changed.emit()

    def install_general_organization_profile(self) -> tuple[int, int]:
        """Merge durable general defaults without deleting user-created policy."""
        if not self.store:
            raise RuntimeError("Open a workspace first")
        profile = general_organization_profile()
        existing_tags = self.store.list_tag_definition_payloads()
        tags_by_key = {str(value.get("key", "")): value for value in existing_tags}
        tag_id_map: dict[str, str] = {}
        added_tags = 0
        for tag_template in profile.tags:
            existing = tags_by_key.get(tag_template.key)
            if existing:
                tag_id_map[tag_template.id] = str(existing["id"])
                continue
            self.store.save_tag_definition(tag_template)
            tag_id_map[tag_template.id] = tag_template.id
            added_tags += 1

        existing_categories = self.store.list_category_payloads()
        category_id_map: dict[str, str] = {}
        added_categories = 0
        for category_template in profile.categories:
            parent_id = category_id_map.get(category_template.parent_id or "")
            existing = next(
                (
                    value
                    for value in existing_categories
                    if str(value.get("semantic_key", "")) == category_template.semantic_key
                    or (
                        str(value.get("name", "")).casefold() == category_template.name.casefold()
                        and value.get("parent_id") == parent_id
                    )
                ),
                None,
            )
            mapped_tags = {
                tag_id_map.get(tag_id, tag_id) for tag_id in category_template.default_tag_ids
            }
            if existing:
                category_id_map[category_template.id] = str(existing["id"])
                upgraded = _category_from_payload(existing)
                changed = False
                if not upgraded.semantic_key:
                    upgraded.semantic_key = category_template.semantic_key
                    changed = True
                new_tags = upgraded.default_tag_ids | mapped_tags
                if new_tags != upgraded.default_tag_ids:
                    upgraded.default_tag_ids = new_tags
                    changed = True
                if category_template.suggest_as_folder and not upgraded.suggest_as_folder:
                    upgraded.suggest_as_folder = True
                    changed = True
                if changed:
                    upgraded.revision += 1
                    self.store.save_category(upgraded)
                continue
            category = replace(
                category_template,
                parent_id=parent_id,
                default_tag_ids=mapped_tags,
            )
            self.store.save_category(category)
            existing_categories.append(asdict(category))
            category_id_map[category_template.id] = category.id
            added_categories += 1

        self.set_folder_depth_policy(profile.depth_policy, mark_stale=False)
        if not self.latest_prompt_text("workspace:general"):
            self.store.save_prompt_revision(
                PromptRevision(
                    "workspace:general",
                    PromptLayerKind.WORKSPACE,
                    profile.workspace_guidance,
                )
            )
        self.store.set_meta("organization_profile", profile.key)
        self.store.activity(
            "organization.profile",
            f"Installed {profile.name}: {added_categories} categories and {added_tags} tags added",
        )
        return added_categories, added_tags

    def folder_depth_policy(self) -> FolderDepthPolicy:
        if not self.store:
            return FolderDepthPolicy()
        raw = self.store.get_meta("folder_depth_policy")
        if not raw:
            return FolderDepthPolicy()
        try:
            value = json.loads(raw)
            return FolderDepthPolicy(
                int(value.get("preferred_depth", 2)),
                int(value.get("maximum_depth", 3)),
                bool(value.get("adaptive", True)),
            ).validated()
        except (TypeError, ValueError, json.JSONDecodeError):
            return FolderDepthPolicy()

    def set_folder_depth_policy(
        self, policy: FolderDepthPolicy, *, mark_stale: bool = True
    ) -> None:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        policy = policy.validated()
        self.store.set_meta("folder_depth_policy", json.dumps(asdict(policy), sort_keys=True))
        if mark_stale:
            self.store.mark_proposals_stale(
                {"folder", "move"}, "Folder hierarchy depth policy changed"
            )
            self.store.activity(
                "organization.depth_policy",
                f"Preferred folder depth {policy.preferred_depth}; maximum {policy.maximum_depth}",
            )
            self.workspace_changed.emit()

    def folder_depth_limit(self, root_id: str, category_id: str = "") -> int:
        limit = self.folder_depth_policy().maximum_depth
        if self.store and category_id:
            category = next(
                (
                    value
                    for value in self.store.list_category_payloads()
                    if str(value["id"]) == category_id
                ),
                None,
            )
            if category:
                limit = min(limit, max(1, int(category.get("max_hierarchy_depth", 3))))
        return limit

    def folder_planning_context(self, root_ids: set[str]) -> dict[str, Any]:
        policy = self.folder_depth_policy()
        categories = self.store.list_category_payloads() if self.store else []
        category_tags = {
            str(value["id"]): set(value.get("default_tag_ids", [])) for value in categories
        }
        roots: dict[str, Any] = {}
        for root_id in sorted(root_ids):
            items = [value for value in self.items if str(value.get("root_id")) == root_id]
            folder_count = sum(bool(value.get("is_dir")) for value in items)
            root_tags = set(self.sources[root_id].tag_ids)
            for category_id in self.sources[root_id].category_ids:
                root_tags.update(category_tags.get(category_id, set()))
            roots[root_id] = {
                "item_count": len(items),
                "folder_count": folder_count,
                "preferred_depth": min(policy.preferred_depth, self.folder_depth_limit(root_id)),
                "recommended_depth": min(
                    recommend_folder_depth(len(items), folder_count, policy),
                    self.folder_depth_limit(root_id),
                ),
                "maximum_depth": self.folder_depth_limit(root_id),
                "category_ids": sorted(self.sources[root_id].category_ids),
                "tag_ids": sorted(root_tags),
            }
        tags = self.store.list_tag_definition_payloads() if self.store else []
        return {
            "policy": asdict(policy),
            "roots": roots,
            "categories": [
                {
                    "id": value["id"],
                    "name": value["name"],
                    "parent_id": value.get("parent_id"),
                    "semantic_key": value.get("semantic_key", ""),
                    "description": value.get("description", ""),
                    "suggest_as_folder": bool(value.get("suggest_as_folder")),
                }
                for value in categories
            ],
            "tags": [
                {
                    "id": value["id"],
                    "name": value["name"],
                    "facet": value["facet"],
                    "description": value.get("description", ""),
                }
                for value in tags
            ],
        }

    def inventory_items_with_tags(self) -> list[dict[str, Any]]:
        if not self.store:
            return [dict(value) for value in self.items]
        tags_by_item: dict[str, set[str]] = {}
        for assignment in self.store.list_tag_assignment_payloads("inventory"):
            if assignment.get("approved", True):
                tags_by_item.setdefault(str(assignment["entity_key"]), set()).add(
                    str(assignment["tag_id"])
                )
        return [
            {
                **value,
                "tag_ids": sorted(tags_by_item.get(self.inventory_tag_key(value), set())),
            }
            for value in self.items
        ]

    @staticmethod
    def inventory_tag_key(item: dict[str, Any]) -> str:
        stable = str(item.get("file_id") or "")
        identity = stable or str(item.get("relative_path", "")).replace("\\", "/")
        return f"{item.get('root_id', '')}:{identity}"

    def set_source_cloud_policy(self, root_id: str, policy: CloudPolicy) -> None:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        source = self.sources.get(root_id)
        if source is None:
            raise ValueError("Unknown source")
        source.cloud_policy = policy
        source.policy_revision += 1
        self.store.save_source(source)
        self.store.mark_proposals_stale(
            {"rename", "folder", "move", "finding"},
            "Source provider privacy policy changed",
        )
        self.store.activity(
            "source.privacy_policy",
            f"Changed provider privacy policy for {source.name} to {policy.value}",
        )
        self.workspace_changed.emit()
        self.activity_changed.emit()

    def assign_item_tags(self, item_ids: set[str], tag_ids: set[str]) -> int:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        known_items = {str(value["id"]) for value in self.items}
        known_tags = {str(value["id"]) for value in self.store.list_tag_definition_payloads()}
        if not item_ids <= known_items or not tag_ids <= known_tags:
            raise ValueError("Unknown inventory item or tag")
        count = 0
        item_lookup = {str(value["id"]): value for value in self.items}
        for item_id in sorted(item_ids):
            entity_key = self.inventory_tag_key(item_lookup[item_id])
            for tag_id in sorted(tag_ids):
                self.store.save_tag_assignment(
                    TagAssignment("inventory", entity_key, tag_id, source="user")
                )
                count += 1
        self.store.mark_proposals_stale({"move", "finding"}, "Approved inventory tags changed")
        self.store.activity(
            "tag.assignment",
            f"Assigned {len(tag_ids)} tag(s) to {len(item_ids)} inventory item(s)",
        )
        self.inventory_changed.emit()
        return count

    def remove_item_tags(self, item_ids: set[str], tag_ids: set[str]) -> int:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        item_lookup = {str(value["id"]): value for value in self.items}
        entity_keys = {
            self.inventory_tag_key(item_lookup[item_id])
            for item_id in item_ids
            if item_id in item_lookup
        }
        count = self.store.delete_tag_assignments("inventory", entity_keys, tag_ids)
        if count:
            self.store.mark_proposals_stale({"move", "finding"}, "Approved inventory tags changed")
            self.store.activity(
                "tag.assignment_removed",
                f"Removed {count} inventory tag assignment(s)",
            )
            self.inventory_changed.emit()
        return count

    def scan_all(self) -> int:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        service = InventoryService(self.scanner)
        total = 0
        for source in self.sources.values():
            run = service.scan_root(source)
            self.store.save_source(source)
            metadata_by_key: dict[tuple[str, str], dict[str, Any]] = {}
            updates: list[tuple[Any, dict[str, Any]]] = []
            fingerprint_mode = self.metadata_fingerprint_mode()
            for item in run.items:
                metadata = self.store.cached_metadata(item)
                if metadata is not None and not metadata_cache_compatible(item, metadata):
                    metadata = None
                path = source.path / item.relative_path
                if metadata is not None and fingerprint_mode in {"crc32", "sha256"}:
                    stored_fingerprint = metadata.get("content_fingerprint", {})
                    if stored_fingerprint.get(
                        "algorithm"
                    ) != fingerprint_mode or content_fingerprint(path, fingerprint_mode)[
                        "value"
                    ] != stored_fingerprint.get("value"):
                        metadata = None
                if metadata is None:
                    metadata = self.metadata_indexer.extract(path, item)
                    if not item.is_dir and fingerprint_mode in {"crc32", "sha256"}:
                        metadata["content_fingerprint"] = content_fingerprint(
                            path, fingerprint_mode
                        )
                    updates.append((item, metadata))
                else:
                    metadata_by_key[(item.root_id, item.relative_path)] = metadata
            metadata_by_key.update(self.store.save_cached_metadata_batch(updates))
            enriched = [
                replace(
                    item,
                    metadata=metadata_by_key[(item.root_id, item.relative_path)],
                )
                for item in run.items
            ]
            self.store.mark_semantic_stale_batch(
                [
                    (
                        "file",
                        f"{source.id}:{item.relative_path}",
                        metadata_fingerprint(item),
                    )
                    for item in run.items
                ]
            )
            self.store.save_snapshot(run.id, source.id, enriched)
            self.store.prune_metadata_cache(source.id, {item.relative_path for item in run.items})
            total += len(enriched)
        self.items = self.store.list_items()
        self.store.activity("inventory.complete", f"Inventoried {total} items")
        self.inventory_changed.emit()
        self.activity_changed.emit()
        return total

    def revalidate_metadata_cache(self) -> int:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        return self.scan_all()

    def apply_inventory_scan(self, result: Any) -> int:
        """Persist a completed background scan on the SQLite-owning UI thread."""
        if not self.store:
            raise RuntimeError("Open a workspace first")
        items_by_key = {
            (item.root_id, item.relative_path): item for run in result.runs for item in run.items
        }
        saved_metadata = self.store.save_cached_metadata_batch(
            [
                (items_by_key[key], payload)
                for key, payload in result.metadata_updates.items()
                if key in items_by_key
            ],
        )
        total = 0
        for run in result.runs:
            source = self.sources[run.root_id]
            capability = result.capabilities.get(run.root_id)
            if capability is not None:
                source.capabilities = capability
            self.store.save_source(source)
            enriched = []
            for item in run.items:
                metadata = saved_metadata.get((item.root_id, item.relative_path), item.metadata)
                enriched_item = replace(item, metadata=metadata)
                enriched.append(enriched_item)
            self.store.mark_semantic_stale_batch(
                [
                    (
                        "file",
                        f"{source.id}:{item.relative_path}",
                        metadata_fingerprint(item),
                    )
                    for item in run.items
                ]
            )
            self.store.save_snapshot(run.id, source.id, enriched)
            self.store.prune_metadata_cache(source.id, {item.relative_path for item in run.items})
            total += len(enriched)
        self.items = self.store.list_items()
        self.store.activity("inventory.complete", f"Inventoried {total} items")
        self.inventory_changed.emit()
        self.activity_changed.emit()
        return total

    def metadata_cache_stats(self) -> dict[str, Any]:
        if not self.store:
            return {
                "records": 0,
                "fresh": 0,
                "expired": 0,
                "archive_members": 0,
                "validation": "size+modified_ns",
            }
        return self.store.metadata_cache_stats()

    def metadata_fingerprint_mode(self) -> str:
        if not self.store:
            return "none"
        value = self.store.get_meta("metadata_content_fingerprint", "none")
        return value if value in {"none", "crc32", "sha256"} else "none"

    def set_metadata_fingerprint_mode(self, mode: str) -> None:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        if mode not in {"none", "crc32", "sha256"}:
            raise ValueError("Unknown metadata fingerprint mode")
        self.store.set_meta("metadata_content_fingerprint", mode)
        self.store.activity(
            "metadata.fingerprint_policy",
            f"Content fingerprint validation set to {mode}",
        )
        self.activity_changed.emit()

    def refresh_software_inventory(self) -> int:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        packages = self.software_scanner.scan()
        self.store.save_software_inventory(packages)
        for package in packages:
            self.store.mark_semantic_stale(
                "software",
                package.id,
                package.version_fingerprint,
                namespace="update_assessment",
            )
        self.software_packages = self.store.list_software_inventory()
        self.store.activity(
            "software.inventory", f"Inventoried {len(packages)} installed application(s)"
        )
        self.software_changed.emit()
        self.activity_changed.emit()
        return len(packages)

    def download_items(self) -> list[dict[str, Any]]:
        if not self.store:
            return []
        download_category_ids = {
            str(value["id"])
            for value in self.store.list_category_payloads()
            if str(value.get("name", "")).casefold() == "downloads"
        }
        assignments = []
        for value in self.store.list_assignment_payloads():
            roles = {str(role) for role in value.get("roles", [])}
            categories = {str(category) for category in value.get("category_ids", [])}
            if FolderRole.DOWNLOADS.value in roles or categories & download_category_ids:
                assignments.append(Path(str(value.get("path", ""))).resolve(strict=False))
        result = []
        for item in self.items:
            if item.get("is_dir"):
                continue
            source = self.sources.get(str(item.get("root_id", "")))
            if source is None:
                continue
            path = (source.path / str(item.get("relative_path", ""))).resolve(strict=False)
            root_is_downloads = FolderRole.DOWNLOADS in source.roles or bool(
                source.category_ids & download_category_ids
            )
            assigned_downloads = any(
                path == assignment or assignment in path.parents for assignment in assignments
            )
            if root_is_downloads or assigned_downloads:
                result.append(item)
        return result

    def refresh_defender_history(
        self, progress: Callable[[int, int, str], None] | None = None
    ) -> dict[str, Any]:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        items = self.download_items()
        paths = [
            self.sources[str(item["root_id"])].path / str(item["relative_path"]) for item in items
        ]
        result = self.defender_scanner.history_for_paths(paths, progress)
        if not result.available:
            raise RuntimeError(result.error or "Microsoft Defender history is unavailable")
        detected = 0
        metadata_updates: dict[tuple[str, str], dict[str, Any]] = {}
        total = len(items)
        for index, (item, path) in enumerate(zip(items, paths, strict=True), start=1):
            detections = result.detections_by_path.get(str(path), [])
            detected += bool(detections)
            defender_metadata = {
                "provider": "Microsoft Defender Antivirus",
                "checked_at": result.checked_at,
                "status": (
                    "detected_in_history" if detections else "no_matching_detection_history"
                ),
                "detection_count": len(detections),
                "detections": detections,
            }
            key = (str(item["root_id"]), str(item["relative_path"]))
            metadata_updates[key] = {"windows_defender": defender_metadata}
            item.setdefault("metadata", {})["windows_defender"] = defender_metadata
            fingerprint = f"{item.get('size', 0)}:{item.get('modified_ns', 0)}"
            self.store.save_semantic_record(
                SemanticRecord(
                    "file",
                    f"{item['root_id']}:{item['relative_path']}",
                    "windows_defender",
                    defender_metadata,
                    source_fingerprint=fingerprint,
                    confidence=1.0,
                    provenance="windows_defender",
                )
            )
            if progress and (index == total or index % 25 == 0):
                progress(index, total, f"Saving Defender status {index:,} of {total:,}…")
        self.store.merge_cached_metadata_batch(metadata_updates)
        self.store.activity(
            "defender.history",
            f"Correlated Defender history with {len(items)} Download item(s); {detected} matched",
        )
        self.activity_changed.emit()
        self.inventory_changed.emit()
        return {"checked": len(items), "detected": detected, "checked_at": result.checked_at}

    def queue_update_research(
        self,
        targets: list[tuple[str, str]],
        release_channel: str,
        provider: str = "",
        model: str = "",
        prompt_hash: str = "",
    ) -> int:
        """Persist explicit desktop research requests for the connected MCP host."""
        if not self.store:
            raise RuntimeError("Open a workspace first")
        unique = sorted(set(targets))[:250]
        for entity_kind, entity_key in unique:
            self.store.save_semantic_record(
                SemanticRecord(
                    entity_kind,
                    entity_key,
                    "update_research_request",
                    {
                        "release_channel_policy": release_channel,
                        "requested_from": "desktop_updates_page",
                        "provider": provider,
                        "model": model,
                        "prompt_hash": prompt_hash,
                    },
                    provenance="user",
                )
            )
        self.store.activity(
            "updates.research_requested",
            f"Queued {len(unique)} selected update research target(s)",
        )
        self.activity_changed.emit()
        return len(unique)

    def record_update_assessment(self, assessment: UpdateAssessment) -> None:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        facts = assessment.model_dump(mode="json")
        if assessment.entity_kind == "software":
            package = next(
                (
                    value
                    for value in self.software_packages
                    if str(value["id"]) == assessment.entity_key
                ),
                None,
            )
            if package is None:
                raise ValueError("Unknown software identifier")
            identity = semantic_fingerprint(
                {
                    "name": str(package["name"]).casefold().strip(),
                    "publisher": str(package.get("publisher", "")).casefold().strip(),
                }
            )
            current_fingerprint = semantic_fingerprint(
                {
                    "identity": identity,
                    "version": package.get("version", ""),
                    "source": package.get("source", ""),
                }
            )
        else:
            item = next(
                (
                    value
                    for value in self.download_items()
                    if f"{value['root_id']}:{value['relative_path']}" == assessment.entity_key
                ),
                None,
            )
            if item is None:
                raise ValueError("Unknown Download item identifier")
            identity = semantic_fingerprint(
                {"application": assessment.application_name.casefold().strip()}
            )
            current_fingerprint = (
                f"{item.get('file_id', '')}:{item.get('size', 0)}:{item.get('modified_ns', 0)}"
            )
        self.store.save_semantic_record(
            SemanticRecord(
                assessment.entity_kind,
                assessment.entity_key,
                "update_assessment",
                facts,
                source_fingerprint=current_fingerprint,
                confidence=assessment.confidence,
                provenance="desktop_ai_structured_output",
            )
        )
        self.store.save_semantic_record(
            SemanticRecord(
                assessment.entity_kind,
                assessment.entity_key,
                "update_hint",
                {
                    "official_page_url": str(assessment.official_page_url or ""),
                    "direct_download_url": str(assessment.direct_download_url or ""),
                    "preferred_url_kind": assessment.preferred_url_kind.value,
                    "application": assessment.application_name,
                    "update_page_hint": (
                        assessment.update_page_hint.model_dump(mode="json")
                        if assessment.update_page_hint
                        else None
                    ),
                    "changelog_hint": (
                        assessment.changelog_hint.model_dump(mode="json")
                        if assessment.changelog_hint
                        else None
                    ),
                    "next_check_strategy": assessment.next_check_strategy,
                },
                source_fingerprint=identity,
                confidence=assessment.confidence,
                provenance="desktop_ai_structured_output",
            )
        )
        self.store.save_semantic_record(
            SemanticRecord(
                assessment.entity_kind,
                assessment.entity_key,
                "update_research_request",
                {"fulfilled_by_check": assessment.checked_at},
                source_fingerprint=identity,
                confidence=assessment.confidence,
                provenance="desktop_ai_structured_output",
                status="fulfilled",
            )
        )
        self.store.save_semantic_record(
            SemanticRecord(
                assessment.entity_kind,
                assessment.entity_key,
                "update_hint_error",
                {"message": "", "needs_ai_reaudit": False},
                confidence=1.0,
                provenance="desktop_ai_structured_output",
                status="resolved",
            )
        )
        self.store.activity(
            "updates.assessment",
            f"Stored structured update assessment for {assessment.application_name}",
        )
        self.activity_changed.emit()

    def record_update_hint_failure(self, entity_kind: str, entity_key: str, message: str) -> None:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        self.store.save_semantic_record(
            SemanticRecord(
                entity_kind,
                entity_key,
                "update_hint_error",
                {"message": message[:1_000], "needs_ai_reaudit": True},
                confidence=1.0,
                provenance="deterministic_update_check",
                status="error",
            )
        )
        self.store.activity("updates.hint_failed", f"Saved update hint failure for {entity_key}")
        self.activity_changed.emit()

    def save_prompt_revision(self, revision: PromptRevision) -> None:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        self.store.save_prompt_revision(revision)
        view = revision.profile_id.removeprefix("view:")
        affected = {
            "rename": {"rename"},
            "repair": {"finding"},
            "folder": {"folder", "move"},
            "move": {"move"},
            "action": {"finding", "move"},
            "audit": {"finding"},
            "cleanup": {"finding"},
            "recurrence": {"finding"},
        }.get(view, {"rename", "folder", "move", "finding"})
        stale = self.store.mark_proposals_stale(affected, "Prompt revision changed")
        self.store.activity("prompt.revision", f"Saved {revision.profile_id}")
        if stale:
            self.store.activity("proposal.stale", f"Marked {stale} proposal set(s) stale")
        self.activity_changed.emit()
        self.prompt_changed.emit(revision.profile_id)

    def latest_prompt_text(self, profile_id: str) -> str:
        if not self.store:
            return ""
        row = self.store.latest_prompt(profile_id)
        return str(row["text"]) if row else ""

    def ai_context(self, view_key: str) -> tuple[str, str]:
        defaults = {
            "local": "deterministic",
            "deepseek": os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            "openrouter": os.getenv("OPENROUTER_MODEL", "openai/gpt-5.2"),
        }
        fallback_provider = "deepseek" if os.getenv("DEEPSEEK_API_KEY") else "local"
        if not self.store:
            return fallback_provider, defaults[fallback_provider]
        raw = self.store.get_meta(f"ai_context:{view_key}")
        if raw:
            try:
                value = json.loads(raw)
                provider = str(value.get("provider", fallback_provider))
                model = str(value.get("model", ""))
                if provider in {
                    "local",
                    "deepseek",
                    "openrouter",
                    "openai",
                    "anthropic",
                    "codex",
                }:
                    return provider, model or defaults.get(provider, "")
            except (TypeError, json.JSONDecodeError):
                pass
        return fallback_provider, defaults[fallback_provider]

    def set_ai_context(self, view_key: str, provider: str, model: str) -> None:
        if not self.store:
            return
        if provider not in {
            "local",
            "deepseek",
            "openrouter",
            "openai",
            "anthropic",
            "codex",
        }:
            raise ValueError("Unknown AI provider")
        self.store.set_meta(
            f"ai_context:{view_key}",
            json.dumps({"provider": provider, "model": model}, sort_keys=True),
        )

    def compile_prompt(
        self,
        view_key: str,
        provider: str,
        model: str,
        view_text: str,
        evidence: str,
    ) -> CompiledPrompt:
        workspace_revision = None
        category_revisions: list[PromptRevision] = []
        if self.store:
            row = self.store.latest_prompt("workspace:general")
            if row:
                workspace_revision = PromptRevision(
                    row["profile_id"],
                    PromptLayerKind(row["kind"]),
                    row["text"],
                    row["id"],
                    row["created_at"],
                )
            category_payloads = {
                value["id"]: value for value in self.store.list_category_payloads()
            }
            selected_root_ids = {
                item["root_id"] for item in self.items if item["id"] in self.selected_item_ids
            } or self.selected_root_ids
            category_ids = {
                category_id
                for root_id in selected_root_ids
                if root_id in self.sources
                for category_id in self.sources[root_id].category_ids
            }

            def category_depth(category_id: str) -> int:
                depth = 0
                current = category_payloads.get(category_id)
                while current and current.get("parent_id"):
                    depth += 1
                    current = category_payloads.get(current["parent_id"])
                return depth

            for category_id in sorted(category_ids, key=category_depth):
                payload = category_payloads.get(category_id)
                if payload and payload.get("guidance", "").strip():
                    category_revisions.append(
                        PromptRevision(
                            f"category:{category_id}",
                            PromptLayerKind.CATEGORY,
                            payload["guidance"],
                            id=f"category:{category_id}:r{payload.get('revision', 1)}",
                        )
                    )
        view_revision = None
        if view_text.strip():
            view_revision = PromptRevision(
                f"view:{view_key}", PromptLayerKind.VIEW, view_text.strip()
            )
        else:
            row = self.store.latest_prompt(f"view:{view_key}") if self.store else None
            if row:
                view_revision = PromptRevision(
                    str(row["profile_id"]),
                    PromptLayerKind(str(row["kind"])),
                    str(row["text"]),
                    str(row["id"]),
                    str(row["created_at"]),
                )
        return PromptCompiler(self.private_redaction_terms()).compile(
            provider=provider,
            model=model,
            workspace=workspace_revision,
            view=view_revision,
            categories=category_revisions,
            evidence=evidence,
        )

    @staticmethod
    def private_redaction_terms() -> tuple[str, ...]:
        raw = SecretStore().get("private_redaction_terms")
        if not raw:
            return ()
        try:
            values = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return ()
        if not isinstance(values, list):
            return ()
        return tuple(
            value.strip()
            for value in values[:250]
            if isinstance(value, str) and 2 <= len(value.strip()) <= 500
        )

    def save_action_result(self, run: ActionRun, findings: FindingSet) -> str | None:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        self.store.save_action_run(run, findings)
        proposal_id: str | None = None
        if run.output_mode.value == "move_proposals":
            item_lookup = {item["id"]: item for item in self.items}
            destinations = [
                source
                for source in self.sources.values()
                if source.roles.intersection({FolderRole.DESTINATION, FolderRole.ARCHIVE})
                and source.capabilities
                and source.capabilities.writable
            ]
            proposal = ProposalSet(
                ProposalKind.MOVE,
                "current-inventory",
                action_run_id=run.id,
                provider="local",
                model="focused-action-router",
            )
            for finding in findings.findings:
                item = item_lookup.get(finding.item_id)
                eligible = [
                    destination
                    for destination in destinations
                    if item and destination.id != item["root_id"]
                ]
                destination = eligible[0] if eligible else None
                blocked = destination is None or item is None
                target = (
                    str(destination.path / Path(item["relative_path"]).name)
                    if destination and item
                    else ""
                )
                if target and Path(target).exists():
                    blocked = True
                proposal.items.append(
                    ProposalItem(
                        finding.item_id,
                        target,
                        ProposalKind.MOVE,
                        status=(ProposalStatus.BLOCKED if blocked else ProposalStatus.PROPOSED),
                        confidence=finding.confidence,
                        rationale=finding.rationale,
                        metadata={
                            "finding_id": finding.id,
                            "destination_root_id": destination.id if destination else None,
                        },
                        issues=["No eligible unoccupied destination"] if blocked else [],
                    )
                )
            self.store.save_proposal_set(proposal)
            proposal_id = proposal.id
        self.store.activity(
            "action.completed",
            f"Focused action produced {len(findings.findings)} finding(s)",
            {"action_run": run.id, "move_proposal_set": proposal_id},
        )
        self.activity_changed.emit()
        return proposal_id

    def source_path(self, root_id: str) -> Path:
        return self.sources[root_id].path

    def hydrate_selected(self, item_ids: set[str]) -> int:
        selected = [
            item for item in self.items if item["id"] in item_ids and item.get("is_placeholder")
        ]
        for item in selected:
            source = self.sources[item["root_id"]]
            path = source.path / item["relative_path"]
            if item.get("is_dir"):
                raise RuntimeError("Hydrate placeholder directories outside AIOrganizer first")
            with path.open("rb") as stream:
                while stream.read(1024 * 1024):
                    pass
        if selected:
            self.scan_all()
        return len(selected)

    def undo_last_commit(self) -> int:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        if self.store.incomplete_journals():
            raise RuntimeError("Resolve incomplete recovery before undo")
        payload = self.store.latest_completed_journal()
        if not payload:
            raise ValueError("No completed filesystem journal is available to undo")
        original = Journal(
            plan_id=payload["plan_id"],
            id=payload["id"],
            state=payload["state"],
            operations=payload["operations"],
            updated_at=payload.get("updated_at", ""),
        )
        undo = FileOperationEngine(self._journal_sink).execute_undo(f"undo_{original.id}", original)
        payload["state"] = "undone"
        payload["undone_by"] = undo.id
        self.store.save_journal(original.id, original.plan_id, "undone", payload)
        self.store.activity(
            "filesystem.undo",
            f"Verified undo of {len(original.operations)} operation(s)",
            {"journal": original.id, "undo_journal": undo.id},
        )
        self.scan_all()
        return len(original.operations)

    def recover_incomplete_journals(self) -> int:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        payloads = self.store.incomplete_journals()
        recovered = 0
        for payload in payloads:
            journal = Journal(
                plan_id=str(payload["plan_id"]),
                id=str(payload["id"]),
                state=str(payload["state"]),
                operations=list(payload.get("operations", [])),
                updated_at=str(payload.get("updated_at", "")),
            )
            FileOperationEngine(self._journal_sink).recover_incomplete(journal)
            recovered += 1
        if recovered:
            self.store.activity(
                "filesystem.recovered",
                f"Rolled back {recovered} interrupted filesystem journal(s)",
            )
            self.scan_all()
            self.activity_changed.emit()
        return recovered

    def execute_rename_rows(
        self,
        rows: list[dict[str, Any]],
        prompt_hash: str,
        provider: str = "local",
        model: str = "deterministic",
    ) -> int:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        if self.store.incomplete_journals():
            raise RuntimeError("Resolve the incomplete filesystem journal before a new commit")
        selected = [row for row in rows if row.get("selected")]
        if not selected:
            raise ValueError("Select at least one proposal")
        self._validate_selected_inventory(selected)
        proposal_set = ProposalSet(
            ProposalKind.RENAME,
            "current-inventory",
            prompt_hash=prompt_hash,
            provider=provider,
            model=model,
        )
        requests: list[RenameRequest] = []
        operations: list[dict[str, Any]] = []
        for row in selected:
            if row.get("is_placeholder"):
                raise RuntimeError("Hydrate selected cloud-only files before rename")
            proposed_name = str(row["proposed"]).strip()
            if not valid_filename_proposal(str(row.get("current", "")), proposed_name):
                raise ValueError(f"Proposed filename is invalid: {proposed_name!r}")
            source_root = self.sources[str(row["root_id"])].path
            source = source_root / str(row["relative_path"])
            target = source.with_name(proposed_name)
            if source == target:
                continue
            token = SnapshotToken.capture(source)
            request = RenameRequest(source, target, token)
            requests.append(request)
            operations.append(
                {
                    "kind": "rename",
                    "source": str(source),
                    "target": str(target),
                    "snapshot": token.sha256,
                }
            )
            proposal_set.items.append(
                ProposalItem(
                    str(row["item_id"]),
                    proposed_name,
                    ProposalKind.RENAME,
                    current_value=source.name,
                    status=ProposalStatus.ACCEPTED,
                    confidence=float(row.get("confidence", 0.0)),
                    rationale=str(row.get("reason", "")),
                    evidence_ids=list(row.get("evidence_ids", [])),
                    metadata={
                        "token_provenance": row.get("token_provenance", {}),
                    },
                )
            )
        if not requests:
            raise ValueError("Selected proposals do not change any filenames")
        self.store.save_proposal_set(proposal_set)
        plan = FrozenPlan(
            new_id("plan"),
            proposal_set.id,
            proposal_set.revision,
            prompt_hash,
            0,
            tuple(operations),
            PlanState.READY,
        )
        self.store.save_frozen_plan(plan)

        def save_journal(journal: Any) -> None:
            self.store.save_journal(
                journal.id, journal.plan_id, journal.state, journal_to_dict(journal)
            )

        FileOperationEngine(save_journal).execute_renames(plan.id, requests)
        self.store.activity(
            "rename.committed", f"Verified {len(requests)} rename operation(s)", {"plan": plan.id}
        )
        self.scan_all()
        self.activity_changed.emit()
        return len(requests)

    def cloud_allowed(self, root_id: str) -> tuple[bool, str]:
        preview = self.provider_request_preview(
            {root_id},
            (),
            "provider",
            "model",
            (EvidenceClass.EXTRACTED_TEXT,),
            0,
            0,
        )
        return preview.allowed, (
            "Root and category policies allow cloud text"
            if preview.allowed
            else "; ".join(preview.blocked_reasons)
        )

    def create_selection_scope(
        self, item_ids: list[str], proposal_set_id: str | None = None
    ) -> SelectionScope:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        unique = tuple(dict.fromkeys(item_ids))
        scope = SelectionScope(unique, proposal_set_id)
        self.store.create_selection_scope(scope)
        self.store.activity(
            "selection.scope_created",
            f"Created a bounded selection scope for {len(unique)} item(s)",
            {"scope_id": scope.id, "proposal_set_id": proposal_set_id or ""},
        )
        self.activity_changed.emit()
        return scope

    def provider_request_preview(
        self,
        root_ids: set[str],
        item_ids: tuple[str, ...],
        provider: str,
        model: str,
        content_classes: tuple[EvidenceClass, ...],
        redaction_count: int,
        estimated_characters: int,
    ) -> ProviderRequestPreview:
        if not self.store:
            return ProviderRequestPreview(
                provider,
                model,
                item_ids,
                content_classes,
                redaction_count,
                estimated_characters,
                estimated_characters // 4,
                {},
                False,
                ("Workspace is not open",),
            )
        categories = {value["id"]: value for value in self.store.list_category_payloads()}
        policies: dict[str, str] = {}
        blocked: list[str] = []
        required = max((_evidence_cloud_rank(value) for value in content_classes), default=1)
        if EvidenceClass.SECRET_LIKE in content_classes:
            blocked.append("Secret-like evidence may not be sent to a provider")
        for root_id in sorted(root_ids):
            source = self.sources.get(root_id)
            if source is None:
                blocked.append(f"Unknown source scope {root_id}")
                continue
            effective = source.cloud_policy
            # A source policy is the user's explicit authorization for that physical root.
            # Category policies supply a default only when a source explicitly inherits;
            # silently lowering a later source choice made text_and_images appear metadata-only.
            if effective == CloudPolicy.INHERIT:
                inherited = [
                    CloudPolicy(category["cloud_policy"])
                    for category_id in source.category_ids
                    if (category := categories.get(category_id))
                    and CloudPolicy(category.get("cloud_policy", "inherit")) != CloudPolicy.INHERIT
                ]
                effective = (
                    min(inherited, key=_cloud_policy_rank) if inherited else CloudPolicy.NONE
                )
            policies[root_id] = effective.value
            if _cloud_policy_rank(effective) < required:
                blocked.append(
                    f"Source {source.name} permits {effective.value}, below the requested content class"
                )
        return ProviderRequestPreview(
            provider,
            model,
            item_ids,
            content_classes,
            redaction_count,
            estimated_characters,
            estimated_characters // 4,
            policies,
            not blocked,
            tuple(blocked),
        )

    def execute_folder_rows(self, rows: list[dict[str, Any]], prompt_hash: str) -> int:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        if self.store.incomplete_journals():
            raise RuntimeError("Resolve the incomplete filesystem journal before a new commit")
        selected = [row for row in rows if row.get("selected")]
        requests: list[FolderCreateRequest] = []
        rename_requests: list[RenameRequest] = []
        proposal_set = ProposalSet(
            ProposalKind.FOLDER, "current-inventory", prompt_hash=prompt_hash
        )
        operations: list[dict[str, Any]] = []
        selected_by_root: dict[str, list[dict[str, Any]]] = {}
        for row in selected:
            selected_by_root.setdefault(str(row["root_id"]), []).append(row)
        folder_items = {
            (str(item["root_id"]), str(item["relative_path"])): item
            for item in self.items
            if item.get("is_dir")
        }
        for root_id, root_rows in selected_by_root.items():
            source = self.sources[root_id]
            current_paths = {
                str(item["relative_path"])
                for item in self.items
                if item["root_id"] == root_id and item.get("is_dir")
            }
            changes = [
                HierarchyChange(
                    str(row["projected"]),
                    None
                    if str(row.get("current", "")).strip() in {"", "—"}
                    else str(row["current"]),
                    str(row.get("category_id", "")) or None,
                )
                for row in root_rows
            ]
            projection = UnionHierarchyPlanner().project(
                root_id,
                current_paths,
                changes,
                case_sensitive=bool(source.capabilities and source.capabilities.case_sensitive),
                windows_rules=platform.system() == "Windows",
            )
            if not projection.ready:
                raise ValueError("Projected hierarchy is invalid: " + "; ".join(projection.issues))
        for row in selected:
            root = self.sources[str(row["root_id"])].path
            projected = str(row["projected"]).strip()
            relative = Path(projected)
            if not projected or relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"Projected folder is invalid: {projected!r}")
            category_id = str(row.get("category_id", ""))
            maximum_depth = self.folder_depth_limit(str(row["root_id"]), category_id)
            if len(relative.parts) > maximum_depth:
                raise ValueError(
                    f"Projected hierarchy exceeds its maximum depth of {maximum_depth}"
                )
            target = root / relative
            current = str(row.get("current", "")).strip()
            if current and current != "—":
                if current == projected:
                    continue
                current_item = folder_items.get((str(row["root_id"]), current), {})
                if current_item.get("is_project_root") or current_item.get(
                    "inside_protected_project"
                ):
                    raise PermissionError(
                        "Generic Folder Plan cannot rename a protected project boundary"
                    )
                source_path = root / current
                if source_path.parent.resolve(strict=True) != target.parent.resolve(strict=True):
                    raise ValueError("Folder Plan may rename in place but cannot reparent folders")
                rename_requests.append(
                    RenameRequest(source_path, target, SnapshotToken.capture(source_path))
                )
                operations.append(
                    {
                        "kind": "folder_rename",
                        "source": str(source_path),
                        "target": str(target),
                    }
                )
            else:
                for project_path in _protected_project_paths(self.items, str(row["root_id"])):
                    candidate = Path(projected)
                    project = Path(project_path)
                    if project_path == "" or candidate == project or project in candidate.parents:
                        raise PermissionError(
                            "Generic Folder Plan cannot create inside a protected project"
                        )
                requests.append(FolderCreateRequest(target, root))
                operations.append({"kind": "folder_create", "target": str(target)})
            proposal_set.items.append(
                ProposalItem(
                    str(row.get("category_id", row["root_id"])),
                    projected,
                    ProposalKind.FOLDER,
                    status=ProposalStatus.ACCEPTED,
                    rationale=str(row.get("reason", "")),
                )
            )
        if requests and rename_requests:
            raise ValueError("Commit folder creates and folder renames as separate plans")
        if not requests and not rename_requests:
            raise ValueError("Select at least one new folder")
        self.store.save_proposal_set(proposal_set)
        plan = FrozenPlan(
            new_id("plan"),
            proposal_set.id,
            proposal_set.revision,
            prompt_hash,
            0,
            tuple(operations),
            PlanState.READY,
        )
        self.store.save_frozen_plan(plan)
        engine = FileOperationEngine(self._journal_sink)
        if requests:
            engine.execute_folder_creates(
                plan.id, sorted(requests, key=lambda request: len(request.path.parts))
            )
        else:
            engine.execute_renames(plan.id, rename_requests)
        completed_count = len(requests) + len(rename_requests)
        self.store.activity(
            "folder.committed",
            f"Verified {completed_count} folder operation(s)",
            {"plan": plan.id},
        )
        self.scan_all()
        return completed_count

    def execute_move_rows(self, rows: list[dict[str, Any]], prompt_hash: str) -> int:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        if self.store.incomplete_journals():
            raise RuntimeError("Resolve the incomplete filesystem journal before a new commit")
        selected = [row for row in rows if row.get("selected")]
        self._validate_selected_inventory(selected)
        item_lookup = {str(item["id"]): item for item in self.items}
        move_candidates = [
            MoveCandidate(
                str(row["item_id"]),
                str(row["root_id"]),
                str(row["destination_root_id"]),
                str(row["relative_path"]),
                str(row.get("destination", "")),
                Path(str(row["relative_path"])).name,
                bool(row.get("is_dir")),
                bool(row.get("is_project_root")),
                bool(item_lookup[str(row["item_id"])].get("inside_protected_project")),
            )
            for row in selected
        ]
        existing_paths: dict[str, set[str]] = {}
        projected_folders: dict[str, set[str]] = {}
        protected_paths: dict[str, set[str]] = {}
        case_sensitive: dict[str, bool] = {}
        for root_id, source in self.sources.items():
            existing_paths[root_id] = {
                str(item["relative_path"]) for item in self.items if item["root_id"] == root_id
            }
            projected_folders[root_id] = {
                str(item["relative_path"])
                for item in self.items
                if item["root_id"] == root_id and item.get("is_dir")
            }
            projected_folders[root_id].add("")
            protected_paths[root_id] = _protected_project_paths(self.items, root_id)
            case_sensitive[root_id] = bool(
                source.capabilities and source.capabilities.case_sensitive
            )
        projections = ProjectedMoveValidator().validate(
            move_candidates,
            existing_paths,
            projected_folders,
            protected_paths,
            case_sensitive,
        )
        issues = [issue for projection in projections.values() for issue in projection.issues]
        if issues:
            raise ValueError("Projected moves are invalid: " + "; ".join(issues))
        requests: list[MoveRequest] = []
        proposal_set = ProposalSet(ProposalKind.MOVE, "current-inventory", prompt_hash=prompt_hash)
        operations: list[dict[str, Any]] = []
        for row in selected:
            if row.get("is_placeholder"):
                raise RuntimeError("Hydrate selected cloud-only files before move")
            source_root = self.sources[str(row["root_id"])]
            destination_root = self.sources[str(row["destination_root_id"])]
            if not destination_root.capabilities or not destination_root.capabilities.writable:
                raise PermissionError("Destination root is not reachable and writable")
            if not destination_root.roles.intersection(
                {FolderRole.DESTINATION, FolderRole.ARCHIVE}
            ):
                raise PermissionError("Destination is not assigned Destination or Archive role")
            if row.get("is_dir") and not row.get("is_project_root"):
                raise PermissionError("Arbitrary directory-tree moves are blocked")
            if item_lookup[str(row["item_id"])].get("inside_protected_project"):
                raise PermissionError("Generic moves cannot move files inside a protected project")
            source_path = source_root.path / str(row["relative_path"])
            target_folder = destination_root.path / str(row.get("destination", ""))
            target = target_folder / source_path.name
            token = SnapshotToken.capture(source_path)
            requests.append(
                MoveRequest(
                    source_path,
                    target,
                    token,
                    source_root.path,
                    destination_root.path,
                    str(source_root.path.stat().st_dev),
                    str(destination_root.path.stat().st_dev),
                )
            )
            operations.append(
                {
                    "kind": "move",
                    "source": str(source_path),
                    "target": str(target),
                    "snapshot": token.sha256,
                }
            )
            proposal_set.items.append(
                ProposalItem(
                    str(row["item_id"]),
                    str(target_folder),
                    ProposalKind.MOVE,
                    current_value=str(source_path.parent),
                    status=ProposalStatus.ACCEPTED,
                    rationale=str(row.get("reason", "")),
                )
            )
        if not requests:
            raise ValueError("Select at least one move proposal")
        self.store.save_proposal_set(proposal_set)
        plan = FrozenPlan(
            new_id("plan"),
            proposal_set.id,
            proposal_set.revision,
            prompt_hash,
            0,
            tuple(operations),
            PlanState.READY,
        )
        self.store.save_frozen_plan(plan)
        engine = FileOperationEngine(self._journal_sink)
        engine.execute_moves(plan.id, requests)
        self.store.activity(
            "move.committed", f"Verified {len(requests)} move(s)", {"plan": plan.id}
        )
        self.scan_all()
        return len(requests)

    def cleanup_candidates(self) -> list[dict[str, Any]]:
        active_paths: set[Path] = set()
        if self.store:
            for payload in self.store.incomplete_journals():
                for operation in payload.get("operations", []):
                    for key in ("source", "target", "temp", "partial", "quarantine"):
                        if operation.get(key):
                            active_paths.add(Path(str(operation[key])))
        rows: list[dict[str, Any]] = []
        move_empty_paths = self.store.completed_move_source_folders() if self.store else set()
        for root_id, source in self.sources.items():
            if not source.capabilities or not source.capabilities.reachable:
                continue
            candidates = self.cleanup_analyzer.analyze(
                root_id,
                source.path,
                self.items,
                active_operation_paths=active_paths,
                move_created_empty_paths=move_empty_paths,
            )
            for candidate in candidates:
                rows.append(
                    {
                        "selected": candidate.selected_by_default and candidate.ready,
                        "status": "ready" if candidate.ready else "blocked",
                        "kind": candidate.kind.value,
                        "path": candidate.relative_path,
                        "size": candidate.total_size,
                        "items": candidate.item_count,
                        "derivation": candidate.derivation,
                        "regeneration": "; ".join(candidate.regeneration_evidence),
                        "exclusions": "; ".join(candidate.exclusions),
                        "destination": "AIOrganizer quarantine (restorable)",
                        "item_id": candidate.item_id,
                        "root_id": candidate.root_id,
                    }
                )
        return rows

    def execute_cleanup_rows(self, rows: list[dict[str, Any]], prompt_hash: str) -> int:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        if self.store.incomplete_journals():
            raise RuntimeError("Resolve the incomplete filesystem journal before cleanup")
        selected = [row for row in rows if row.get("selected")]
        if not selected:
            raise ValueError("Select at least one cleanup candidate")

        selected_keys = {
            (str(row["root_id"]), str(row["path"]), str(row["kind"])) for row in selected
        }
        current_keys: set[tuple[str, str, str]] = set()
        move_empty_paths = self.store.completed_move_source_folders()
        for root_id in {key[0] for key in selected_keys}:
            source = self.sources[root_id]
            for candidate in self.cleanup_analyzer.analyze(
                root_id,
                source.path,
                self.items,
                move_created_empty_paths=move_empty_paths,
            ):
                current_keys.add((candidate.root_id, candidate.relative_path, candidate.kind.value))
        missing = selected_keys - current_keys
        if missing:
            raise RuntimeError(
                "Cleanup evidence changed; regenerate the cleanup review before applying"
            )

        requests: list[CleanupRequest] = []
        proposal_set = ProposalSet(
            ProposalKind.CLEANUP,
            "current-inventory",
            prompt_hash=prompt_hash,
            provider="local",
            model="deterministic",
        )
        operations: list[dict[str, Any]] = []
        for row in selected:
            root = self.sources[str(row["root_id"])].path
            source_path = root / str(row["path"])
            token = SnapshotToken.capture(source_path)
            cleanup_kind = CleanupKind(str(row["kind"]))
            requests.append(CleanupRequest(source_path, root, token, cleanup_kind.value))
            operations.append(
                {
                    "kind": "cleanup",
                    "cleanup_kind": cleanup_kind.value,
                    "source": str(source_path),
                    "snapshot": token.sha256,
                }
            )
            proposal_set.items.append(
                ProposalItem(
                    str(row["item_id"]),
                    "AIOrganizer quarantine",
                    ProposalKind.CLEANUP,
                    current_value=str(source_path),
                    status=ProposalStatus.ACCEPTED,
                    rationale=str(row.get("derivation", "")),
                    metadata={
                        "regeneration_evidence": str(row.get("regeneration", "")),
                        "exclusions": str(row.get("exclusions", "")),
                    },
                )
            )
        self.store.save_proposal_set(proposal_set)
        plan = FrozenPlan(
            new_id("plan"),
            proposal_set.id,
            proposal_set.revision,
            prompt_hash,
            0,
            tuple(operations),
            PlanState.READY,
        )
        self.store.save_frozen_plan(plan)
        FileOperationEngine(self._journal_sink).execute_cleanup(plan.id, requests)
        self.store.activity(
            "cleanup.quarantined",
            f"Quarantined {len(requests)} cleanup candidate(s) with restore available",
            {"plan": plan.id},
        )
        self.scan_all()
        self.activity_changed.emit()
        return len(requests)

    def restore_last_cleanup(self) -> int:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        if self.store.incomplete_journals():
            raise RuntimeError("Resolve incomplete recovery before restoring cleanup")
        payload = self.store.latest_completed_cleanup_journal()
        if not payload:
            raise ValueError("No completed cleanup quarantine is available to restore")
        original = Journal(
            plan_id=payload["plan_id"],
            id=payload["id"],
            state=payload["state"],
            operations=payload["operations"],
            updated_at=payload.get("updated_at", ""),
        )
        restored = FileOperationEngine(self._journal_sink).execute_undo(
            f"restore_{original.id}", original
        )
        payload["state"] = "undone"
        payload["undone_by"] = restored.id
        self.store.save_journal(original.id, original.plan_id, "undone", payload)
        count = len([op for op in original.operations if op.get("kind") == "cleanup"])
        self.store.activity(
            "cleanup.restored",
            f"Restored {count} quarantined cleanup candidate(s)",
            {"journal": original.id, "restore_journal": restored.id},
        )
        self.scan_all()
        self.activity_changed.emit()
        return count

    def discover_recurrence_candidates(self) -> list[dict[str, Any]]:
        if not self.store:
            return []
        eligible = [
            item
            for item in self.items
            if not item.get("is_dir")
            and str(item.get("extension", "")).casefold() in {".pdf", ".docx", ".xlsx", ".txt"}
        ]
        evidence_by_item: dict[str, list[dict[str, Any]]] = {}
        item_ids = [str(item["id"]) for item in eligible]
        for offset in range(0, len(item_ids), 200):
            chunk = set(item_ids[offset : offset + 200])
            payload = self.store.list_evidence_payloads(chunk, limit=250)
            for evidence in payload["evidence"]:
                evidence_by_item.setdefault(str(evidence["item_id"]), []).append(evidence)
        candidates = self.series_builder.build(eligible, evidence_by_item)
        return [
            {
                "selected": False,
                "name": candidate.name,
                "issuer": candidate.issuer,
                "document_type": candidate.document_type,
                "account": candidate.masked_account_id,
                "cadence": candidate.cadence.value,
                "confidence": candidate.cadence_confidence,
                "periods": len({value.period_start for value in candidate.observations}),
                "documents": len(candidate.observations),
                "rationale": "; ".join(candidate.rationale),
                "candidate_id": candidate.id,
                "stable_fingerprint": candidate.stable_fingerprint,
                "observations": [asdict(value) for value in candidate.observations],
            }
            for candidate in candidates
        ]

    def save_reviewed_series(
        self,
        candidate: dict[str, Any],
        *,
        name: str,
        issuer: str,
        document_type: str,
        masked_account_id: str,
        cadence: str,
        start_period: str,
        end_period: str | None,
        grace_days: int,
        series_id: str | None = None,
        revision: int = 1,
    ) -> str:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        current_by_id = {str(item["id"]): item for item in self.items}
        current_by_path = {
            (str(item["root_id"]), str(item["relative_path"])): item for item in self.items
        }
        observations_list: list[SeriesObservation] = []
        for value in candidate.get("observations", []):
            root_id = str(value.get("root_id", ""))
            relative_path = str(value.get("relative_path", ""))
            item = current_by_id.get(str(value["item_id"])) or current_by_path.get(
                (root_id, relative_path)
            )
            if item is None:
                raise RuntimeError("A reviewed series document is no longer in the inventory")
            observations_list.append(
                SeriesObservation(
                    str(item["id"]),
                    str(value["period_start"]),
                    max(0.85, float(value["confidence"])),
                    tuple(str(record) for record in value.get("evidence", [])),
                    str(item["root_id"]),
                    str(item["relative_path"]),
                    f"{item.get('size', 0)}:{item.get('modified_ns', 0)}",
                )
            )
        observations = tuple(observations_list)
        if not observations:
            raise ValueError("Select at least one observed document for the series")
        series = RecurrenceSeries(
            name=name,
            issuer=issuer,
            document_type=document_type,
            masked_account_id=masked_account_id,
            cadence=Cadence(cadence),
            start_period=start_period,
            end_period=end_period,
            grace_days=grace_days,
            observations=observations,
            stable_fingerprint=str(candidate["stable_fingerprint"]),
            id=series_id or new_id("series"),
            revision=revision,
        )
        series.validate()
        self.store.save_recurrence_series(series)
        self.store.activity(
            "recurrence.series_reviewed",
            f"Reviewed recurring series {series.name}",
            {"series_id": series.id, "observations": len(series.observations)},
        )
        self.recurrence_changed.emit()
        self.activity_changed.emit()
        return series.id

    def recurrence_series(self) -> list[dict[str, Any]]:
        return self.store.list_recurrence_series() if self.store else []

    def recurrence_gap_rows(
        self, series_id: str, *, as_of: date | None = None
    ) -> list[dict[str, Any]]:
        if not self.store:
            return []
        payload = next(
            (
                value
                for value in self.store.list_recurrence_series()
                if str(value["id"]) == series_id
            ),
            None,
        )
        if payload is None:
            raise ValueError("Recurring series no longer exists")
        series = recurrence_series_from_payload(payload)
        series.observations = rebind_observations(series.observations, self.items)
        exceptions = [
            RecurrenceException(
                str(value["series_id"]),
                str(value["period_start"]),
                GapStatus(str(value["status"])),
                str(value["reason"]),
                str(value["updated_at"]),
            )
            for value in self.store.list_recurrence_exceptions(series_id)
        ]
        return [asdict(row) for row in GapMatrix().build(series, exceptions, as_of=as_of)]

    def set_recurrence_exception(
        self, series_id: str, period_start: str, status: str, reason: str
    ) -> None:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        if not reason.strip():
            raise ValueError("Explain why this period is dismissed or not applicable")
        exception = RecurrenceException(
            series_id,
            period_start,
            GapStatus(status),
            reason.strip(),
        )
        self.store.save_recurrence_exception(exception)
        self.store.activity(
            "recurrence.exception",
            f"Marked {period_start} as {exception.status.value}",
            {"series_id": series_id, "reason": reason.strip()},
        )
        self.recurrence_changed.emit()
        self.activity_changed.emit()

    def clear_recurrence_exception(self, series_id: str, period_start: str) -> None:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        self.store.delete_recurrence_exception(series_id, period_start)
        self.recurrence_changed.emit()
        self.activity_changed.emit()

    def _validate_selected_inventory(self, rows: list[dict[str, Any]]) -> None:
        """Reject actions based on stale metadata or externally changed filesystem state."""
        item_lookup = {str(item["id"]): item for item in self.items}
        for row in rows:
            item_id = str(row.get("item_id", ""))
            if not item_id:
                continue
            item = item_lookup.get(item_id)
            if item is None:
                raise RuntimeError(
                    "Inventory item is stale; revalidate the inventory before applying"
                )
            source = self.sources[str(item["root_id"])].path / str(item["relative_path"])
            try:
                info = source.stat(follow_symlinks=False)
            except OSError as error:
                raise RuntimeError(
                    "A selected item changed outside AIOrganizer; revalidate before applying"
                ) from error
            if info.st_size != int(item["size"]) or info.st_mtime_ns != int(item["modified_ns"]):
                raise RuntimeError(
                    "A selected item changed outside AIOrganizer; revalidate before applying"
                )
            fingerprint_mode = self.metadata_fingerprint_mode()
            if fingerprint_mode in {"crc32", "sha256"} and source.is_file():
                expected = item.get("metadata", {}).get("content_fingerprint", {})
                if expected.get("algorithm") != fingerprint_mode:
                    raise RuntimeError(
                        "Content fingerprint is missing; revalidate metadata before applying"
                    )
                actual = content_fingerprint(source, fingerprint_mode)
                if actual["value"] != expected.get("value"):
                    raise RuntimeError(
                        "A selected item content fingerprint changed; revalidate before applying"
                    )

    def _journal_sink(self, journal: Any) -> None:
        if not self.store:
            raise RuntimeError("Workspace closed during filesystem operation")
        self.store.save_journal(
            journal.id, journal.plan_id, journal.state, journal_to_dict(journal)
        )

    def _load_sources(self) -> None:
        assert self.store
        self.sources.clear()
        for payload in self.store.list_source_payloads():
            source = SourceRoot(
                path=Path(payload["path"]),
                name=payload["name"],
                id=payload["id"],
                roles={FolderRole(value) for value in payload.get("roles", [])},
                category_ids=set(payload.get("category_ids", [])),
                tag_ids=set(payload.get("tag_ids", [])),
                cloud_policy=CloudPolicy(payload.get("cloud_policy", "none")),
                exclusions=list(payload.get("exclusions", [])),
                max_hierarchy_depth=(
                    int(payload["max_hierarchy_depth"])
                    if payload.get("max_hierarchy_depth") is not None
                    else None
                ),
                policy_revision=int(payload.get("policy_revision", 1)),
            )
            source.capabilities = self.scanner.capabilities(source.path)
            self.sources[source.id] = source


def _cloud_policy_rank(policy: CloudPolicy) -> int:
    return {
        CloudPolicy.NONE: 0,
        CloudPolicy.LOCAL_ONLY: 0,
        CloudPolicy.METADATA_ONLY: 1,
        CloudPolicy.CLOUD_TEXT: 2,
        CloudPolicy.TEXT_AND_IMAGES: 3,
        CloudPolicy.INHERIT: 0,
    }[policy]


def _category_from_payload(payload: dict[str, Any]) -> CategoryDefinition:
    return CategoryDefinition(
        name=str(payload["name"]),
        id=str(payload["id"]),
        parent_id=str(payload["parent_id"]) if payload.get("parent_id") else None,
        description=str(payload.get("description", "")),
        semantic_key=str(payload.get("semantic_key", "")),
        examples=list(payload.get("examples", [])),
        guidance=str(payload.get("guidance", "")),
        sensitivity=Sensitivity(payload.get("sensitivity", "normal")),
        cloud_policy=CloudPolicy(payload.get("cloud_policy", "inherit")),
        default_naming_profile=payload.get("default_naming_profile"),
        allowed_destination_category_ids=set(payload.get("allowed_destination_category_ids", [])),
        allowed_roles={
            FolderRole(value) for value in payload.get("allowed_roles", ["destination", "archive"])
        },
        preferred_destinations={
            str(key): int(value) for key, value in payload.get("preferred_destinations", {}).items()
        },
        max_hierarchy_depth=int(payload.get("max_hierarchy_depth", 3)),
        permitted_kinds=set(payload.get("permitted_kinds", [])),
        default_tag_ids=set(payload.get("default_tag_ids", [])),
        suggest_as_folder=bool(payload.get("suggest_as_folder", False)),
        revision=int(payload.get("revision", 1)),
    )


def _evidence_cloud_rank(value: EvidenceClass) -> int:
    return {
        EvidenceClass.METADATA: 1,
        EvidenceClass.EXTRACTED_TEXT: 2,
        EvidenceClass.VISUAL_CONTENT: 3,
        EvidenceClass.SECRET_LIKE: 99,
    }[value]


def _protected_project_paths(items: list[dict[str, Any]], root_id: str) -> set[str]:
    paths = {
        str(item["relative_path"])
        for item in items
        if item["root_id"] == root_id and item.get("is_project_root")
    }
    if any(
        item["root_id"] == root_id
        and item.get("inside_protected_project")
        and not item.get("protected_project_path")
        for item in items
    ):
        paths.add("")
    paths.update(
        str(item.get("protected_project_path", ""))
        for item in items
        if item["root_id"] == root_id and item.get("protected_project_path")
    )
    return paths
