# ADR-006: OCR and packaging

Status: accepted

Use Tesseract 5.5.2 as a worker subprocess. Bundle English, orientation, and Latin
script data. Additional official tessdata_fast packs require an explicit download
and checksum verification. Portable release bundles are unsigned during alpha.
