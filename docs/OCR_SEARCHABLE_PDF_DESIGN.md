# Searchable PDF OCR safety boundary

Adding a selectable OCR text layer is practical, but it cannot preserve the original PDF byte for
byte: adding text necessarily creates a different file, and even an incremental PDF update changes
the byte stream and invalidates existing digital signatures. AIOrganizer must therefore never call
this an “exact rebuild” and must never overwrite the source PDF in place.

The safe workflow belongs in the explicit **Document Repair** tool, not in Rename or routine inventory:

1. Preserve the original file and its size/modified-time fingerprint; optionally record SHA-256 when
   the user requests the action.
2. Generate OCR words and lines with normalized page-relative bounding boxes and confidence, together
   with page geometry and rotation. Store this sidecar evidence first. This positioned sidecar is now
   produced by Document Repair and is useful to search and audit without changing PDF.
3. If AI correction is enabled, send only selected policy-allowed, locally placeholder-redacted lines.
   Validate exact line IDs and placeholders plus conservative edit similarity before restoring private
   values locally. Corrected text stays anchored to its original line box; raw word boxes remain available
   for later fine-grained review. AI may not invent, delete, merge, or reorder positioned lines.
4. After explicit review, create a new sibling or chosen-output PDF containing an invisible text layer
   aligned to the original page geometry. The default sibling name is
   `<original-stem>-rebuilt-with-text.pdf` (with a deterministic collision suffix when needed). Never
   replace the original automatically.
5. Validate the derived file before offering it: page count and page boxes must match; every original
   page must render; a raster comparison must show no material visible change; text extraction must
   round-trip; and the output must reopen in both Qt PDF and a second parser.
6. Block encrypted PDFs without an explicit unlocked copy. Detect and prominently label digitally
   signed inputs. Their signed original is immutable; the `-rebuilt-with-text.pdf` neighbor is an
   explicitly unsigned derivative and must never be presented as retaining or transferring the
   signature. Quarantine failed partial outputs.

OCRmyPDF/Tesseract can provide much of the rendering and positioning machinery, but it is an optional
external dependency and normally rewrites the PDF. A later implementation can use it with optimization
disabled and the validations above, while accurately describing the result as a derived searchable copy.
Inventory and Rename should continue to consume cached sidecar OCR; they do not need PDF write-back.
