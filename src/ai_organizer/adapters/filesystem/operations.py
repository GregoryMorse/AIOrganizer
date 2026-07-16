from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from uuid import uuid4

from ai_organizer.domain.models import new_id, utc_now

from .inventory import sha256_file


@dataclass(frozen=True, slots=True)
class SnapshotToken:
    path: Path
    size: int
    modified_ns: int
    file_id: str
    sha256: str

    @classmethod
    def capture(cls, path: Path) -> SnapshotToken:
        info = path.stat(follow_symlinks=False)
        return cls(
            path.resolve(strict=True),
            info.st_size if path.is_file() else _tree_size(path),
            info.st_mtime_ns,
            f"{info.st_dev}:{info.st_ino}",
            sha256_file(path) if path.is_file() else _tree_digest(path),
        )

    def validate(self) -> list[str]:
        issues: list[str] = []
        if not self.path.exists():
            return ["source no longer exists"]
        info = self.path.stat(follow_symlinks=False)
        current_size = info.st_size if self.path.is_file() else _tree_size(self.path)
        if current_size != self.size:
            issues.append("source size changed")
        if info.st_mtime_ns != self.modified_ns:
            issues.append("source modification time changed")
        if f"{info.st_dev}:{info.st_ino}" != self.file_id:
            issues.append("source filesystem identity changed")
        if _digest(self.path) != self.sha256:
            issues.append("source content hash changed")
        return issues


@dataclass(frozen=True, slots=True)
class RenameRequest:
    source: Path
    target: Path
    snapshot: SnapshotToken
    id: str = field(default_factory=lambda: new_id("rename"))


@dataclass(frozen=True, slots=True)
class MoveRequest:
    source: Path
    target: Path
    snapshot: SnapshotToken
    source_root: Path
    destination_root: Path
    source_volume_id: str
    destination_volume_id: str
    id: str = field(default_factory=lambda: new_id("move"))


@dataclass(frozen=True, slots=True)
class FolderCreateRequest:
    path: Path
    root: Path
    id: str = field(default_factory=lambda: new_id("folder_create"))


@dataclass(frozen=True, slots=True)
class CleanupRequest:
    source: Path
    root: Path
    snapshot: SnapshotToken
    cleanup_kind: str
    id: str = field(default_factory=lambda: new_id("cleanup"))


@dataclass(slots=True)
class Journal:
    plan_id: str
    id: str = field(default_factory=lambda: new_id("journal"))
    state: str = "prepared"
    operations: list[dict[str, object]] = field(default_factory=list)
    updated_at: str = field(default_factory=utc_now)


JournalSink = Callable[[Journal], None]


