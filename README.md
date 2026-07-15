<p align="center">
  <img src="docs/assets/aiorganizer-logo.svg" alt="AIOrganizer — human-approved, AI-assisted organization" width="820">
</p>

# AIOrganizer

AIOrganizer is a local-first desktop application for reviewing and safely applying
AI-assisted file organization proposals. AI can inspect evidence and revise
proposals; only the desktop application can validate and execute a user-approved
plan.

## Current alpha scope

- Saved `.aioworkspace` workspaces with multiple filesystem sources
- User-defined hierarchical categories and enforced folder roles
- Inventory audits that propose naming, cleanup, and project-handling guidance from observed patterns
- Durable semantic records with freshness state, provenance, and preserved update-site hints
- OS-specific installed-software inventory and downloaded-installer correlation in Updates
- Versioned, layered AI guidance for each application view
- Inventory and content evidence for common file formats
- Rename, folder-plan, and cross-source move proposals
- Reviewed recurring-document series with explainable period coverage and gap exceptions
- Focused security and organization actions
- OpenAI, Anthropic, embedded Codex, and MCP integration points
- Journaled operations, verification, recovery records, quarantine, and undo plans
- First-run safety onboarding, workspace backup, review export, and private-data-free diagnostics
- Experimental metadata-only Office.js Outlook selection companion
- Accessibility preferences, Qt localization catalogs, contributor/provider contracts, and release attestations

The project is Windows-first and is tested on Windows, macOS, and Linux. It is
an unsigned alpha: use copied data before using it on original files.

## Fast Python development

The normal development loop runs the Python source directly. It does not invoke
Nuitka, compile C, build a package, or create a release binary.

Set up the local environment once:

```powershell
uv sync --extra desktop --extra analysis --extra mcp
```

To enable the optional Outlook connector in the same Python environment, run once:

```powershell
uv sync --extra desktop --extra analysis --extra mcp --extra email
```

Then launch the application after each edit with:

```powershell
.\dev.cmd
```

For local AI testing, edit the ignored `.env` file created in the repository root:

```dotenv
DEEPSEEK_API_KEY=your-key-here
DEEPSEEK_MODEL=deepseek-v4-flash
```

OpenRouter is also available through `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL`, and an editable
organization-qualified `OPENROUTER_MODEL` such as `openai/gpt-5.2`.

Environment values are fallbacks; credentials saved in Settings use the operating-system
credential store and take precedence. Restart the development app after changing `.env`.

The explicit Python equivalent is:

```powershell
.\.venv\Scripts\python.exe .\dev.py
```

Source changes under `src/` take effect on the next launch; there is no rebuild
step. Pass application arguments through the launcher when needed, for example
`.\dev.cmd --smoke-test`.

Large inventories run in a background worker. The progress window first shows live discovery
counts and bytes, then switches to determinate item/byte progress for metadata extraction once the
final totals are known. Cancellation saves no partial snapshot.

File-backed review pages share one tabbed inspector. It renders PDFs, images, and Markdown in the
app; offers embedded audio/video playback; syntax-highlights bounded code and text; exposes all
indexed metadata; and presents ZIP/RAR/TAR member headers in a sortable, filterable, paginated table.
PDFs and images support explicit zoom and drag panning. Binary inspection uses a seekable 4 KiB hex
window, and text reads are capped at 1 MB, so inspecting a very large file never loads it wholesale.

Tests and production packaging remain available, but are not part of this loop.
GitHub workflows are manual-only during active development, so a normal push
does not start CI or release packaging. Python 3.12 is the release runtime. See the
[product and architecture plan](docs/PRODUCT_AND_ARCHITECTURE_PLAN.md),
[threat model](docs/threat-model.md), and [ADRs](docs/decisions/README.md).

## Local MCP development

AIOrganizer contains a local stdio MCP server. There is no listener, inbound connection, public
URL, or reverse tunnel: Codex or VS Code launches the Python process and exchanges MCP messages
over its standard input/output streams.

