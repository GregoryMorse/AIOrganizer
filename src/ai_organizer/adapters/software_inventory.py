from __future__ import annotations

import json
import platform
import subprocess

from ai_organizer.domain.semantic import SoftwarePackage, software_package_id


class SoftwareInventory:
    def scan(self) -> list[SoftwarePackage]:
        system = platform.system()
        if system == "Windows":
            return self._windows()
        if system == "Darwin":
            return self._macos()
        return self._linux()

    def _windows(self) -> list[SoftwarePackage]:
        import winreg

        locations = [
            (
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                "system",
            ),
            (
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
                "system",
            ),
            (
                winreg.HKEY_CURRENT_USER,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                "user",
            ),
        ]
        packages: dict[str, SoftwarePackage] = {}
        for hive, path, scope in locations:
            try:
                key = winreg.OpenKey(hive, path)
            except OSError:
                continue
            with key:
                for index in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        child_name = winreg.EnumKey(key, index)
                        with winreg.OpenKey(key, child_name) as child:
                            name = _registry_value(child, "DisplayName")
                            if not name or _registry_value(child, "SystemComponent") == "1":
                                continue
                            publisher = _registry_value(child, "Publisher")
                            package = SoftwarePackage(
                                software_package_id(name, publisher),
                                name,
                                publisher,
                                _registry_value(child, "DisplayVersion"),
                                "windows_registry",
                                scope,
                                _registry_value(child, "InstallDate"),
                                _registry_value(child, "InstallLocation"),
                            )
                            current = packages.get(package.id)
                            if current is None or (not current.version and package.version):
                                packages[package.id] = package
                    except OSError:
                        continue
        return sorted(packages.values(), key=lambda package: package.name.casefold())

    def _macos(self) -> list[SoftwarePackage]:
        try:
            result = subprocess.run(
                ["system_profiler", "SPApplicationsDataType", "-json"],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            payload = json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            return []
        packages = []
        for item in payload.get("SPApplicationsDataType", []):
            name = str(item.get("_name", "")).strip()
            if name:
                packages.append(
                    SoftwarePackage(
                        software_package_id(name, ""),
                        name,
                        "",
                        str(item.get("version", "")),
                        "system_profiler",
                        install_location=str(item.get("path", "")),
                    )
                )
        return packages

    def _linux(self) -> list[SoftwarePackage]:
        commands = [
            (["dpkg-query", "-W", "-f=${Package}\t${Version}\t${Maintainer}\n"], "dpkg"),
            (["rpm", "-qa", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\t%{VENDOR}\n"], "rpm"),
        ]
        for command, source in commands:
            try:
                result = subprocess.run(
                    command, check=True, capture_output=True, text=True, timeout=120
                )
            except (OSError, subprocess.SubprocessError):
                continue
            packages = []
            for line in result.stdout.splitlines():
                name, version, publisher = [*line.split("\t", 2), "", ""][:3]
                if name:
                    packages.append(
                        SoftwarePackage(
                            software_package_id(name, publisher),
                            name,
                            publisher,
                            version,
                            source,
                        )
                    )
            return packages
        return []


def _registry_value(key: object, name: str) -> str:
    import winreg

    try:
        return str(winreg.QueryValueEx(key, name)[0]).strip()
    except OSError:
        return ""