class FileOperationEngine:
    def __init__(self, journal_sink: JournalSink) -> None:
        self._journal_sink = journal_sink

    def execute_renames(self, plan_id: str, requests: Iterable[RenameRequest]) -> Journal:
        items = list(requests)
        self._preflight_renames(items)
        journal = Journal(plan_id)
        journal.operations = [
            {
                "id": item.id,
                "kind": "rename",
                "source": str(item.source),
                "target": str(item.target),
                "snapshot": item.snapshot.sha256,
                "state": "prepared",
            }
            for item in items
        ]
        self._persist(journal)
        temporary: list[tuple[RenameRequest, Path]] = []
        try:
            journal.state = "executing"
            self._persist(journal)
            for index, request in enumerate(items):
                temp = request.source.with_name(f".aiorganizer-tmp-{uuid4().hex}")
                os.rename(request.source, temp)
                temporary.append((request, temp))
                journal.operations[index]["temp"] = str(temp)
                journal.operations[index]["state"] = "staged"
                self._persist(journal)
            for index, (request, temp) in enumerate(temporary):
                os.rename(temp, request.target)
                journal.operations[index]["state"] = "verified"
                self._persist(journal)
            journal.state = "completed"
            self._persist(journal)
            return journal
        except Exception as error:
            journal.state = "partially_failed"
            journal.operations.append({"error": str(error), "state": "error"})
            self._persist(journal)
            self._rollback_renames(temporary, journal)
            raise

    def execute_folder_creates(
        self, plan_id: str, requests: Iterable[FolderCreateRequest]
    ) -> Journal:
        items = sorted(requests, key=lambda request: len(request.path.parts))
        planned: set[Path] = set()
        target_keys: set[str] = set()
        for request in items:
            root = request.root.resolve(strict=True)
            target = request.path.resolve(strict=False)
            if root != target and root not in target.parents:
                raise PermissionError("Folder creation escapes configured root")
            if target.exists():
                raise FileExistsError(target)
            target_key = str(target).casefold()
            if target_key in target_keys:
                raise FileExistsError(f"Duplicate folder target: {target}")
            target_keys.add(target_key)
            if not target.parent.exists() and target.parent not in planned:
                raise FileNotFoundError("Folder plans must create parents before children")
            planned.add(target)
        journal = Journal(plan_id)
        journal.operations = [
            {"id": item.id, "kind": "folder_create", "target": str(item.path), "state": "prepared"}
            for item in items
        ]
        self._persist(journal)
        created: list[Path] = []
        try:
            journal.state = "executing"
            self._persist(journal)
            for index, request in enumerate(items):
                request.path.mkdir()
                created.append(request.path)
                journal.operations[index]["state"] = "verified"
                self._persist(journal)
            journal.state = "completed"
            self._persist(journal)
            return journal
        except Exception as error:
            journal.state = "partially_failed"
            journal.operations.append({"error": str(error), "state": "error"})
            for path in reversed(created):
                try:
                    path.rmdir()
                except OSError as rollback_error:
                    journal.operations.append(
                        {"rollback_error": str(rollback_error), "state": "recovery_required"}
                    )
            journal.state = (
                "recovery_required"
                if any("rollback_error" in operation for operation in journal.operations)
                else "rolled_back"
            )
            self._persist(journal)
            raise

    def execute_moves(self, plan_id: str, requests: Iterable[MoveRequest]) -> Journal:
        items = list(requests)
        self._preflight_moves(items)
        journal = Journal(plan_id)
        journal.operations = [
            {
                "id": item.id,
                "kind": "move",
                "source": str(item.source),
                "target": str(item.target),
                "source_root": str(item.source_root),
                "destination_root": str(item.destination_root),
                "snapshot": item.snapshot.sha256,
                "state": "prepared",
            }
            for item in items
        ]
        self._persist(journal)
        journal.state = "executing"
        self._persist(journal)
        completed: list[tuple[MoveRequest, Path | None]] = []
        try:
            for index, request in enumerate(items):
                if _same_volume(request.source, request.target.parent):
                    os.rename(request.source, request.target)
                    completed.append((request, None))
                else:
                    partial = request.target.with_name(f".aiorganizer-partial-{uuid4().hex}")
                    journal.operations[index]["partial"] = str(partial)
                    self._persist(journal)
                    _copy(request.source, partial)
                    if _digest(partial) != request.snapshot.sha256:
                        _remove_partial(partial)
                        raise OSError("Cross-volume copy hash verification failed")
                    os.rename(partial, request.target)
                    quarantine = self._quarantine_path(request)
                    quarantine.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(request.source, quarantine)
                    completed.append((request, quarantine))
                    journal.operations[index]["quarantine"] = str(quarantine)
                if _digest(request.target) != request.snapshot.sha256:
                    raise OSError("Final destination hash verification failed")
                journal.operations[index]["state"] = "verified"
                self._persist(journal)
            journal.state = "completed"
            self._persist(journal)
            return journal
        except Exception as error:
            journal.state = "partially_failed"
            journal.operations.append({"error": str(error), "state": "error"})
            self._persist(journal)
            self._rollback_moves(completed, journal)
            for operation in journal.operations:
                partial_value = operation.get("partial")
                source_value = operation.get("source")
                if partial_value and source_value and Path(str(source_value)).exists():
                    _remove_partial(Path(str(partial_value)))
            raise

    def execute_cleanup(self, plan_id: str, requests: Iterable[CleanupRequest]) -> Journal:
        items = list(requests)
        self._preflight_cleanup(items)
        journal = Journal(plan_id)
        journal.operations = []
        for request in items:
            relative = request.source.resolve(strict=False).relative_to(
                request.root.resolve(strict=False)
            )
            target = request.root / ".AIOrganizer-Cleanup-Quarantine" / journal.id / relative
            journal.operations.append(
                {
                    "id": request.id,
                    "kind": "cleanup",
                    "cleanup_kind": request.cleanup_kind,
                    "source": str(request.source),
                    "target": str(target),
                    "snapshot": request.snapshot.sha256,
                    "state": "prepared",
                }
            )
        self._persist(journal)
        completed: list[tuple[Path, Path]] = []
        try:
            journal.state = "executing"
            self._persist(journal)
            for operation in journal.operations:
                source = Path(str(operation["source"]))
                target = Path(str(operation["target"]))
                target.parent.mkdir(parents=True, exist_ok=True)
                os.rename(source, target)
                completed.append((source, target))
                expected = str(operation.get("snapshot", ""))
                if expected and _digest(target) != expected:
                    raise OSError(f"Cleanup quarantine verification failed: {source}")
                operation["state"] = "verified"
                self._persist(journal)
            journal.state = "completed"
            self._persist(journal)
            return journal
        except Exception as error:
            journal.state = "partially_failed"
            journal.operations.append({"error": str(error), "state": "error"})
            self._persist(journal)
            for source, target in reversed(completed):
                try:
                    if target.exists() and not source.exists():
                        source.parent.mkdir(parents=True, exist_ok=True)
                        os.rename(target, source)
                except OSError as rollback_error:
                    journal.operations.append(
                        {"rollback_error": str(rollback_error), "state": "recovery_required"}
                    )
            journal.state = (
                "recovery_required"
                if any("rollback_error" in operation for operation in journal.operations)
                else "rolled_back"
            )
            self._persist(journal)
            raise

    def recover_incomplete(self, incomplete: Journal) -> Journal:
        """Safely roll an interrupted journal back after inspecting observed filesystem state."""
        if incomplete.state in {"completed", "rolled_back", "verified", "undone"}:
            raise ValueError("Journal does not require recovery")
        staged: list[tuple[dict[str, object], Path, Path]] = []
        try:
            incomplete.state = "recovering"
            self._persist(incomplete)
            for operation in incomplete.operations:
                kind = str(operation.get("kind", ""))
                if kind not in {"rename", "move", "cleanup"}:
                    continue
                source = Path(str(operation["source"]))
                target = Path(str(operation["target"]))
                partial_value = operation.get("partial")
                if partial_value:
                    partial = Path(str(partial_value))
                    if partial.exists() and source.exists():
                        _remove_partial(partial)
                location = self._recovery_location(operation, source, target)
                if location is None:
                    continue
                recovery_temp = location.with_name(f".aiorganizer-recover-{uuid4().hex}")
                os.rename(location, recovery_temp)
                staged.append((operation, source, recovery_temp))
                operation["recovery_temp"] = str(recovery_temp)
                operation["state"] = "recovery_staged"
                self._persist(incomplete)
            for operation, source, recovery_temp in staged:
                if source.exists():
                    raise FileExistsError(f"Recovery source is occupied: {source}")
                os.rename(recovery_temp, source)
                expected = str(operation.get("snapshot", ""))
                if expected and _digest(source) != expected:
                    raise OSError(f"Recovered source hash differs: {source}")
                if operation.get("quarantine"):
                    target = Path(str(operation["target"]))
                    if target.exists():
                        if expected and _digest(target) != expected:
                            raise OSError(f"Cross-volume recovery target hash differs: {target}")
                        _remove_partial(target)
                operation["state"] = "rolled_back"
                self._persist(incomplete)
            for operation in reversed(incomplete.operations):
                if operation.get("kind") != "folder_create":
                    continue
                target = Path(str(operation["target"]))
                if target.exists():
                    target.rmdir()
                operation["state"] = "rolled_back"
                self._persist(incomplete)
            incomplete.state = "rolled_back"
            self._persist(incomplete)
            return incomplete
        except Exception as error:
            incomplete.state = "recovery_required"
            incomplete.operations.append({"error": str(error), "state": "recovery_error"})
            self._persist(incomplete)
            raise

    @staticmethod
    def _recovery_location(operation: dict[str, object], source: Path, target: Path) -> Path | None:
        recovery_value = operation.get("recovery_temp")
        if recovery_value and Path(str(recovery_value)).exists():
            return Path(str(recovery_value))
        quarantine_value = operation.get("quarantine")
        if quarantine_value and Path(str(quarantine_value)).exists():
            return Path(str(quarantine_value))
        temporary_value = operation.get("temp")
        if temporary_value and Path(str(temporary_value)).exists():
            return Path(str(temporary_value))
        state = str(operation.get("state", "prepared"))
        if target.exists() and (state != "prepared" or not source.exists()):
            return target
        if source.exists():
            return None
        if target.exists():
            return target
        raise FileNotFoundError(f"Cannot locate interrupted operation source: {source}")

    def execute_undo(self, plan_id: str, completed: Journal) -> Journal:
        if completed.state != "completed":
            raise ValueError("Only a completed journal can be undone")
        undo = Journal(plan_id)
        undo.operations = [
            {**operation, "state": "undo_prepared"}
            for operation in reversed(completed.operations)
            if operation.get("kind")
        ]
        self._persist(undo)
        staged: list[tuple[dict[str, object], Path, Path, Path]] = []
        undo_targets = {
            Path(str(operation["target"])).resolve(strict=False)
            for operation in undo.operations
            if operation.get("kind") in {"rename", "move", "cleanup"}
            and not operation.get("quarantine")
        }
        try:
            undo.state = "executing"
            self._persist(undo)
            for operation in undo.operations:
                kind = str(operation["kind"])
                source = Path(str(operation.get("source", "")))
                target = Path(str(operation["target"]))
                if kind == "folder_create":
                    target.rmdir()
                    operation["state"] = "verified"
                    self._persist(undo)
                    continue
                quarantine_value = operation.get("quarantine")
                if quarantine_value:
                    quarantine = Path(str(quarantine_value))
                    if source.exists() or not quarantine.exists() or not target.exists():
                        raise OSError("Cross-volume undo preconditions changed")
                    expected = _digest(target)
                    os.rename(quarantine, source)
                    if _digest(source) != expected:
                        raise OSError("Restored quarantine hash verification failed")
                    _remove_partial(target)
                    operation["state"] = "verified"
                    self._persist(undo)
                    continue
                source_is_another_target = source.resolve(strict=False) in undo_targets
                if (source.exists() and not source_is_another_target) or not target.exists():
                    raise OSError("Undo preconditions changed")
                temporary = target.with_name(f".aiorganizer-undo-{uuid4().hex}")
                os.rename(target, temporary)
                staged.append((operation, source, target, temporary))
                operation["temp"] = str(temporary)
                operation["state"] = "staged"
                self._persist(undo)
            for operation, source, _target, temporary in staged:
                os.rename(temporary, source)
                expected = str(operation.get("snapshot", ""))
                if expected and _digest(source) != expected:
                    raise OSError(f"Undo content verification failed: {source}")
            for operation in undo.operations:
                operation["state"] = "verified"
            undo.state = "completed"
            self._persist(undo)
            return undo
        except Exception as error:
            undo.state = "recovery_required"
            undo.operations.append({"error": str(error), "state": "error"})
            self._persist(undo)
            raise

    def _preflight_renames(self, requests: list[RenameRequest]) -> None:
        sources = {request.source.resolve(strict=False) for request in requests}
        targets: set[str] = set()
        for request in requests:
            if request.source.parent.resolve(strict=True) != request.target.parent.resolve(
                strict=True
            ):
                raise ValueError("Rename operations may not change parent folders")
            issues = request.snapshot.validate()
            if issues:
                raise OSError(f"Stale source {request.source}: {', '.join(issues)}")
            target_key = str(request.target.resolve(strict=False)).casefold()
            if target_key in targets:
                raise FileExistsError(f"Duplicate target: {request.target}")
            targets.add(target_key)
            if request.target.exists() and request.target.resolve(strict=False) not in sources:
                raise FileExistsError(request.target)

    def _preflight_moves(self, requests: list[MoveRequest]) -> None:
        targets: set[str] = set()
        for request in requests:
            source_resolved = request.source.resolve(strict=True)
            root_resolved = request.source_root.resolve(strict=True)
            if str(root_resolved.stat().st_dev) != request.source_volume_id:
                raise OSError("Source volume identity changed")
            if source_resolved != root_resolved and root_resolved not in source_resolved.parents:
                raise PermissionError("Move source escapes configured root")
            destination_root = request.destination_root.resolve(strict=True)
            if str(destination_root.stat().st_dev) != request.destination_volume_id:
                raise OSError("Destination volume identity changed")
            target_resolved = request.target.resolve(strict=False)
            if (
                target_resolved != destination_root
                and destination_root not in target_resolved.parents
            ):
                raise PermissionError("Move target escapes configured destination root")
            if request.target.exists():
                raise FileExistsError(request.target)
            if not request.target.parent.exists():
                raise FileNotFoundError(
                    "Destination folder must be created through an accepted Folder Plan first"
                )
            key = str(request.target.resolve(strict=False)).casefold()
            if key in targets:
                raise FileExistsError(f"Duplicate target: {request.target}")
            targets.add(key)
            issues = request.snapshot.validate()
            if issues:
                raise OSError(f"Stale source {request.source}: {', '.join(issues)}")
            if not _same_volume(request.source, request.target.parent):
                required = request.snapshot.size
                free = shutil.disk_usage(request.target.parent).free
                if free < required:
                    raise OSError("Insufficient destination space for verified cross-volume move")

    def _preflight_cleanup(self, requests: list[CleanupRequest]) -> None:
        sources: set[Path] = set()
        for request in requests:
            root = request.root.resolve(strict=True)
            source = request.source.resolve(strict=True)
            if source == root or root not in source.parents:
                raise PermissionError("Cleanup source escapes configured root")
            if any(
                part.casefold() in {".aiorganizer-cleanup-quarantine", ".aiorganizer-quarantine"}
                for part in source.relative_to(root).parts
            ):
                raise PermissionError("Cleanup cannot quarantine an existing quarantine path")
            if source in sources:
                raise ValueError(f"Duplicate cleanup source: {source}")
            if any(parent in sources for parent in source.parents):
                raise ValueError("Cleanup sources may not overlap")
            if any(source in existing.parents for existing in sources):
                raise ValueError("Cleanup sources may not overlap")
            sources.add(source)
            issues = request.snapshot.validate()
            if issues:
                raise OSError(f"Stale cleanup source {request.source}: {', '.join(issues)}")

    def _rollback_renames(self, staged: list[tuple[RenameRequest, Path]], journal: Journal) -> None:
        for request, temp in reversed(staged):
            try:
                current = request.target if request.target.exists() else temp
                if current.exists() and not request.source.exists():
                    os.rename(current, request.source)
            except OSError as error:
                journal.operations.append(
                    {"rollback_error": str(error), "state": "recovery_required"}
                )
        journal.state = (
            "rolled_back"
            if not any("rollback_error" in op for op in journal.operations)
            else "recovery_required"
        )
        self._persist(journal)

    def _rollback_moves(
        self, completed: list[tuple[MoveRequest, Path | None]], journal: Journal
    ) -> None:
        for request, quarantine in reversed(completed):
            try:
                if quarantine and quarantine.exists() and not request.source.exists():
                    os.rename(quarantine, request.source)
                    _remove_partial(request.target)
                elif request.target.exists() and not request.source.exists():
                    os.rename(request.target, request.source)
            except OSError as error:
                journal.operations.append(
                    {"rollback_error": str(error), "state": "recovery_required"}
                )
        journal.state = (
            "rolled_back"
            if not any("rollback_error" in op for op in journal.operations)
            else "recovery_required"
        )
        self._persist(journal)

    def _quarantine_path(self, request: MoveRequest) -> Path:
        relative = request.source.resolve(strict=False).relative_to(
            request.source_root.resolve(strict=False)
        )
        return request.source_root / ".AIOrganizer-Quarantine" / request.id / relative

    def _persist(self, journal: Journal) -> None:
        journal.updated_at = utc_now()
        self._journal_sink(journal)