The development dependency command above installs the MCP and metadata-analysis extras. The normal
desktop launch automatically registers a stable `aiorganizer` server with Codex when the Codex CLI
is installed. The checked-in `.vscode/mcp.json` also makes the server available when this repository
is opened in VS Code. Hosts retain their normal workspace/server trust prompt.

Whenever a workspace is created or opened, the app publishes its path to a small per-user state
file. Automatically launched MCP processes read that pointer, so switching workspaces requires no
host reconfiguration. Set `AIORGANIZER_AUTO_REGISTER_MCP=0` before launch to disable automatic
Codex registration. A host that was already running may require an MCP refresh or new session after
the first registration.

## Phase 5 recurring documents

Recurrences discovers repeated, period-bearing document patterns but does not track them
automatically. Each candidate shows issuer, document type, masked account suffix, inferred cadence,
confidence, covered periods, and grouping rationale. Review can correct the series fields and
individually exclude falsely grouped documents before saving a durable reviewed series.

The period matrix classifies every expected interval as verified, probably present, missing, not
due, inside its grace period, intentionally skipped, or ignored. Each row states its deadline and
why it received that status. Missing periods can be dismissed individually with a required reason,
and series cadence, start/end dates, and grace days remain editable.

Series observations retain root-relative identity and source fingerprints so rescans can rebind
documents safely. Missing files stop satisfying a period; changed files are downgraded to ambiguous
until reviewed again. The attachment boundary accepts metadata-only descriptors and can rank an
attachment against an individually missing period. It deliberately exposes no download method and
has no Outlook dependency.

## Phase 6 Microsoft Graph email

Email is an optional Microsoft Graph connector with one active delegated account at a time. Put a
public/native Microsoft Entra application ID in `AIORGANIZER_GRAPH_CLIENT_ID` in the ignored `.env`
file, install the `email` extra, restart `dev.cmd`, and use **Email → Sign in read-only**. The app
opens Microsoft's device sign-in flow; it never asks for a Microsoft password or client secret.

Initial consent is limited to `User.Read` and `Mail.Read`. Read refreshes use a separate delta cursor
for every selected mail folder and cache bounded, redacted message previews plus attachment metadata.
Attachment bytes are never requested. Serialized MSAL tokens live in the operating-system credential
store, not the workspace. The workspace stores only account identity, scopes, Graph object metadata,
opaque delta links, security-evidence classifications, and reviewed proposals.

Folder creation, message moves, category changes, and inspected inbox rules are staged independently.
Selecting proposals shows the exact additional delegated scopes before an incremental consent flow;
applying requires typing `APPLY EMAIL CHANGES`. Immediately before a move or category change, the app
re-reads the message and compares its folder, `changeKey`, and ETag. A mismatch marks the proposal
stale. Sending, replying, forwarding, deleting, and permanent deletion have no connector operation.

Accounts & Security Evidence groups registration, welcome, verification, security-alert,
password-reset, MFA, billing, and cancellation signals using sender identity and already-redacted
subjects. It retains no message bodies, reset links, tokens, or codes. Attachment metadata can be
matched against missing reviewed recurrence periods without downloading the attachment.

The automatically registered MCP server exposes bounded read-only views of the local mail cache:
summary, folders, sanitized messages, attachment descriptors, and Accounts & Security Evidence.
Those tools never contact Graph, return credentials, download attachment bytes, or apply mail changes.

The automated Phase 6 suite uses a fake Graph transport for paging, delta, permission, conflict, and
no-send/no-delete checks. A real test-tenant soak remains an explicit release gate and is not run by
the normal Python development loop.

## Phase 7 companion, export, and release groundwork

The first launch presents a short safety/development tour; it remains available under **Help**.
**File** now provides three distinct durable outputs:

