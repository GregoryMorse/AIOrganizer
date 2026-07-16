# Document Repair workflow

Document Repair is a staged PDF-only tool. Rename, Move, Audit, and other tools may consume cached
repair evidence, but they do not start OCR or rebuild a PDF themselves.

The workflow is intentionally split:

1. **Find candidates locally.** Read PDF structure and embedded text with OCR disabled. Report pages
   needing OCR, encryption/signature state, raster-image count and share, approximate full-page DPI,
   and whether image recompression might help.
2. **Build selected previews locally.** OCR only checked rows. Store raw page text plus normalized
   word/line bounding boxes, page geometry, and rotation as sidecar evidence.
   When images dominate file size, create a cache-only 200-DPI/quality-85 derivative and reopen it
   with a second parser. The source remains untouched.
3. **Review six PDF views.** Current Preview, Extracted/OCR Text, Metadata, and Hex are joined by
   Proposed OCR Text and Proposed Compression. The compression proposal is the actual temporary PDF,
   not an estimated byte count.
4. **Correct OCR cautiously.** The explicit **AI-clean selected OCR text** step sends only line IDs and
   policy-authorized, locally sanitized line text; page images, filenames, and coordinate bounds stay
   local. Built-in secret/identifier detections and user private terms become unique opaque placeholders.
   The complete result is rejected unless every line ID and placeholder round-trips and every edit stays
   within a conservative similarity/length bound. Original values are restored locally. Raw OCR and the
   corrected positioned-line proposal remain side by side.
5. **Apply as derivatives.** A later commit stage writes a reviewed sibling such as
   `-rebuilt-with-text.pdf` or `-optimized.pdf`. It never overwrites a signed original. Before offering
   the output, validate page count/boxes, renderability, extracted-text round-trip, and visual delta.

“Without losing visual fidelity” is a review goal, not an automatic guarantee. DPI and compression
heuristics only identify candidates. The temporary derivative must be smaller by a material threshold
and must pass rendered comparison before the UI recommends keeping it.