def journal_to_dict(journal: Journal) -> dict[str, object]:
    return asdict(journal)


def _same_volume(source: Path, target_parent: Path) -> bool:
    return source.stat().st_dev == target_parent.stat().st_dev


def _copy(source: Path, target: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, target, symlinks=True, copy_function=_copy_file_durable)
        return
    with source.open("rb") as source_stream, target.open("xb") as target_stream:
        shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
        target_stream.flush()
        os.fsync(target_stream.fileno())
    shutil.copystat(source, target, follow_symlinks=False)


def _copy_file_durable(source: str, target: str) -> str:
    copied = shutil.copy2(source, target, follow_symlinks=False)
    with Path(copied).open("rb") as stream:
        os.fsync(stream.fileno())
    return copied


def _digest(path: Path) -> str:
    return sha256_file(path) if path.is_file() else _tree_digest(path)


def _tree_digest(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    for child in sorted(path.rglob("*"), key=lambda item: item.as_posix().casefold()):
        relative = child.relative_to(path).as_posix()
        digest.update(relative.encode())
        if child.is_symlink():
            digest.update(b"symlink:")
            digest.update(os.readlink(child).encode())
        elif child.is_file():
            digest.update(sha256_file(child).encode())
    return digest.hexdigest()


def _tree_size(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        if child.is_file() and not child.is_symlink():
            total += child.stat(follow_symlinks=False).st_size
    return total


def _remove_partial(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