- **Backup workspace** creates a complete `.aioworkspace` SQLite backup.
- **Export review bundle** creates a checksummed ZIP of policies, guidance, proposals, journals, and
  activity. It warns first because it can contain local paths, filenames, and operation history.
- **Export diagnostic bundle** contains runtime/schema/count information and activity kinds only. It
  excludes paths, filenames, document/email metadata, evidence text, semantic facts, proposal payloads,
  operation payloads, and credentials.

Settings includes an **Accessibility & Language** tab for text scaling, a high-contrast palette, and
system/English locale selection. English remains the source/fallback language; additional translations
are human-reviewed Qt `.qm` catalogs so safety terminology is never silently machine-translated.

The experimental [`outlook-addin`](outlook-addin/README.md) is a static Office.js read-item task pane.
It reads only the selected item's subject, sender, date, opaque identifier, and attachment descriptors,
then exports a versioned JSON handoff. **File → Import Outlook selection metadata** validates strict
size/schema bounds, rejects bodies and unknown authority fields, redacts links/codes/tokens, and stores
the result as untrusted semantic metadata. The add-in has no mailbox-write, attachment-download, local
HTTP, AI approval, or desktop apply authority. Its manifest passes Microsoft's current validation service.

Portable packaging, native package definitions, CycloneDX SBOMs, checksums, and GitHub provenance/SBOM
attestations remain manual release operations. [`packaging`](packaging/README.md) includes credential-gated
Windows/macOS/Linux signing hooks with native verification; no signing identity or secret is stored in the
repository. Platform certificate-backed release signing and Apple notarization cannot be performed until
real publisher identities are configured. None of this runs from `dev.cmd` or a normal push.

Contributor rules are in [`CONTRIBUTING.md`](CONTRIBUTING.md). The versioned provider manifest and safety
boundary are documented in [`docs/provider-plugin-contract.md`](docs/provider-plugin-contract.md). This is
a contract for review and conformance—not a general executable-plugin loader.

## Phase 4 cleanup and cross-volume safety

Cleanup is a separate explicit review. It proposes currently empty non-project folders,
AIOrganizer partials older than one hour and not owned by an active journal, and conservative
build artifacts backed by project manifests and tool-specific regeneration rules. A directory
merely named `build`, `target`, or `cache` is not enough evidence. Each row shows total bytes, item
count, derivation, regeneration evidence, exclusions, and its restorable quarantine destination.

Cleanup has no permanent-delete operation. Build artifacts are unselected by default, applying a
batch requires typing `QUARANTINE`, and the latest completed batch can be restored unless an
original path has since become occupied. Cleanup quarantine directories are excluded from normal
inventory scans.

Cross-volume moves use an explicit copy, verify, and finalize protocol. Preflight checks free
space, partial targets are removed after failed copies when the source remains authoritative,
directory copies flush regular files, and the final cryptographic digest covers regular files and
symbolic-link structure. The original moves into a separate indefinite quarantine only after the
destination verifies.

## Phase 3 folder and move planning

Folder Plan uses a row-aligned union hierarchy: existing folders, projected descendants, and
not-yet-created ghost folders appear in one table. Edit a projected folder path to propose an
in-place rename, select the operations to apply, then freeze and preflight. Reparenting, nested
rename plans, missing parents, platform-invalid names, case-aware projected collisions, and
protected project boundaries are rejected before filesystem mutation.

Move review shows the full projected target and collision reason. It accepts regular files and
detected project roots as atomic units, while blocking arbitrary directory trees and generic moves
into or out of detected code projects. Every selected batch is revalidated against the current
inventory immediately before commit.

Interrupted operation journals remain visible after an application restart. Overview offers an
explicit recovery action that stages any observed partial result, restores original locations,
checks content hashes, and only then marks the journal rolled back. Verified commits remain
undoable after reopening the workspace.

## Phase 2 AI evidence and proposal editing

