from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from ai_organizer.adapters.filesystem import (
    FileOperationEngine,
    FileSystemInventory,
    FolderCreateRequest,
    Journal,
    MoveRequest,
    RenameRequest,
    SnapshotToken,
    journal_to_dict,
)
from ai_organizer.adapters.persistence import WorkspaceStore
from ai_organizer.application.services import InventoryService
from ai_organizer.domain.actions import ActionRun, FindingSet, builtin_actions
from ai_organizer.domain.models import (
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
    new_id,
)
from ai_organizer.domain.prompts import (
    CompiledPrompt,
    PromptCompiler,
    PromptLayerKind,
    PromptRevision,
)


class WorkspaceController(QObject):
    workspace_changed = Signal()
    inventory_changed = Signal()
    activity_changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.store: WorkspaceStore | None = None
        self.scanner = FileSystemInventory()
        self.sources: dict[str, SourceRoot] = {}
        self.items: list[dict[str, Any]] = []
        self.selected_item_ids: set[str] = set()
        self.selected_root_ids: set[str] = set()
        self.selected_folders: set[tuple[str, str]] = set()

    def create_workspace(self, path: Path, name: str) -> None:
        self.close()
        self.store = WorkspaceStore.create(path, name)
        personal = CategoryDefinition("Personal", sensitivity=Sensitivity.CONFIDENTIAL)
        work = CategoryDefinition("Work")
        research = CategoryDefinition("Research")
        code = CategoryDefinition("Code")
        media = CategoryDefinition("Media")
        templates = [
            personal,
            CategoryDefinition(
                "Identity", parent_id=personal.id, sensitivity=Sensitivity.RESTRICTED
            ),
            CategoryDefinition(
                "Finance", parent_id=personal.id, sensitivity=Sensitivity.RESTRICTED
            ),
            CategoryDefinition("Health", parent_id=personal.id, sensitivity=Sensitivity.RESTRICTED),
            CategoryDefinition(
                "Travel", parent_id=personal.id, sensitivity=Sensitivity.CONFIDENTIAL
            ),
            work,
            research,
            CategoryDefinition("Papers", parent_id=research.id),
            CategoryDefinition("Data", parent_id=research.id),
            code,
            CategoryDefinition("Active Projects", parent_id=code.id),
            CategoryDefinition("Archived Projects", parent_id=code.id),
            media,
            CategoryDefinition("Uncategorized"),
        ]
        for category in templates:
            self.store.save_category(category)
        for action in builtin_actions():
            self.store.save_action(action)
        self.store.activity("workspace.created", f"Created workspace {name}")
        self.workspace_changed.emit()

    def open_workspace(self, path: Path) -> None:
        self.close()
        self.store = WorkspaceStore(path)
        self._load_sources()
        self.items = self.store.list_items()
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

    def add_source(
        self,
        path: Path,
        roles: set[FolderRole] | None = None,
        cloud_policy: CloudPolicy = CloudPolicy.NONE,
        category_ids: set[str] | None = None,
        exclusions: list[str] | None = None,
    ) -> SourceRoot:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        candidate = SourceRoot(
            path.resolve(strict=True),
            path.name or str(path),
            roles=roles or {FolderRole.INBOX},
            cloud_policy=cloud_policy,
            category_ids=category_ids or set(),
            exclusions=exclusions or [],
        )
        InventoryService.validate_non_overlapping([*self.sources.values(), candidate])
        candidate.capabilities = self.scanner.capabilities(candidate.path)
        self.sources[candidate.id] = candidate
        self.store.save_source(candidate)
        self.store.activity("source.added", f"Added source {candidate.name}")
        self.workspace_changed.emit()
        return candidate

    def scan_all(self) -> int:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        service = InventoryService(self.scanner)
        total = 0
        for source in self.sources.values():
            run = service.scan_root(source)
            self.store.save_source(source)
            self.store.save_snapshot(run.id, source.id, list(run.items))
            total += len(run.items)
        self.items = self.store.list_items()
        self.store.activity("inventory.complete", f"Inventoried {total} items")
        self.inventory_changed.emit()
        self.activity_changed.emit()
        return total

    def save_prompt_revision(self, revision: PromptRevision) -> None:
        if not self.store:
            raise RuntimeError("Open a workspace first")
        self.store.save_prompt_revision(revision)
        view = revision.profile_id.removeprefix("view:")
        affected = {
            "rename": {"rename"},
            "folder": {"folder", "move"},
            "move": {"move"},
            "action": {"finding", "move"},
        }.get(view, {"rename", "folder", "move", "finding"})
        stale = self.store.mark_proposals_stale(affected, "Prompt revision changed")
        self.store.activity("prompt.revision", f"Saved {revision.profile_id}")
        if stale:
            self.store.activity("proposal.stale", f"Marked {stale} proposal set(s) stale")
        self.activity_changed.emit()

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
        view_revision = (
            PromptRevision(f"view:{view_key}", PromptLayerKind.VIEW, view_text)
            if view_text.strip()
            else None
        )
        return PromptCompiler().compile(
            provider=provider,
            model=model,
            workspace=workspace_revision,
            view=view_revision,
            categories=category_revisions,
            evidence=evidence,
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
            if not proposed_name or Path(proposed_name).name != proposed_name:
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
        if not self.store:
            return False, "Workspace is not open"
        source = self.sources[root_id]
        if source.cloud_policy != CloudPolicy.TEXT_AND_IMAGES:
            return False, f"Cloud processing is disabled for source {source.name}"
        categories = {value["id"]: value for value in self.store.list_category_payloads()}
        for category_id in source.category_ids:
            category = categories.get(category_id)
            if category and category.get("cloud_policy") == CloudPolicy.NONE.value:
                return False, f"Category {category['name']} prohibits cloud processing"
        return True, "Root and category policies allow text and rendered images"

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
        categories = {value["id"]: value for value in self.store.list_category_payloads()}
        for row in selected:
            root = self.sources[str(row["root_id"])].path
            projected = str(row["projected"]).strip()
            relative = Path(projected)
            if not projected or relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"Projected folder is invalid: {projected!r}")
            category = categories.get(str(row.get("category_id", "")), {})
            maximum_depth = int(category.get("max_hierarchy_depth", 4))
            if len(relative.parts) > maximum_depth:
                raise ValueError(
                    f"Projected hierarchy exceeds its maximum depth of {maximum_depth}"
                )
            target = root / relative
            current = str(row.get("current", "")).strip()
            if current and current != "—":
                if current == projected:
                    continue
                source = root / current
                if source.parent.resolve(strict=True) != target.parent.resolve(strict=True):
                    raise ValueError("Folder Plan may rename in place but cannot reparent folders")
                rename_requests.append(RenameRequest(source, target, SnapshotToken.capture(source)))
                operations.append(
                    {"kind": "folder_rename", "source": str(source), "target": str(target)}
                )
            else:
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
            engine.execute_folder_creates(plan.id, requests)
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
            source = source_root.path / str(row["relative_path"])
            target_folder = destination_root.path / str(row.get("destination", ""))
            target = target_folder / source.name
            token = SnapshotToken.capture(source)
            requests.append(
                MoveRequest(
                    source,
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
                    "source": str(source),
                    "target": str(target),
                    "snapshot": token.sha256,
                }
            )
            proposal_set.items.append(
                ProposalItem(
                    str(row["item_id"]),
                    str(target_folder),
                    ProposalKind.MOVE,
                    current_value=str(source.parent),
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
                cloud_policy=CloudPolicy(payload.get("cloud_policy", "none")),
                exclusions=list(payload.get("exclusions", [])),
                policy_revision=int(payload.get("policy_revision", 1)),
            )
            source.capabilities = self.scanner.capabilities(source.path)
            self.sources[source.id] = source
