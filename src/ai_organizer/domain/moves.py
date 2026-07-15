from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True, slots=True)
class MoveCandidate:
    item_id: str
    source_root_id: str
    destination_root_id: str
    source_relative_path: str
    destination_folder: str
    filename: str
    is_directory: bool = False
    is_project_root: bool = False
    inside_protected_project: bool = False

    @property
    def target_relative_path(self) -> str:
        folder = self.destination_folder.replace("\\", "/").strip("/")
        return (PurePosixPath(folder) / self.filename).as_posix() if folder else self.filename


@dataclass(frozen=True, slots=True)
class MoveProjection:
    item_id: str
    target_relative_path: str
    status: str
    issues: tuple[str, ...]


class ProjectedMoveValidator:
    def validate(
        self,
        candidates: list[MoveCandidate],
        existing_paths: dict[str, set[str]],
        projected_folders: dict[str, set[str]],
        protected_project_paths: dict[str, set[str]],
        case_sensitive_roots: dict[str, bool],
    ) -> dict[str, MoveProjection]:
        result: dict[str, MoveProjection] = {}
        target_owners: dict[tuple[str, str], list[str]] = {}
        for candidate in candidates:
            sensitive = case_sensitive_roots.get(candidate.destination_root_id, True)
            target = candidate.target_relative_path
            key = (candidate.destination_root_id, _key(target, sensitive))
            target_owners.setdefault(key, []).append(candidate.item_id)

        for candidate in candidates:
            issues: list[str] = []
            sensitive = case_sensitive_roots.get(candidate.destination_root_id, True)
            target = candidate.target_relative_path
            target_key = _key(target, sensitive)
            if candidate.inside_protected_project:
                issues.append("Generic moves cannot move an item from inside a protected project")
            if candidate.is_directory and not candidate.is_project_root:
                issues.append("Arbitrary directory-tree moves are blocked")
            if not candidate.filename or any(value in candidate.filename for value in ("/", "\\")):
                issues.append("Move target filename is not one component")
            if candidate.destination_folder:
                raw_folder = candidate.destination_folder.replace("\\", "/").strip()
                if raw_folder.startswith("/") or (
                    len(raw_folder) >= 2
                    and raw_folder[0].isalpha()
                    and raw_folder[1] == ":"
                ):
                    issues.append("Destination folder must be relative to its configured root")
                folder = raw_folder.strip("/")
                parts = PurePosixPath(folder).parts
                if any(part in {"", ".", ".."} for part in parts):
                    issues.append("Destination folder is not a contained relative path")
                available = {
                    _key(value, sensitive)
                    for value in projected_folders.get(candidate.destination_root_id, set())
                }
                if _key(folder, sensitive) not in available:
                    issues.append("Destination folder is absent from current/projected hierarchy")
            occupied = {
                _key(value, sensitive)
                for value in existing_paths.get(candidate.destination_root_id, set())
            }
            if target_key in occupied:
                issues.append(f"Projected move target already exists: {target}")
            if len(target_owners[(candidate.destination_root_id, target_key)]) > 1:
                issues.append(f"Multiple selected moves share target: {target}")
            target_folder = PurePosixPath(target).parent
            for project_path in protected_project_paths.get(candidate.destination_root_id, set()):
                project = PurePosixPath(project_path)
                if target_folder == project or project in target_folder.parents:
                    issues.append("Generic moves cannot target inside a protected project")
                    break
            result[candidate.item_id] = MoveProjection(
                candidate.item_id,
                target,
                "blocked" if issues else "ready",
                tuple(issues),
            )
        return result


def _key(path: str, case_sensitive: bool) -> str:
    normalized = path.replace("\\", "/")
    return normalized if case_sensitive else normalized.casefold()
