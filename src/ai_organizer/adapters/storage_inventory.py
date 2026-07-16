from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any


class StorageInventory:
    """Bounded, read-only discovery of mounted storage and direct directory entries."""

    def list_volumes(self, sources: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        source_rows = list(sources or [])
        volumes = []
        for raw in _platform_volumes():
            mount = Path(str(raw["mount_point"]))
            row = dict(raw)
            row["volume_id"] = _volume_id(mount)
            try:
                usage = shutil.disk_usage(mount)
                row.update(
                    {
                        "total_bytes": usage.total,
                        "used_bytes": usage.used,
                        "free_bytes": usage.free,
                        "used_percent": round(usage.used * 100 / usage.total, 1)
                        if usage.total
                        else 0.0,
                        "reachable": True,
                    }
                )
            except OSError as error:
                row.update(
                    {
                        "total_bytes": None,
                        "used_bytes": None,
                        "free_bytes": None,
                        "used_percent": None,
                        "reachable": False,
                        "error": type(error).__name__,
                    }
                )
            roots = [
                str(source.get("id", ""))
                for source in source_rows
                if source.get("path") and _is_within(Path(str(source.get("path", ""))), mount)
            ]
            row["configured_root_ids"] = sorted(value for value in roots if value)
            row["configured_source_count"] = len(row["configured_root_ids"])
            row["has_configured_source"] = bool(row["configured_root_ids"])
            volumes.append(row)
        return volumes

    def list_directory(
        self,
        volume_id: str,
        relative_path: str = "",
        *,
        include_hidden: bool = False,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        volume = next(
            (
                value
                for value in _platform_volumes()
                if _volume_id(Path(str(value["mount_point"]))) == volume_id
            ),
            None,
        )
        if volume is None:
            raise ValueError("Unknown current volume identifier")
        mount = Path(str(volume["mount_point"])).resolve(strict=False)
        relative = Path(relative_path.replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Directory path must remain relative to the selected volume")
        target = (mount / relative).resolve(strict=False)
        if not _is_within(target, mount):
            raise ValueError("Directory path escapes the selected volume")
        if not target.is_dir():
            raise ValueError("Selected volume directory is unavailable")
        entries: list[dict[str, Any]] = []
        try:
            children = list(os.scandir(target))
        except OSError as error:
            return {
                "volume_id": volume_id,
                "relative_path": relative.as_posix() if str(relative) != "." else "",
                "entries": [],
                "total": 0,
                "offset": 0,
                "limit": 0,
                "has_more": False,
                "error": type(error).__name__,
            }
        for child in children:
            hidden = child.name.startswith(".") or _windows_hidden(child)
            if hidden and not include_hidden:
                continue
            try:
                info = child.stat(follow_symlinks=False)
                kind = "symlink" if child.is_symlink() else "folder" if child.is_dir() else "file"
                size = info.st_size if kind == "file" else None
                modified_ns = info.st_mtime_ns
            except OSError:
                kind, size, modified_ns = "unavailable", None, None
            child_relative = (relative / child.name).as_posix()
            entries.append(
                {
                    "name": child.name,
                    "relative_path": child_relative.removeprefix("./"),
                    "kind": kind,
                    "size": size,
                    "modified_ns": modified_ns,
                    "hidden": hidden,
                }
            )
        entries.sort(key=lambda value: (value["kind"] != "folder", value["name"].casefold()))
        start = max(0, offset)
        bounded = max(1, min(250, limit))
        return {
            "volume_id": volume_id,
            "mount_point": str(mount),
            "relative_path": relative.as_posix() if str(relative) != "." else "",
            "entries": entries[start : start + bounded],
            "total": len(entries),
            "offset": start,
            "limit": bounded,
            "has_more": start + bounded < len(entries),
            "content_read": False,
        }


def _platform_volumes() -> list[dict[str, Any]]:
    system = platform.system()
    if system == "Windows":
        return _windows_volumes()
    if system == "Linux":
        return _linux_volumes()
    if system == "Darwin":
        return _macos_volumes()
    return [{"mount_point": str(Path.home().anchor or "/"), "kind": "unknown"}]


def _windows_volumes() -> list[dict[str, Any]]:
    import ctypes

    kernel32 = ctypes.windll.kernel32
    mask = int(kernel32.GetLogicalDrives())
    kinds = {
        0: "unknown",
        1: "invalid",
        2: "removable",
        3: "fixed",
        4: "network",
        5: "optical",
        6: "ramdisk",
    }
    result = []
    for index in range(26):
        if not mask & (1 << index):
            continue
        mount = f"{chr(65 + index)}:\\"
        drive_type = int(kernel32.GetDriveTypeW(mount))
        label = ctypes.create_unicode_buffer(261)
        filesystem = ctypes.create_unicode_buffer(261)
        serial = ctypes.c_ulong()
        maximum_component = ctypes.c_ulong()
        flags = ctypes.c_ulong()
        ok = bool(
            kernel32.GetVolumeInformationW(
                mount,
                label,
                len(label),
                ctypes.byref(serial),
                ctypes.byref(maximum_component),
                ctypes.byref(flags),
                filesystem,
                len(filesystem),
            )
        )
        result.append(
            {
                "mount_point": mount,
                "label": label.value if ok else "",
                "filesystem": filesystem.value if ok else "",
                "serial": f"{serial.value:08X}" if ok else "",
                "kind": kinds.get(drive_type, "unknown"),
                "is_network": drive_type == 4,
                "is_removable": drive_type in {2, 5},
            }
        )
    return result


def _linux_volumes() -> list[dict[str, Any]]:
    mounts = Path("/proc/self/mountinfo")
    if not mounts.is_file():
        return [{"mount_point": "/", "kind": "fixed"}]
    ignored = {
        "autofs",
        "bpf",
        "cgroup",
        "cgroup2",
        "configfs",
        "debugfs",
        "devpts",
        "devtmpfs",
        "fusectl",
        "hugetlbfs",
        "mqueue",
        "proc",
        "pstore",
        "securityfs",
        "sysfs",
        "tracefs",
    }
    result: dict[str, dict[str, Any]] = {}
    for line in mounts.read_text(encoding="utf-8", errors="replace").splitlines():
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        fields, trailing = left.split(), right.split()
        if len(fields) < 6 or len(trailing) < 2 or trailing[0] in ignored:
            continue
        mount = _unescape_mount(fields[4])
        source = trailing[1]
        result[mount] = {
            "mount_point": mount,
            "label": "",
            "filesystem": trailing[0],
            "device": source,
            "kind": "network" if "://" in source or source.startswith("//") else "fixed",
            "is_network": "://" in source or source.startswith("//"),
            "is_removable": mount.startswith(("/media/", "/run/media/")),
        }
    return list(result.values())


def _macos_volumes() -> list[dict[str, Any]]:
    try:
        completed = subprocess.run(
            ["mount"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return [{"mount_point": "/", "kind": "fixed"}]
    result = []
    for line in completed.stdout.splitlines():
        source, separator, rest = line.partition(" on ")
        mount, marker, details = rest.partition(" (")
        if not separator or not marker:
            continue
        filesystem = details.split(",", 1)[0].rstrip(")")
        result.append(
            {
                "mount_point": mount,
                "label": Path(mount).name if mount != "/" else "",
                "filesystem": filesystem,
                "device": source,
                "kind": "removable" if mount.startswith("/Volumes/") else "fixed",
                "is_network": filesystem.casefold() in {"smbfs", "nfs", "webdav"},
                "is_removable": mount.startswith("/Volumes/"),
            }
        )
    return result or [{"mount_point": "/", "kind": "fixed"}]


def _volume_id(mount: Path) -> str:
    normalized = os.path.normcase(os.path.abspath(str(mount)))
    return "volume_" + hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _is_within(path: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath(
            (os.path.abspath(path), os.path.abspath(parent))
        ) == os.path.abspath(parent)
    except ValueError:
        return False


def _windows_hidden(entry: os.DirEntry[str]) -> bool:
    try:
        attributes = int(getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0))
    except OSError:
        return False
    return bool(attributes & 0x2)


def _unescape_mount(value: str) -> str:
    return value.replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")
