# ADR-010: Cross-platform portable packaging

Status: accepted

Use uv-locked Python 3.12 dependencies and Nuitka standalone builds. CI produces a
Windows x64 ZIP, macOS universal2 ZIP, and Linux x86_64 TAR.GZ, plus SHA-256,
CycloneDX JSON SBOM, and GitHub provenance/SBOM attestations. Inno Setup, macOS app-bundle metadata,
and Linux desktop metadata are manual wrappers around the same portable payload. Platform signing
uses explicit credential-gated hooks and native post-sign verification; unsigned development artifacts
remain clearly labeled. Packaging never runs from `dev.cmd`, normal pushes, or application startup.
