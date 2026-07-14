# Threat model

## Assets

Original files, workspace metadata, extracted content, provider credentials,
Codex sessions, operation journals, and quarantine contents.

## Trust boundaries

- Documents, filenames, metadata, OCR, email, and archives are untrusted input.
- Provider and MCP output is untrusted proposal data.
- Category and prompt changes are user-controlled but cannot bypass hard policy.
- Filesystems can change, disappear, change case behavior, or reuse mount paths.

## Required mitigations

- Resolve paths relative to approved roots; reject links, reparse traversal, and
  containment escape.
- Address AI-visible items with opaque IDs and bounded evidence.
- Compile prompts with immutable safety/schema layers and label evidence untrusted.
- Apply the most restrictive root/category/action privacy policy.
- Require approved category assignments for routing.
- Reject stale source, prompt, category, action, or proposal revisions.
- Reject existing targets; never use overwrite semantics.
- Journal intent before mutation and each transition after mutation.
- Hash-verify cross-volume copies before finalization and source quarantine.
- Redact secret-like material from logs, prompts, and diagnostic exports.
- Never execute macros, scripts, archive members, links, QR codes, or embedded URLs.
- Block all commits while an incomplete journal needs recovery.

## Phase 1 abuse cases

| Threat | Required control |
|---|---|
| Category or prompt manipulation relaxes privacy | Immutable safety/schema layers and most-restrictive root/category/action policy are evaluated outside AI. |
| Source content instructs the model to mutate or reveal data | Evidence is bounded, redacted, delimited as untrusted, and model tools remain proposal-only. |
| Sensitive-data action reproduces a credential | Findings retain only secret type and opaque item ID; values are redacted before prompts, previews, and logs. |
| AI invents an attractive but ineligible destination | Deterministic routing creates the eligible candidate set; AI may only rank that set. |
| Cross-root or cross-volume operation escapes containment | Both source and destination roots are resolved and checked again at commit; changed mounts/file IDs stop the journal. |
| Quarantine discloses retained originals | Quarantine stays on the source volume under a unique journal path and is never cloud-submitted or cleaned automatically. |
| Generic workflow splits a project | Project markers stop descendant inventory; only the project root can be reviewed as an atomic bundle. |
| Prompt/category/action/evidence changes after review | Dependency revisions stale proposal sets and nonterminal frozen plans. |
| A removable device is replaced at the same path | Volume and stable file identity are snapshot preconditions; mismatches stop execution and require refresh. |

## Explicit non-goals for the alpha

The alpha does not guarantee protection against an administrator or malware already
running as the user. It does not permanently delete files, send mail, log into
websites, or inspect password-manager secret values.
