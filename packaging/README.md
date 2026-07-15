# Manual release packaging

Nothing in this directory is invoked by `dev.cmd`, a normal Git push, or application startup.
The GitHub workflows are `workflow_dispatch` only. Portable builds remain the common input:

- Windows x64 portable ZIP; `windows/AIOrganizer.iss` can create an Inno Setup installer.
- macOS arm64/x86_64 standalone trees merged to universal2, with `macos/Info.plist` as the app-bundle metadata baseline.
- Linux x86_64 TAR.GZ, with `linux/AIOrganizer.desktop` for desktop integration/AppDir packaging.

Release builds generate CycloneDX JSON SBOMs, SHA-256 checksums, and GitHub artifact provenance/SBOM
attestations. Platform signing is an additional credential-gated operation:

```powershell
$env:AIORGANIZER_RELEASE_SIGNING="1"
$env:AIORGANIZER_WINDOWS_CERT_SHA1="certificate-thumbprint"
.\.venv\Scripts\python.exe packaging\sign_release.py windows artifacts\path\AIOrganizer.exe --execute
```

The signing helper accepts only files beneath `artifacts/`, passes no certificate password on the
command line, verifies the result, and records a checksum marker. macOS uses a configured Developer ID
identity and hardened-runtime timestamp; distribution also requires `notarytool` submission/stapling.
Linux produces and verifies a detached armored GPG signature. Never publish an artifact merely because
a marker exists—release automation must repeat native signature verification.

Self-signed Windows certificates are testing-only. Public Windows distribution should use Microsoft
Store signing, Azure Artifact Signing, an eligible open-source signing service, or a publicly trusted
publisher certificate. Public macOS builds require Developer ID signing and Apple notarization.
