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
        items = list(requests)
        for request in items:
            root = request.root.resolve(strict=True)
            target = request.path.resolve(strict=False)
            if root != target and root not in target.parents:
                raise PermissionError("Folder creation escapes configured root")
            if target.exists():
                raise FileExistsError(target)
            if not target.parent.exists():
                raise FileNotFoundError("Folder plans must create parents before children")
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
            raise

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
        staged: list[tuple[Path, Path, Path]] = []
        undo_targets = {
            Path(str(operation["target"])).resolve(strict=False)
            for operation in undo.operations
            if operation.get("kind") in {"rename", "move"} and not operation.get("quarantine")
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
                staged.append((source, target, temporary))
                operation["temp"] = str(temporary)
                operation["state"] = "staged"
                self._persist(undo)
            for source, _target, temporary in staged:
                os.rename(temporary, source)
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
        shutil.copytree(source, target, symlinks=True)
        return
    with source.open("rb") as source_stream, target.open("xb") as target_stream:
        shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
        target_stream.flush()
        os.fsync(target_stream.fileno())
    shutil.copystat(source, target, follow_symlinks=False)


def _digest(path: Path) -> str:
    return sha256_file(path) if path.is_file() else _tree_digest(path)


def _tree_digest(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    for child in sorted(path.rglob("*"), key=lambda item: item.as_posix().casefold()):
        relative = child.relative_to(path).as_posix()
        digest.update(relative.encode())
        if child.is_file() and not child.is_symlink():
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