Rename review can create an expiring MCP selection scope containing only opaque item IDs. MCP
proposal writes require that desktop-created scope, the proposal's expected revision, and an
idempotency key. Replays return the original result; stale revisions, changed requests, unknown
items, path-shaped rename values, and attempts to expand a scope fail safely. Evidence summaries
and document pages are bounded, paginated, secret-redacted, and explicitly marked untrusted.

Local extraction assesses embedded PDF text page by page and sends only low-coverage pages to a
Tesseract 5 subprocess when one is installed or configured with `AIORGANIZER_TESSERACT`. Missing OCR
is recorded as an explicit confidence route rather than silently treated as good evidence. The app
does not bundle a large OCR executable into the Python development loop. Evidence extraction and
provider requests run in worker threads so the Qt event loop remains responsive.

Each source has a persistent provider privacy ceiling: local only, metadata only, cloud text, or
text and images. Before a direct provider request, the confirmation shows provider/model, item and
content counts, source policies, redactions, byte size, and estimated tokens. Provider responses are
validated against a strict local schema before they can alter a proposal, and AI changes always
enter `needs_review` rather than becoming accepted.

The host model chooses and calls tools exposed by that child process. MCP hosts may use their own
browsing/search capability. The desktop's direct DeepSeek/OpenRouter Updates flow instead supplies bounded public
HTTPS search and visible-page-text tools itself, along with preserved semantic hints and a strict JSON
Schema. Verified results include a literal version prefix and constrained version-format enum; the app
compiles the bounded regex locally so later checks can fetch and
parse the saved official page without an AI call. A failed parser is visible and routes the selected item
back through AI re-audit; no returned code is executed.

Organization policy is multi-axis: semantic categories answer what material is, facet tags capture
content/lifecycle/state/origin/technology/audience properties, and source roles control workflow authority.
New workspaces include a general-purpose profile and existing workspaces can merge it explicitly. Reusable
source presets configure common inbox, software, repository, dependency, personal, research, education,
teaching, and legacy-migration cases without hardcoded paths. Folder Plan uses a preferred depth plus a hard
workspace/source/category ceiling; adaptive AI may choose a shallower structure but never a deeper one.

The Updates page keeps installed software separate from Download-category files. On Windows it
can correlate those files with current and past Microsoft Defender detections. The result is stored
both as semantic history and directly in each file's metadata. “No matching detection history” is
intentionally not presented as “clean,” and AIOrganizer does not start a Defender scan or alter
Defender settings.

Metadata records do not expire. Every scan first compares file size and nanosecond modified time;
unchanged files reuse their durable metadata without opening the file. Settings has optional CRC32
and SHA-256 validation, disabled by default because either choice necessarily rereads entire files.
All file actions independently repeat the same validation immediately before execution.

Metadata extraction is size-bounded: PE/Windows version resources, MSI properties, ELF headers and
dynamic facts, and Mach-O load commands are read through header/resource APIs rather than whole-file
loads. Large text files are sampled for an explicitly marked line-count estimate, Office XML reads
are capped, and image metadata uses Pillow. Audio/video metadata uses bounded `ffprobe` probing when
available, with Mutagen as the audio/tag fallback. ZIP/JAR/WHL, MSIX/AppX, RAR, and TAR member headers
are indexed without extracting file contents. Member paths, compressed/uncompressed sizes,
timestamps, CRC, encryption, directory, and format-specific fields are stored in a separate
paginated database table and exposed through the Inventory preview and
`inventory_list_archive_members` MCP tool. RAR header listing uses `rarfile`; no external unrar
executable is needed unless future functionality actually decompresses content.

## Safety boundary

MCP and model providers expose proposal-only capabilities. There is no AI-facing
approve, commit, delete, arbitrary-path, or command-execution operation. File
changes require a frozen plan, fresh preflight validation, and a user action in
the desktop application.

## License

Apache-2.0. Qt/PySide and bundled third-party components retain their own
licenses; release bundles include the required notices.
