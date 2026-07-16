from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _module() -> ModuleType:
    path = Path(__file__).parents[2] / "packaging" / "build_portable.py"
    spec = importlib.util.spec_from_file_location("aiorganizer_build_portable", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_nuitka_macos_symlink_fix_is_guarded_and_idempotent(tmp_path: Path) -> None:
    module = _module()
    scanner = tmp_path / "DllDependenciesMacOS.py"
    scanner.write_text(
        "if resolved_filename == standalone_entry_point.source_path:\n"
        "    return standalone_entry_point.dest_path\n",
        encoding="utf-8",
    )

    assert module.patch_nuitka_macos_symlink_scan(scanner) is True
    assert module.patch_nuitka_macos_symlink_scan(scanner) is False
    patched = scanner.read_text(encoding="utf-8")
    assert "os.path.realpath(resolved_filename)" in patched
    assert "os.path.realpath(standalone_entry_point.source_path)" in patched


def test_nuitka_macos_symlink_fix_strengthens_upstream_comparison(tmp_path: Path) -> None:
    module = _module()
    scanner = tmp_path / "DllDependenciesMacOS.py"
    scanner.write_text(
        "if areSamePaths(resolved_filename, standalone_entry_point.source_path):\n"
        "    return standalone_entry_point.dest_path\n",
        encoding="utf-8",
    )

    assert module.patch_nuitka_macos_symlink_scan(scanner) is True
    patched = scanner.read_text(encoding="utf-8")
    assert "os.path.realpath(resolved_filename)" in patched
    assert "if areSamePaths(resolved_filename," not in patched


def test_nuitka_macos_symlink_fix_rejects_unknown_scanner(tmp_path: Path) -> None:
    module = _module()
    scanner = tmp_path / "DllDependenciesMacOS.py"
    scanner.write_text("# unexpected future implementation\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Unsupported Nuitka"):
        module.patch_nuitka_macos_symlink_scan(scanner)
