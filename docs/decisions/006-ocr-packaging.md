# ADR-006: OCR and packaging

Status: accepted

Use Tesseract 5.5.2 as a worker subprocess. Bundle English, orientation, and Latin
script data pinned to an official tessdata_fast commit with checked SHA-256 values.
Additional official packs require an explicit download and checksum verification.
The engine must report exactly 5.5.2; a compatible system engine may be discovered
when a redistributable platform binary is unavailable. Portable release bundles
are unsigned during alpha.
