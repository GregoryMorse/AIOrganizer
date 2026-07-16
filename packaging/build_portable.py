from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def patch_nuitka_macos_symlink_scan(dependency_scan_file: Path) -> bool:
    """Backport Nuitka's symlink-safe macOS dependency lookup.

    Nuitka 2.8.10 compares a resolved dylib path with its collected source path
    as strings. Homebrew's Intel runner paths can name the same OpenSSL library
    through two symlinks, which makes an otherwise successful standalone build
    fail during its final dependency fixup. Nuitka's upstream scanner now uses
    ``areSamePaths`` for this comparison.
    """

    old = "if resolved_filename == standalone_entry_point.source_path:"
    new = "if areSamePaths(resolved_filename, standalone_entry_point.source_path):"
    source = dependency_scan_file.read_text(encoding="utf-8")
    if new in source:
        return False
    if old not in source:
        raise RuntimeError("Unsupported Nuitka macOS dependency scanner; refusing an unsafe patch")
    dependency_scan_file.write_text(source.replace(old, new, 1), encoding="utf-8")
    return True


def nuitka_macos_dependency_scan_file() -> Path:
    import nuitka

    return Path(nuitka.__file__).resolve().parent / "freezer" / "DllDependenciesMacOS.py"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = root / "artifacts"
    output.mkdir(exist_ok=True)
    compilation_root = root / "build" / f"portable-{args.platform}"
    command = [
        sys.executable,
        "-m",
        "nuitka",
        "--standalone",
        "--assume-yes-for-downloads",
        "--enable-plugin=pyside6",
        "--include-qt-plugins=platforms,imageformats",
        # FastMCP requires cryptography only for optional OAuth/JWT transports.
        # AIOrganizer's bundled MCP boundary is local, unauthenticated stdio and
        # has no approval/commit tools. Excluding it also avoids a Nuitka 2.8.10
        # Intel-macOS dependency-scanner failure in cryptography's OpenSSL wheel.
        ("--nofollow-import-to=pytest,hypothesis,*.tests,*.testing,cryptography"),
        "--output-filename=AIOrganizer",
        f"--output-dir={compilation_root}",
    ]
    for package in (
        "ai_organizer",
        "anthropic",
        "elftools",
        "keyring",
        "lingua",
        "mcp",
        "mutagen",
        "msal",
        "openai",
        "PIL",
        "platformdirs",
        "pydantic",
        "pypdf",
        "rarfile",
        "requests",
    ):
        command.append(f"--include-package={package}")
    if sys.platform != "linux":
        command.extend(
            [
                "--include-package=codex_cli_bin",
                "--include-package-data=codex_cli_bin",
            ]
        )
    command.append("--include-data-dir=src/ai_organizer/resources=ai_organizer/resources")
    command.append("src/ai_organizer/bootstrap/main.py")
    environment = os.environ.copy()
    environment.setdefault("NUITKA_CACHE_DIR", str(root / ".nuitka-cache"))
    if sys.platform == "darwin":
        dependency_scan_file = nuitka_macos_dependency_scan_file()
        if patch_nuitka_macos_symlink_scan(dependency_scan_file):
            print(f"Applied symlink-safe Nuitka macOS scanner fix: {dependency_scan_file}")
    subprocess.run(
        command,
        cwd=root,
        env=environment,
        check=True,
    )
    build_dir = compilation_root / "main.dist"
    if not build_dir.exists():
        raise RuntimeError(f"Nuitka standalone output not found: {build_dir}")
    for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md", "README.md"):
        shutil.copy2(root / name, build_dir / name)
    tesseract_bundle = os.getenv("AIORGANIZER_TESSERACT_BUNDLE", "")
    if tesseract_bundle:
        bundle_path = Path(tesseract_bundle).resolve(strict=True)
        shutil.copytree(
            bundle_path,
            build_dir / "resources" / "tesseract",
            dirs_exist_ok=True,
        )
    signing = (
        "Platform signature verification was required by the release workflow."
        if os.getenv("AIORGANIZER_SIGNED_BUILD") == "1"
        else "This development artifact has no platform code signature; security warnings may appear."
    )
    (build_dir / "ALPHA-NOTICE.txt").write_text(
        "AIOrganizer v0.1.0 alpha. "
        + signing
        + " Test with copied data first. Cross-volume source "
        "quarantine is retained indefinitely until explicit Cleanup review.\n",
        encoding="utf-8",
    )
    suffix = "zip" if args.platform != "linux-x86_64" else "gztar"
    archive = shutil.make_archive(str(output / f"AIOrganizer-{args.platform}"), suffix, build_dir)
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
