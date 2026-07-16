from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from ai_organizer.adapters.storage_inventory import StorageInventory


class InventoryQueryService:
    """Bounded read-only discovery over persisted inventory records."""

    def __init__(
        self,
        items: Sequence[Mapping[str, Any]],
        sources: Sequence[Mapping[str, Any]] = (),
        cache_stats: Mapping[str, Any] | None = None,
        organization_context: Mapping[str, Any] | None = None,
        storage_inventory: StorageInventory | None = None,
    ) -> None:
        self.items = list(items)
        self.sources = list(sources)
        self.cache_stats = dict(cache_stats or {})
        self.organization_context = dict(organization_context or {})
        self.storage_inventory = storage_inventory or StorageInventory()

    def organization_taxonomy(self) -> dict[str, Any]:
        """Return approved vocabulary and hierarchy constraints, never inferred content."""
        return dict(self.organization_context)

    def storage_volumes(self) -> dict[str, Any]:
        """Return mounted volume capacity and configured-source coverage."""
        volumes = self.storage_inventory.list_volumes([dict(value) for value in self.sources])
        return {
            "volumes": volumes,
            "total": len(volumes),
            "uncovered_volume_count": sum(
                1 for value in volumes if not value.get("has_configured_source")
            ),
        }

    def storage_list_directory(
        self,
        volume_id: str,
        relative_path: str = "",
        *,
        include_hidden: bool = False,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """List names and stat metadata only; never read file content."""
        return self.storage_inventory.list_directory(
            volume_id,
            relative_path,
            include_hidden=include_hidden,
            offset=offset,
            limit=limit,
        )

    def list_roots(self) -> list[dict[str, Any]]:
        counts = Counter(str(item.get("root_id", "")) for item in self.items)
        return [
            {
                "root_id": str(source.get("id", "")),
                "name": str(source.get("name", "")),
                "roles": list(source.get("roles", [])),
                "category_ids": list(source.get("category_ids", [])),
                "tag_ids": list(source.get("tag_ids", [])),
                "item_count": counts[str(source.get("id", ""))],
            }
            for source in self.sources
        ]

    def search(
        self,
        glob: str = "**",
        *,
        extensions: Sequence[str] = (),
        root_ids: set[str] | None = None,
        item_type: Literal["any", "file", "folder"] = "any",
        min_size: int | None = None,
        max_size: int | None = None,
        modified_after_ns: int | None = None,
        modified_before_ns: int | None = None,
        offset: int = 0,
        limit: int = 100,
        include_metadata: bool = True,
    ) -> dict[str, Any]:
        matcher = compile_inventory_glob(glob)
        normalized_extensions = {
            value.casefold() if value.startswith(".") else f".{value.casefold()}"
            for value in extensions
            if value.strip()
        }
        matches: list[Mapping[str, Any]] = []
        for item in self.items:
            if root_ids and str(item.get("root_id")) not in root_ids:
                continue
            is_dir = bool(item.get("is_dir"))
            if (item_type == "file" and is_dir) or (item_type == "folder" and not is_dir):
                continue
            path = str(item.get("relative_path", "")).replace("\\", "/")
            if not matcher.fullmatch(path):
                continue
            extension = str(item.get("extension") or _extension(path)).casefold()
            if normalized_extensions and extension not in normalized_extensions:
                continue
            size = int(item.get("size", 0))
            modified = int(item.get("modified_ns", 0))
            if min_size is not None and size < min_size:
                continue
            if max_size is not None and size > max_size:
                continue
            if modified_after_ns is not None and modified <= modified_after_ns:
                continue
            if modified_before_ns is not None and modified >= modified_before_ns:
                continue
            matches.append(item)
        start = max(0, offset)
        bounded = max(1, min(limit, 250))
        page = matches[start : start + bounded]
        return {
            "items": [_public_item(item, include_metadata) for item in page],
            "total": len(matches),
            "offset": start,
            "limit": bounded,
            "has_more": start + bounded < len(matches),
            "glob": glob,
        }

    def list_children(
        self,
        *,
        root_id: str,
        parent_item_id: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        parent_path = ""
        if parent_item_id:
            parent = next(
                (
                    item
                    for item in self.items
                    if str(item.get("id")) == parent_item_id
                    and str(item.get("root_id")) == root_id
                    and item.get("is_dir")
                ),
                None,
            )
            if parent is None:
                raise ValueError("Unknown folder item identifier")
            parent_path = str(parent.get("relative_path", ""))
        children = [
            item
            for item in self.items
            if str(item.get("root_id")) == root_id
            and str(
                item.get("parent_path")
                if item.get("parent_path") is not None
                else _parent_path(str(item.get("relative_path", "")))
            )
            == parent_path
        ]
        start = max(0, offset)
        bounded = max(1, min(limit, 250))
        return {
            "items": [_public_item(item, True) for item in children[start : start + bounded]],
            "total": len(children),
            "offset": start,
            "has_more": start + bounded < len(children),
        }

    def summary(self, glob: str = "**", root_ids: set[str] | None = None) -> dict[str, Any]:
        result = self.search(glob, root_ids=root_ids, limit=1, include_metadata=False)
        matcher = compile_inventory_glob(glob)
        scoped = [
            item
            for item in self.items
            if (not root_ids or str(item.get("root_id")) in root_ids)
            and matcher.fullmatch(str(item.get("relative_path", "")).replace("\\", "/"))
        ]
        files = [item for item in scoped if not item.get("is_dir")]
        folders = [item for item in scoped if item.get("is_dir")]
        extensions = Counter(
            str(item.get("extension") or _extension(str(item.get("relative_path", "")))).casefold()
            or "[none]"
            for item in files
        )
        mime_types = Counter(str(item.get("mime_type", "unknown")) for item in files)
        top_level = Counter(
            (str(item.get("relative_path", "")).replace("\\", "/").split("/", 1)[0] or "[root]")
            for item in files
        )
        health_status = Counter(
            str(item.get("metadata", {}).get("file_health_status", "not_inspected"))
            for item in files
        )
        health_codes = Counter(
            str(issue.get("code", "unknown"))
            for item in files
            for issue in item.get("metadata", {}).get("file_health_issues", [])
            if isinstance(issue, Mapping)
        )
        return {
            "glob": glob,
            "total": result["total"],
            "files": len(files),
            "folders": len(folders),
            "total_file_bytes": sum(int(item.get("size", 0)) for item in files),
            "by_extension": dict(extensions.most_common()),
            "by_mime_type": dict(mime_types.most_common()),
            "by_top_level_folder": dict(top_level.most_common(100)),
            "by_file_health_status": dict(health_status.most_common()),
            "by_file_health_issue_code": dict(health_codes.most_common(100)),
            "oldest_modified_ns": min(
                (int(item.get("modified_ns", 0)) for item in scoped), default=None
            ),
            "newest_modified_ns": max(
                (int(item.get("modified_ns", 0)) for item in scoped), default=None
            ),
            "metadata_cache": self.cache_stats,
        }

    def list_file_issues(
        self,
        *,
        root_ids: set[str] | None = None,
        severity: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return parser-reported issues without treating warnings as corruption claims."""
        values = []
        for item in self.items:
            if item.get("is_dir") or (root_ids and str(item.get("root_id", "")) not in root_ids):
                continue
            issues = [
                dict(value)
                for value in item.get("metadata", {}).get("file_health_issues", [])
                if isinstance(value, Mapping)
                and (not severity or str(value.get("severity")) == severity)
            ]
            if not issues:
                continue
            values.append(
                {
                    "item_id": str(item.get("id", "")),
                    "root_id": str(item.get("root_id", "")),
                    "relative_path": str(item.get("relative_path", "")),
                    "status": str(item.get("metadata", {}).get("file_health_status", "warning")),
                    "issues": issues[:50],
                }
            )
        start = max(0, offset)
        bounded = max(1, min(250, limit))
        return {
            "items": values[start : start + bounded],
            "total": len(values),
            "offset": start,
            "limit": bounded,
            "has_more": start + bounded < len(values),
            "interpretation": (
                "Warnings mean a parser recovered from a nonstandard or inconsistent structure; "
                "they are not automatic proof of corruption."
            ),
        }

    def folder_tree(
        self,
        *,
        root_ids: set[str] | None = None,
        max_depth: int | None = None,
        offset: int = 0,
        limit: int = 250,
    ) -> dict[str, Any]:
        """Return a bounded flat hierarchy that is cheap for models to reason over."""
        depth_limit = max(1, min(12, max_depth)) if max_depth else None
        folders: list[dict[str, Any]] = []
        for item in self.items:
            if not item.get("is_dir"):
                continue
            root_id = str(item.get("root_id", ""))
            if root_ids and root_id not in root_ids:
                continue
            path = str(item.get("relative_path", "")).replace("\\", "/").strip("/")
            depth = len([part for part in path.split("/") if part])
            if depth_limit is not None and depth > depth_limit:
                continue
            folders.append(
                {
                    "item_id": str(item.get("id", "")),
                    "root_id": root_id,
                    "path": path,
                    "parent_path": _parent_path(path),
                    "depth": depth,
                    "child_file_count": int(item.get("child_file_count", 0)),
                    "child_folder_count": int(item.get("child_folder_count", 0)),
                    "is_project_root": bool(item.get("is_project_root")),
                    "roles": list(item.get("roles", [])),
                }
            )
        folders.sort(key=lambda value: (value["root_id"], value["path"].casefold()))
        start = max(0, offset)
        bounded = max(1, min(250, limit))
        return {
            "folders": folders[start : start + bounded],
            "total": len(folders),
            "offset": start,
            "limit": bounded,
            "has_more": start + bounded < len(folders),
            "max_depth": depth_limit,
        }

    def get_item(self, item_id: str) -> dict[str, Any]:
        item = next((item for item in self.items if str(item.get("id")) == item_id), None)
        if item is None:
            raise ValueError("Unknown item identifier")
        return _public_item(item, True)


def compile_inventory_glob(pattern: str) -> re.Pattern[str]:
    """Compile slash-separated glob syntax where ** crosses folder boundaries."""
    normalized = (pattern or "**").strip().replace("\\", "/").lstrip("/")
    output = ["^"]
    index = 0
    while index < len(normalized):
        character = normalized[index]
        if character == "*":
            if index + 1 < len(normalized) and normalized[index + 1] == "*":
                index += 2
                if index < len(normalized) and normalized[index] == "/":
                    output.append("(?:.*/)?")
                    index += 1
                else:
                    output.append(".*")
                continue
            output.append("[^/]*")
        elif character == "?":
            output.append("[^/]")
        else:
            output.append(re.escape(character))
        index += 1
    output.append("$")
    return re.compile("".join(output), re.IGNORECASE)


def _public_item(item: Mapping[str, Any], include_metadata: bool) -> dict[str, Any]:
    keys = (
        "id",
        "root_id",
        "relative_path",
        "parent_path",
        "name",
        "extension",
        "mime_type",
        "size",
        "created_ns",
        "modified_ns",
        "is_dir",
        "is_placeholder",
        "is_project_root",
        "child_file_count",
        "child_folder_count",
        "tag_ids",
    )
    result = {key: item.get(key) for key in keys}
    path = str(item.get("relative_path", ""))
    result["name"] = result["name"] or path.replace("\\", "/").rsplit("/", 1)[-1]
    result["extension"] = result["extension"] or _extension(path)
    result["parent_path"] = (
        result["parent_path"] if result["parent_path"] is not None else _parent_path(path)
    )
    if include_metadata:
        result["metadata"] = item.get("metadata", {})
    return result


def _extension(path: str) -> str:
    folded = path.casefold()
    for suffix in (".synctex.gz", ".tar.gz", ".tar.bz2", ".run.xml"):
        if folded.endswith(suffix):
            return suffix
    name = path.rsplit("/", 1)[-1]
    return f".{name.rsplit('.', 1)[-1]}" if "." in name else ""


def _parent_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    return normalized.rsplit("/", 1)[0] if "/" in normalized else ""
