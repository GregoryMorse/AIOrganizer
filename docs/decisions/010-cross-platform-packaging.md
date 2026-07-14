# ADR-010: Cross-platform portable packaging

Status: accepted

Use uv-locked Python 3.12 dependencies and Nuitka standalone builds. CI produces a
Windows x64 ZIP, macOS universal2 ZIP, and Linux x86_64 TAR.GZ, plus SHA-256 and
SPDX SBOM artifacts. The first alpha is unsigned and displays platform security,
copied-data testing, and indefinite-quarantine warnings.
