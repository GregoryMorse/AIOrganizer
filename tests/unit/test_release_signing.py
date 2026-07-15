from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _module() -> ModuleType:
    path = Path(__file__).parents[2] / "packaging" / "sign_release.py"
    spec = importlib.util.spec_from_file_location("aiorganizer_sign_release", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_signing_commands_require_explicit_nonsecret_identity(tmp_path: Path) -> None:
    module = _module()
    artifact = tmp_path / "AIOrganizer.exe"
    with pytest.raises(RuntimeError, match="CERT_SHA1"):
        module.signing_command("windows", artifact, {})
    command = module.signing_command(
        "windows", artifact, {"AIORGANIZER_WINDOWS_CERT_SHA1": "ABC123"}
    )
    assert command[:3] == ["signtool", "sign", "/fd"]
    assert "/p" not in command
    assert "ABC123" in command
    assert module.verification_command("windows", artifact)[:3] == [
        "signtool",
        "verify",
        "/pa",
    ]


def test_release_signing_rejects_unknown_platform() -> None:
    module = _module()
    with pytest.raises(ValueError, match="platform"):
        module.signing_command("plan9", Path("artifact"), {})
