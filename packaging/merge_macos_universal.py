from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


def merge(arm64: Path, x86_64: Path, output: Path) -> None:
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    relative_files = {
        path.relative_to(root)
        for root in (arm64, x86_64)
        for path in root.rglob("*")
        if path.is_file()
    }
    for relative in sorted(relative_files):
        arm_file = arm64 / relative
        intel_file = x86_64 / relative
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if arm_file.is_file() and intel_file.is_file() and _is_macho(arm_file):
            if not _is_macho(intel_file):
                raise RuntimeError(f"Architecture mismatch for {relative}")
            subprocess.run(
                ["lipo", "-create", str(arm_file), str(intel_file), "-output", str(target)],
                check=True,
            )
            target.chmod(0o755)
        elif arm_file.is_file():
            shutil.copy2(arm_file, target)
        else:
            shutil.copy2(intel_file, target)
    executable = output / "AIOrganizer"
    if not executable.is_file():
        raise RuntimeError("Merged application executable is missing")
    executable.chmod(0o755)


def _is_macho(path: Path) -> bool:
    result = subprocess.run(["file", "-b", str(path)], capture_output=True, text=True, check=True)
    return "Mach-O" in result.stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("arm64", type=Path)
    parser.add_argument("x86_64", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    merge(args.arm64, args.x86_64, args.output)
    if args.archive:
        args.archive.parent.mkdir(parents=True, exist_ok=True)
        shutil.make_archive(str(args.archive.with_suffix("")), "zip", root_dir=args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
