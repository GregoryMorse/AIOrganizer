from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path


def signing_command(
    platform_name: str, artifact: Path, environment: Mapping[str, str]
) -> list[str]:
    if platform_name == "windows":
        thumbprint = environment.get("AIORGANIZER_WINDOWS_CERT_SHA1", "").strip()
        if not thumbprint:
            raise RuntimeError("AIORGANIZER_WINDOWS_CERT_SHA1 is required")
        return [
            "signtool",
            "sign",
            "/fd",
            "SHA256",
            "/sha1",
            thumbprint,
            "/tr",
            environment.get("AIORGANIZER_TIMESTAMP_URL", "https://timestamp.acs.microsoft.com"),
            "/td",
            "SHA256",
            str(artifact),
        ]
    if platform_name == "macos":
        identity = environment.get("AIORGANIZER_MACOS_SIGN_IDENTITY", "").strip()
        if not identity:
            raise RuntimeError("AIORGANIZER_MACOS_SIGN_IDENTITY is required")
        return [
            "codesign",
            "--force",
            "--deep",
            "--options",
            "runtime",
            "--timestamp",
            "--sign",
            identity,
            str(artifact),
        ]
    if platform_name == "linux":
        key = environment.get("AIORGANIZER_GPG_KEY_ID", "").strip()
        if not key:
            raise RuntimeError("AIORGANIZER_GPG_KEY_ID is required")
        return [
            "gpg",
            "--batch",
            "--yes",
            "--local-user",
            key,
            "--armor",
            "--detach-sign",
            str(artifact),
        ]
    raise ValueError("platform must be windows, macos, or linux")


def verification_command(platform_name: str, artifact: Path) -> list[str]:
    if platform_name == "windows":
        return ["signtool", "verify", "/pa", "/all", "/v", str(artifact)]
    if platform_name == "macos":
        return ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(artifact)]
    if platform_name == "linux":
        return ["gpg", "--verify", f"{artifact}.asc", str(artifact)]
    raise ValueError("platform must be windows, macos, or linux")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Explicit release-only signing hook; never called by dev.cmd"
    )
    parser.add_argument("platform", choices=["windows", "macos", "linux"])
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    artifact = args.artifact.resolve(strict=True)
    artifacts_root = (root / "artifacts").resolve(strict=False)
    if artifacts_root not in artifact.parents:
        raise ValueError("Only files beneath the repository artifacts directory may be signed")
    sign = signing_command(args.platform, artifact, os.environ)
    verify = verification_command(args.platform, artifact)
    if not args.execute:
        print(json.dumps({"sign": sign, "verify": verify}, indent=2))
        return 0
    if os.getenv("AIORGANIZER_RELEASE_SIGNING") != "1":
        raise RuntimeError("Set AIORGANIZER_RELEASE_SIGNING=1 for explicit release signing")
    subprocess.run(sign, check=True)
    subprocess.run(verify, check=True)
    marker = artifact.with_name(f"{artifact.name}.signature.json")
    marker.write_text(
        json.dumps(
            {
                "artifact": artifact.name,
                "platform": args.platform,
                "sha256": artifact_digest(artifact),
                "verification": "passed",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0


def artifact_digest(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(path.read_bytes())
        return digest.hexdigest()
    for child in sorted(value for value in path.rglob("*") if value.is_file()):
        digest.update(child.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(child.read_bytes())
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
