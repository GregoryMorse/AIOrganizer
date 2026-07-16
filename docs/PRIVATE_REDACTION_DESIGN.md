# Local privacy and redaction boundary

Personal documents may be inventoried, rendered, OCRed, searched, and classified locally. Raw
document text, OCR text, page images, and attachment bytes do not cross the cloud boundary merely
because a cloud AI provider is configured.

Before any cloud request, AIOrganizer builds a derived request payload through these layers:

1. Built-in deterministic detection masks credential assignments, private-key blocks, contextual
   OTP/MFA/reset codes, IBANs, and long numeric identifiers including space- or hyphen-formatted
   account and card numbers.
2. A user-maintained private-value list masks exact names, addresses, identifiers, or phrases the
   generic detectors cannot know. The list is stored in the operating-system credential store and is
   never written to a workspace, log, diagnostic export, or repository.
3. A future optional local PII model may add contextual person, address, organization, medical,
   financial, and government-identifier spans. A cloud model must never be the first detector because
   that would require sending the unredacted content.
4. The user sees a request preview and may inspect the redacted derivative. Ambiguous detections can
   block rather than redact. Cloud submission requires both an adequate per-source privacy ceiling and
   an explicit action.

For raster images and scanned PDFs, text-only masking is insufficient. Local OCR must retain word or
line bounding boxes (for example Tesseract TSV/hOCR), map detected spans back to page coordinates, and
render an irreversible flattened redacted preview. Raw images remain local. Searchable-PDF generation
and privacy-redacted derivatives are separate outputs and never overwrite the original.

Provider retention controls are defense-in-depth, not permission to transmit raw private content.
OpenAI API inputs are not used for training by default, but ordinary abuse-monitoring logs can retain
content for up to 30 days; Zero Data Retention requires an eligible approved organization. Anthropic's
commercial API normally retains inputs and outputs for up to 30 days, with separate approved zero-data-
retention arrangements. OpenRouter supports per-request Zero Data Retention routing; AIOrganizer
requests it and must fail rather than fall back to an endpoint without a declared ZDR policy. Provider
terms can change, so release validation must recheck these controls and show the effective policy in the
request preview.

No detector can promise perfect PII discovery. The safe default for a personal source is therefore
**Local only**. Redaction lowers risk for deliberately enabled cloud workflows; it does not silently
upgrade a local-only source into cloud-authorized content.

The MCP server treats its client as cloud processing by default. Inventory names require at least a
`metadata_only` source policy, extracted summaries and page text require `cloud_text`, and rendered
pages require `text_and_images`; unconfigured storage browsing is blocked. A deliberately local MCP
host may set `AIORGANIZER_MCP_PROCESSING_LOCALITY=local`, which preserves local-only workflows without
weakening the default boundary for remote models.
