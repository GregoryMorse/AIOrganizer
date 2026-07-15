# AI Organizer: Product and Architecture Plan

Status: Phase 0–5 implementation baseline
Date: 2026-07-15
Scope: local files, folder planning, focused actions, moves, cleanup, and reviewed recurring-document tracking; Outlook follows in a staged release

> **Phase 1 decision update:** Where older sequencing text below defers moves,
> cross-volume handling, categories, layered prompts, Anthropic, or embedded Codex,
> this accepted baseline supersedes it. Phase 1 includes reviewed folder creation and
> in-place folder rename, regular-file and approved atomic-project moves, and the
> copy/verify/finalize/indefinite-quarantine protocol. It also includes OpenAI,
> Anthropic, and an embedded Codex app-server/SDK path. Cleanup and Outlook remain
> Outlook remains roadmap work; Cleanup is implemented by the Phase 4 update below.

> **Phase 2 implementation update:** Evidence extraction now routes individual low-text PDF pages
> to optional local Tesseract OCR, records confidence/review state, and runs extraction/provider
> calls outside the UI thread. Direct providers are schema-validated and governed by persistent
> per-source metadata/text/image privacy levels with a visible request preview. The stdio MCP server
> exposes bounded evidence, resource, prompt, and proposal-editing surfaces. Proposal writes require
> an expiring desktop-created selection scope, expected revision, and persistent idempotency key;
> they are separately audited and cannot approve or commit work.

> **Phase 3 implementation update:** Folder review now uses a row-aligned union hierarchy with
> projected descendants and ghost creates. Folder renames are in-place only. File move and folder
> plans receive case-aware projected-state collision and containment preflight, and generic actions
> cannot cross detected project boundaries. Journals persist enough observed state for explicit
> restart recovery, including interrupted swaps and interrupted recovery itself; verified plans
> remain undoable after reopening a workspace.

> **Phase 4 implementation update:** Cleanup is an explicit, typed-confirmation workflow that
> sends empty folders, inactive AIOrganizer partials, and manifest-backed generated artifacts to a
> hidden, restorable per-source quarantine. It exposes item/byte totals, derivation, regeneration
> evidence, and exclusions, never permanent deletion. Cross-volume copy failures remove known
> partial targets while preserving authoritative sources; preflight covers destination space and
> verification covers regular content and symbolic-link structure.

> **Phase 5 implementation update:** Period-bearing document patterns become candidates only and
> remain untracked until the user reviews series fields and individual membership. Reviewed series
> persist root-relative observation identities and source fingerprints. The gap matrix distinguishes
> verified, ambiguous, missing, not-due, grace-window, skipped, and ignored periods with a deadline
> and explanation on every row. Exceptions are individual and reasoned. A metadata-only attachment
> matcher is ready for future connectors but has no content retrieval or download authority.

## 1. Executive decision

Build a local-first desktop application using Python 3.12+ and PySide6/Qt Widgets. Keep the domain engine independent of Qt and expose a separate local MCP server that allows AI clients to inspect evidence and edit proposals. The desktop application, never the AI, owns validation, approval, filesystem or mailbox mutation, journaling, verification, and undo.

The first releasable vertical slice inventories multiple categorized roots, extracts bounded evidence, reviews PDFs beside virtualized proposal tables, builds folder and move plans, and commits only frozen user selections through journaled and verified transactions.

Do not begin with Outlook, arbitrary directory-tree moves, or cleanup. Phase 1 permits regular-file moves and explicitly approved detected projects as atomic bundles; it never moves arbitrary directory trees.

## 2. Product principles and invariants

These rules are product requirements, not implementation preferences.

1. **Nothing is applied implicitly.** Scanning and analysis can run automatically after the user starts them. Any external mutation requires an explicit review and an explicit final confirmation.
2. **AI creates or edits proposals only.** No AI-facing tool can approve a proposal, apply a plan, delete an item, or bypass a validation error.
3. **Review is item-level.** Bulk acceptance is available, but it is a user action over visible, selected items. Low-confidence and blocked items cannot be silently included.
4. **Every commit is based on an immutable snapshot.** If a file, folder, message, rule, or relevant parent changed after analysis, the affected operation is stopped and must be refreshed.
5. **Every mutation is journaled before execution.** The journal records intent, preconditions, progress, result, verification, and the inverse operation where an inverse is possible.
6. **Deletion exists only in Cleanup.** Initial cleanup uses quarantine or the operating-system recycle bin. Permanent deletion is a separate, strongly warned action and is not part of the early releases.
7. **Paths are containment-checked.** No operation may escape the source roots through `..`, symlinks, junctions, aliases, case tricks, or changed mount points.
8. **Content is local by default.** A source has an explicit AI privacy policy: metadata only, local processing, cloud text, or cloud images. Cloud submission is visibly indicated before analysis.
9. **Documents and email are untrusted input.** Text inside a file or message can inform classification, but is never treated as an instruction to the agent or application.
10. **The application remains useful without AI.** Inventory, deterministic naming rules, PDF review, proposal editing, validation, commits, logs, and undo work without a model provider.
11. **Original names remain recoverable.** The database and exported operation manifest preserve original paths and stable identifiers.
12. **No password harvesting.** The account-registration feature records evidence that an account or security event exists. It never extracts, displays, stores, or sends passwords, reset codes, session tokens, or MFA secrets.

## 3. Terminology and state machine

Use these terms consistently in the UI, code, documentation, and MCP schemas:

- **Workspace**: a user-defined organizational context containing one or more source roots, policies, and naming profiles.
- **Inventory snapshot**: a point-in-time record of items and relevant metadata.
- **Evidence**: metadata, extracted text, OCR output, thumbnails, detected entities, and reasons used for a suggestion.
- **Proposal set**: an editable collection of suggested operations of one type, such as rename or move.
- **Proposal item**: a single suggested change with confidence, rationale, validation state, and review decision.
- **Plan**: a frozen, validated selection of accepted proposal items.
- **Commit**: the explicit execution of a plan.
- **Journal**: the durable record of commit preparation, execution, verification, failure, recovery, and undo.

Proposal items use the states `proposed`, `accepted`, `rejected`, `edited`, `needs_review`, `blocked`, and `stale`. A plan uses `draft`, `validating`, `ready`, `executing`, `verified`, `partially_failed`, `rolled_back`, and `completed`.

The important boundary is:

```text
inventory -> evidence -> editable proposal -> user decisions -> frozen plan
          -> preflight validation -> user commit -> journaled operations
          -> verification -> undo availability
```

Returning from a frozen plan to editing creates a new plan revision. It never mutates the plan that was already shown on a confirmation screen.

## 4. Framework decision

### 4.1 Recommended stack

- Python 3.12 or newer
- PySide6 with Qt Widgets for the desktop shell
- `QAbstractItemModel`-based models for large tables and hierarchy diffs
- Qt PDF / `QPdfView` for the first integrated PDF viewer
- SQLite in WAL mode for inventory, proposals, durable jobs, and journals
- standard-library SQLite with explicit, backup-first numbered migrations
- Pydantic at process, provider, MCP, and serialized-plan boundaries
- Official MCP Python SDK with a conservative version pin
- `uv` for development environments and dependency locking
- `pyside6-deploy`/Nuitka for initial packaged builds, evaluated against PyInstaller during packaging work
- `pytest`, Hypothesis, Ruff, and mypy for verification

### 4.2 Why PySide6/Qt is the best first choice

This application is dominated by desktop review controls: very large checkable tables, tree diffs, synchronized selection, keyboard-driven triage, background jobs, filesystem integration, and an embedded document viewer. Qt is particularly strong in those areas. Python also gives direct access to PDF parsing, OCR, image processing, MIME detection, clustering, local model clients, and Microsoft Graph libraries without introducing a second backend language.

Qt Widgets are preferable to QML for the first release. The desired interface is a dense productivity application rather than a touch-first animated UI. Widgets and Qt's model/view APIs make the review grids and hierarchy diff less risky. A later visual redesign can still introduce focused QML components without changing the domain engine.

### 4.3 Tradeoffs

- Packaged Python applications are larger and need deliberate startup and dependency work.
- Qt and Qt PDF licensing obligations must be documented and satisfied for distributions.
- Python type boundaries must be enforced; allowing UI objects, dictionaries, and provider payloads to leak into the domain would make the codebase fragile.
- CPU-heavy OCR must run out of process, not on the UI thread or merely in an asyncio task.

### 4.4 Alternatives not selected

- **Tauri/web UI** would produce small shells and a flexible interface, but the bridge between a Rust shell, a web UI, PDF rendering, Python AI/OCR workers, and native filesystem behavior creates more moving parts for the first release.
- **Electron** is viable and makes a future Outlook add-in feel familiar, but adds a large runtime while still needing a Python or native analysis service.
- **Avalonia/.NET** is a good cross-platform desktop option, especially for a C# team, but the AI/OCR ecosystem and embedded PDF choices make it less direct for this particular project.
- **Native Windows/WPF** would give excellent Windows integration but conflicts with the open-source, portable goal.

The decision should be revisited only if a two-week proof of concept shows a blocking PDF, accessibility, packaging, or performance issue.

## 5. High-level architecture

The application is a modular monolith with two local entry points: the Qt desktop process and an MCP process. Both use the same application services and database schema. Only the desktop process exposes commit commands.

```text
                         AI host
            (Codex, another MCP client, or API adapter)
                              |
                    MCP proposal tools only
                              |
+-----------------------------v----------------------------------+
|                    AI Organizer local system                    |
|                                                                |
|  Qt UI --> application use cases --> domain rules              |
|             |          |             |                         |
|             |          |             +-- naming/recurrence     |
|             |          +-- proposal/plan/commit engine          |
|             +-- inventory/extraction durable jobs               |
|                                                                |
|  adapters: filesystem | PDF/OCR | SQLite | AI | Graph | keyring|
+-----------------------------|----------------------------------+
                              |
              user-approved, journaled external changes
                              |
                 filesystems and Microsoft Graph
```

Dependency direction is inward: UI and adapters depend on application interfaces and domain objects; domain code does not import Qt, SQLAlchemy, Graph, OCR, MCP, or a model SDK.

### 5.1 Process model

- **Desktop process**: UI, lightweight queries, plan validation, and commits.
- **Worker processes**: PDF extraction, rendering, OCR, hashing, thumbnail generation, and other CPU- or crash-prone analyzers. Jobs are durable in SQLite and resumable.
- **MCP process**: normally launched over stdio by the AI host. It reads inventory/evidence and changes proposal records using optimistic concurrency. It cannot invoke commit services.
- **Optional provider subprocess**: a local Codex app-server integration may be added later and should remain isolated from the main process.

The database runs in WAL mode. Long-running worker results are written in small transactions. The UI never waits synchronously for OCR or a provider response.

## 6. Repository layout

Start as one Python distribution with strict internal packages. Split distributions only when deployment or versioning provides a concrete reason.

```text
AIOrganizer/
  README.md
  LICENSE
  pyproject.toml
  uv.lock
  docs/
    PRODUCT_AND_ARCHITECTURE_PLAN.md
    decisions/
    threat-model.md
    naming-policy.md
    user-workflows.md
  src/ai_organizer/
    domain/
      inventory/
      evidence/
      naming/
      proposals/
      planning/
      recurrence/
      safety/
    application/
      commands/
      queries/
      ports/
      jobs/
    adapters/
      filesystem/
      persistence/
      pdf/
      ocr/
      ai/
      outlook/
      secrets/
    desktop/
      models/
      views/
      controllers/
      dialogs/
      resources/
    mcp_server/
      tools/
      resources/
      prompts/
      server.py
    cli/
    bootstrap/
  migrations/
  tests/
    unit/
    integration/
    property/
    fault_injection/
    ui/
    contract/
    golden/
    fixtures/
  integrations/
    outlook-addin/          # added only in the Outlook add-in phase
  packaging/
    windows/
    macos/
    linux/
```

Avoid generic dumping grounds such as `utils.py`, `helpers.py`, `common.py`, and `misc.py`. A module should be named for the capability or policy it owns. Cross-cutting primitives remain small and explicit.

## 7. Core data model

The initial schema should include the following concepts.

### 7.1 Inventory and evidence

- `Workspace`: name, policy references, active naming profile
- `SourceRoot`: normalized identity, display path, volume identity, inclusion/exclusion rules, cloud-processing policy
- `InventorySnapshot`: start/end timestamps, scanner version, status
- `Item`: opaque ID, source root, relative path, kind, MIME, size, timestamps, filesystem identity, link/reparse status
- `ItemFingerprint`: algorithm, partial/full status, digest
- `DocumentEvidence`: extractor version, page count, text coverage, language, detected dates/entities/document type, confidence
- `EvidenceBlob`: local cache reference, sensitivity classification, expiry and purge state
- `AnalysisFinding`: typed finding with evidence references and provenance

Paths are stored as source-root ID plus normalized relative path, not as unconstrained absolute strings. The original display spelling is retained separately.

### 7.2 Proposals and operations

- `ProposalSet`: kind, snapshot, base revision, creation source, policy versions
- `ProposalItem`: item ID, proposed values, confidence, rationale, evidence references, review state, validation issues
- `SelectionScope`: an expiring set of item IDs created by the UI for an AI revision request
- `Plan`: immutable revision, accepted items, validation report, summary
- `Operation`: typed source and target, preconditions, dependencies, inverse, idempotency key
- `CommitJournal`: state and timestamps
- `OperationJournalEntry`: prepared/executing/executed/verified/undone/failure data

### 7.3 Later domains

- `RecurringSeries`, `ExpectedOccurrence`, `ObservedOccurrence`, `GapDecision`
- `MailAccount`, `MailFolderSnapshot`, `MessageEvidence`, `MailProposalSet`
- `AccountRelationship`: service/domain, owning mailbox, first/last evidence, categories, review state; no credential values
- `RuleProposal`: condition, exception, action, test sample, enabled-on-create decision

## 8. File analysis pipeline

Analysis is progressive so a large workspace becomes useful quickly and expensive work is targeted.

### Stage A: inventory

1. Enumerate chosen roots without following symlinks or junctions by default.
2. Apply user exclusions and protected-project rules.
3. Record metadata and filesystem identity.
4. Detect type using content signatures where practical, not extension alone.
5. Mark inaccessible, unstable, special, sparse, encrypted, cloud-placeholder, and link items explicitly.

### Stage B: cheap extraction

- PDF embedded text and metadata
- image EXIF and dimensions
- Office/container metadata where supported
- archive listing without extraction
- code/project boundary markers
- deterministic filename tokenization and typo candidates

### Stage C: confidence assessment

Estimate text coverage, encoding quality, page consistency, title/date/entity confidence, and ambiguity. A document with adequate embedded text should not be OCRed merely because OCR is available.

### Stage D: selective OCR

Render only the pages needing OCR. Start with title/first page and suspicious textless pages, then expand if classification remains uncertain. Tesseract is the baseline offline engine. Additional OCR engines are adapters, not domain dependencies.

### Stage E: optional vision model

Vision is a final escalation for documents whose layout or image-only content defeats local extraction. The user sees which pages and what content class will be sent. Per-source privacy policy can forbid this stage entirely.

### Stage F: structured findings

All analyzers return typed claims with confidence and evidence provenance. Providers do not return executable file operations directly. The proposal builder converts claims plus deterministic rules into candidate changes, then the validator independently checks them.

Extracted text and images are cached in the application data directory, not beside source documents. The user can inspect cache usage and purge it by workspace or globally.

## 9. Naming system

Do not hard-code one concatenated filename convention. Implement versioned naming profiles composed of tokens, separators, casing rules, and per-document-type templates.

Recommended human-readable default:

```text
YYYY-MM-DD - Entity - Document Type - Descriptor - Period.ext
```

Examples:

```text
2026-06-30 - Example Bank - Statement - Current Account - 2026-06.pdf
2026-07-05 - Utility Company - Invoice - Electricity - 2026-06.pdf
2025 - Tax Authority - Tax Return - Final.pdf
```

Not every token belongs in every name. Context that is stable and already unambiguous in the directory hierarchy should not be repeated mechanically in all filenames. The profile engine needs:

- required, optional, and forbidden tokens per document type
- trusted date sources and date precedence
- an explicit `unknown` policy rather than inventing dates
- entity canonicalization and aliases
- abbreviation and stop-word rules
- separator, whitespace, casing, and Unicode normalization
- extension casing and MIME/extension mismatch handling
- disambiguation strategy for collisions
- platform-reserved names and invalid characters
- length budgets for components and complete paths
- stable recurrence keys distinct from display names
- custom profiles import/export as versioned YAML or JSON

Renames show the parsed old tokens, proposed tokens, evidence for each inferred token, confidence, and validation issues. Typo repair uses a workspace dictionary and entity aliases; it must not silently “correct” unfamiliar proper nouns.

Source code and complete software projects use separate rules. The general renamer does not rename tracked source files, module names, project manifests, or references. A future code-refactor feature would need language-aware updates and is outside the organizer MVP.

## 10. User experience

### 10.1 Main shell

The main window contains:

- workspace/source selector and scan status
- left navigation: Overview, Inventory, Rename, Folder Plan, Move, Cleanup, Recurrences, Email, Activity
- central task-specific review surface
- collapsible evidence/preview inspector
- durable job/activity drawer
- a clear privacy/provider indicator

The application always distinguishes “analyzed,” “proposed,” “accepted,” and “applied.” Counts use those exact states.

### 10.2 Rename review

Use a virtualized table with these primary columns:

```text
[check] Preview | Current name | Proposed name | Confidence | Issues | Reason
```

Selecting a PDF opens it in the right-hand viewer at the most relevant page. Evidence highlights show the source of date, entity, type, and period. Keyboard commands support accept, reject, edit, next, previous, and filter. Filters include confidence, extractor/OCR method, issue type, file type, source, and review state.

Multi-selection provides “Ask AI to revise selection.” This creates an expiring selection scope and either:

- sends a structured request to an integrated provider, or
- displays/copies a short instruction for an external MCP-connected AI host.

The returned revisions remain proposals and are visually diffed against the previous revision.

### 10.3 Folder hierarchy proposal

A simple pair of unrelated tree widgets will become confusing when nodes are inserted. Use a **union hierarchy diff model**: one logical row set represents both current and projected nodes.

```text
[check] Current folder              Projected folder            Action     Status
        Documents/Taxes             Documents/Tax                rename     ready
        -                           Documents/Tax/2026            create     ready
        Downloads/Statements        Downloads/Statements          unchanged  -
```

The same model can be rendered as a unified tree or as synchronized current/projected panes. Newly proposed folders appear as ghost nodes; renamed nodes remain row-aligned. The folder stage may create folders and rename folders in place. It does not move files, reparent existing folders, or delete anything.

The projected tree is computed before execution, including all descendant paths, collision checks, platform limits, and case behavior.

### 10.4 File move review

Moves are a separate proposal set and do not change filenames. The table shows current folder, proposed folder, filename, reason, confidence, destination conflicts, and the projected folder tree. A user can revise destinations in bulk.

The first release supporting moves should allow same-volume moves only. Cross-volume moves require a later copy-verify-finalize protocol and distinct UI wording because they are not atomic renames.

### 10.5 Cleanup review

Cleanup categories are explicit:

- empty folders produced by accepted moves
- known build artifacts and caches
- abandoned partial files
- duplicates (later; never inferred solely from names)
- user-defined patterns

Each proposal shows total size, item count, derivation, regeneration evidence, exclusions, and destination: quarantine or recycle bin. Build-artifact rules use project markers, tool manifests, ignore files, and known patterns. A directory merely named `build`, `target`, `dist`, or `cache` is insufficient proof on its own.

Permanent deletion is not part of the initial cleanup implementation.

## 11. Folder organization strategy

The organizer should propose stable, shallow structures rather than constructing deep AI-generated taxonomies that are hard to maintain.

The hierarchy engine uses:

- user-defined top-level domains such as Personal, Household, Finance, Employment, Education, Projects, and Archive
- entity and document-type clusters
- temporal granularity appropriate to frequency (year, year/month, or none)
- recurrence patterns
- existing structures with high consistency, which should be preserved
- maximum depth and minimum-items-per-folder policies
- explicit “Inbox/To File” landing areas where useful

The proposal explains why each folder exists and estimates what will populate it in the subsequent move step. Empty target folders that have no accepted future moves are warned and excluded by default.

The sequence is deliberately:

1. create/rename the target folder skeleton;
2. propose and commit file moves without renaming;
3. re-inventory and verify;
4. propose cleanup of now-empty legacy folders.

This makes every transformation understandable and individually reversible.

## 12. Code and loose-file organization

Code needs special handling because moving one file can break imports, build files, source maps, links, and version-control state.

Detect project roots using markers such as `.git`, solution/workspace files, `pyproject.toml`, `package.json`, lockfiles, build manifests, and language-specific project descriptors. Treat a detected project as an atomic protected boundary in general file workflows.

Rules:

- do not rename or move files inside a detected project through generic rename/move tools;
- do not clean ignored files merely because they are ignored;
- project cleanup proposals must name the generating tool and regeneration basis;
- a whole untracked project directory may be proposed as one atomic move after validation;
- loose scripts can be grouped as candidates, but dependency/import analysis and a user-created project boundary are required before moving them;
- nested repositories, worktrees, submodules, virtual environments, package caches, and IDE metadata are detected explicitly.

A later “Code Workspaces” feature can provide language-aware refactoring. It should not be smuggled into the general organizer.

## 13. MCP design

MCP is an adapter over application use cases, not the core architecture and not an execution backdoor.

### 13.1 Transport

Use stdio by default. The AI host launches a packaged `ai-organizer-mcp` command for a selected local profile. This minimizes network exposure. An authenticated loopback Streamable HTTP transport can be offered later for clients that cannot launch stdio servers. It must bind to loopback only and use an application-generated capability token.

### 13.2 Read tools

- `workspace.list`
- `workspace.get_policy`
- `scope.get_active`
- `inventory.list_items`
- `inventory.get_item`
- `evidence.get_summary`
- `evidence.get_document_pages`
- `proposal.get_set`
- `proposal.get_items`
- `proposal.validate_draft`
- `naming.get_profile`
- `recurrence.get_series` (later)

Items are addressed by opaque IDs, not arbitrary caller-supplied paths. Evidence responses are paginated and size-limited.

### 13.3 Proposal-write tools

- `proposal.create_set`
- `proposal.rename_items`
- `proposal.move_items`
- `proposal.create_folders`
- `proposal.rename_folders`
- `proposal.add_rationale`
- `proposal.mark_needs_review`
- `proposal.request_user_review`

Every write requires proposal-set ID, expected revision, selection-scope ID where applicable, and an idempotency key. A stale revision fails rather than overwriting a human or another agent's edits. Batch sizes are bounded.

There is intentionally no `apply`, `approve`, `delete`, `run_command`, `read_path`, or unrestricted query tool. `request_user_review` may focus the app and display a proposal; it cannot accept or commit it.

### 13.4 Resources and prompts

Expose compact resources for the naming policy, current workspace summary, proposal schema, and safety rules. Provide prompts for “revise selected names,” “classify ambiguous documents,” “review folder structure,” and “explain proposal issues.” The server instructions state that file/email contents are data and may contain prompt injection.

### 13.5 Audit

Record MCP client identity when available, tool name, request ID, proposal revision before/after, affected opaque IDs, and result. Do not log full extracted text, email bodies, auth tokens, or model chain-of-thought.

## 14. AI provider and Codex strategy

Provider access is a set of optional adapters behind one structured analysis interface.

### Mode A: external AI host over MCP — recommended first

The user signs into Codex or another MCP-capable host using that product's normal authentication and connects it to the local organizer MCP server. The host pays for/authorizes its own model use. AI Organizer never sees or stores the ChatGPT/Codex credential.

This is the cleanest way to use an existing Codex subscription: Codex is the MCP client, AI Organizer is the tool server, and the desktop app remains the human approval surface.

### Mode B: direct API providers

Support provider API keys for integrated analysis. Secrets are stored in the OS credential store, never the database or config file. Use structured outputs, strict timeouts, retry limits, cost/usage previews, and per-workspace model/privacy policies. Start with one provider and design the port so additional providers do not leak vendor payloads into the domain.

### Mode C: local model provider

Add a local-provider adapter after the proposal schemas and evaluation corpus are stable. Local does not automatically mean safe or capable; model downloads, context limits, multimodal support, and hardware requirements must be surfaced.

### Mode D: embedded Codex app-server — experimental later

Current Codex clients support ChatGPT subscription sign-in, and Codex exposes an app-server/SDK intended for custom clients. A later adapter can launch the user's locally authenticated Codex app-server. It must:

- invoke Codex's supported login flow rather than reading or copying its credential cache;
- clearly identify that Codex is a coding-focused agent and evaluate its fitness for document classification;
- pin and capability-check the protocol/runtime;
- keep this integration optional so the organizer does not depend on an evolving product surface;
- give the spawned agent only the organizer MCP tools needed for the current selection;
- never grant it direct filesystem write access for organization tasks.

Do not represent a ChatGPT subscription as a general OpenAI API key. They are distinct access and billing paths.

## 15. Filesystem transaction and recovery design

### 15.1 Preflight

Before showing the final confirmation and again immediately before execution:

- resolve and verify every source root and volume identity;
- reject symlink/junction traversal and out-of-root targets;
- compare filesystem identity, size, modification time, and optional digest to the snapshot;
- check permissions, target-parent existence, free space where relevant, reserved names, path lengths, and case rules;
- detect duplicate targets and conflicts with unplanned items;
- compute operation dependencies and inverse operations;
- show a complete dry-run summary.

### 15.2 Rename scheduling

Renames can contain swaps, cycles, case-only changes, and targets occupied by another planned source. Build a dependency graph and use unique temporary names inside the same directory for cycles and platform-required case transitions. Temporary names are journaled before use and are never guessed on recovery.

### 15.3 Commit protocol

1. Persist frozen plan and all prepared operations.
2. Re-run preconditions.
3. Mark journal `executing` and fsync/flush the journal boundary.
4. Execute one idempotent operation at a time.
5. Persist each result immediately.
6. Verify all final paths/identities.
7. Mark complete and create the undo plan.

On restart, the application detects incomplete journals and offers a deterministic resume or rollback based on observed state. It does not start new mutation work until recovery is resolved.

### 15.4 Moves

Same-volume moves use filesystem rename semantics where supported. Cross-volume moves are deferred until the implementation can:

1. copy to a uniquely named partial target;
2. flush and verify length and cryptographic hash;
3. atomically finalize the destination where possible;
4. move the source to quarantine or otherwise preserve an undo window;
5. verify and journal both sides.

The UI must call this “copy and finalize,” not imply atomicity.

### 15.5 Undo

Undo is another validated plan. It can fail safely if original locations are now occupied or items changed. Conflicts are presented for manual resolution; the tool never overwrites newer content to force an undo.

## 16. Recurring documents and gap detection

This feature is built on reviewed document evidence, not on raw filename similarity alone.

Series candidates use issuer/entity, document type, masked account identifier, cadence, statement/coverage period, and stable layout/text fingerprints. The user confirms or edits a series before it becomes tracked.

For each series, derive expected periods and classify them as:

- present and verified
- probably present but ambiguous
- missing
- not due yet
- within grace period
- intentionally skipped/not applicable
- ignored by user

The page shows a timeline or period matrix, not merely a warning count. It explains why an item belongs to a series and why a gap is expected. Users can change cadence, start/end dates, grace periods, and exceptions.

Email attachments can later be matched to missing periods and proposed for download/archive. Downloading and filing an attachment remains an explicit proposal. The application should not log into banks or scrape websites as part of this feature.

## 17. Outlook and email plan

### 17.1 First integration: Microsoft Graph in the desktop app

Use delegated Microsoft identity authentication and Microsoft Graph. The account selector shows one active account at a time, as requested. Begin with read-only permissions and request write scopes only when the user enables apply features.

Initial capabilities:

- inventory selected mail folders and headers;
- preview sanitized text and attachments;
- propose folders, categories, and message moves;
- cluster recurring senders and message types;
- propose inbox rules and test each against a historical sample;
- identify likely registration, welcome, verification, security-alert, password-reset, MFA, billing, and cancellation messages;
- match downloadable document attachments to recurring series.

Mailbox IDs can change after moves, so store immutable IDs when supported and still validate remote state before apply. Use delta queries for refresh rather than repeatedly downloading an entire mailbox.

### 17.2 Apply behavior

- Message moves are item-level accepted operations with remote ETag/change validation where available.
- Folder creation is separate from message moves.
- Proposed rules show conditions, exceptions, action, priority, and a sample of messages they would have matched.
- Create rules disabled by default in the first implementation; enabling is a second explicit action after inspection.
- Deleting mail is a Cleanup operation and initially means move to Deleted Items, never permanent deletion.
- Sending, replying, forwarding, changing account security settings, and extracting codes are out of scope.

### 17.3 Account-registration view

The feature should be named **Accounts & Security Evidence**, not Passwords. It shows services for which the mailbox contains likely account evidence, the mailbox involved, first/last evidence, and security-event categories. Sensitive bodies are not retained unless the user explicitly opts in, and reset links/codes/tokens are redacted.

A future password-manager connector could let the user compare reviewed service identifiers with entries in a password manager, but it must use that manager's supported API and never read secret values unless a narrowly defined workflow truly requires it.

### 17.4 Outlook add-in: later thin companion

After Graph workflows are stable, add a web-based Office/Outlook add-in written in TypeScript. It supplies a task pane for the currently selected message and delegates analysis/proposal work to the organizer service. Do not use COM/VSTO: those approaches do not support new Outlook and conflict with portability.

The add-in is a companion surface, not the primary engine. It requires its own manifest, web hosting/deployment plan, authentication, and capability matrix. It should not be allowed to commit broad mailbox operations from a hidden event handler.

## 18. Privacy and security model

### 18.1 Data classification

Classify cached evidence as metadata, extracted content, visual content, email content, security/account evidence, or secret-like. Secret-like spans are redacted and excluded from model requests and logs.

### 18.2 Provider request preview

For cloud providers, show a concise request summary: provider, model, number of items/pages, content types, redactions, and estimated size. Allow “remember for this source and analysis type,” not a single global permission hidden in settings.

### 18.3 Prompt injection

- source text is wrapped/tagged as untrusted evidence;
- tool instructions outrank document content;
- output must match schemas;
- model-proposed paths are normalized and independently validated;
- no document can expand tool permissions or selection scope;
- URLs, QR codes, scripts, macros, and embedded attachments are never executed during analysis.

### 18.4 Local service security

- stdio is the only application-exposed MCP transport;
- any HTTP service binds to loopback and uses a high-entropy capability token;
- no permissive cross-origin policy;
- provider and Microsoft tokens live in the OS keyring;
- logs exclude content and credentials by default;
- diagnostic bundles are previewed and redacted before export.

### 18.5 Open-source supply chain

- lock dependencies and generate an SBOM for releases;
- audit licenses, especially PDF/OCR and packaging components;
- sign release artifacts when release infrastructure exists;
- no runtime plugin may gain commit authority merely by being installed;
- third-party analyzers run with bounded input/output and time limits.

## 19. Testing and verification strategy

Safety behavior needs more than happy-path unit tests.

### Unit and property tests

- naming tokenization, rendering, and reversibility
- cross-platform invalid characters and reserved names
- case-insensitive collisions and Unicode normalization collisions
- rename dependency graphs, swaps, and cycles
- root containment and relative-path normalization
- proposal state machine and optimistic concurrency
- recurrence cadence and grace-period calculations

Use Hypothesis to generate path sets and rename graphs. Assert that a valid plan has unique targets, never escapes its roots, and either reaches the projected state or stops with a recoverable journal.

### Integration tests

- real temporary filesystems on Windows, Linux, and macOS CI where possible
- symlinks, junctions/reparse points, permission changes, locked files, long paths, case-only renames, and cloud placeholders
- SQLite crash/restart and migration behavior
- PDF text extraction, malformed PDFs, image-only PDFs, and encrypted PDFs
- MCP contract tests with stale revisions and scope violations
- Graph adapter tests against recorded/synthetic responses; live tests only in a dedicated test tenant

### Fault injection

Inject failure before and after every journal boundary and filesystem operation. Kill the worker/process at each point, restart, and verify that the app can classify observed state and offer a safe recovery.

### Golden/evaluation corpus

Use synthetic or redistributable fixtures representing invoices, bank statements, payslips, tax forms, scans, multilingual documents, ambiguous dates, and malicious prompt-injection text. Never commit personal user documents or email to the repository.

Track precision and review effort, not just model “accuracy”:

- correct exact proposal rate
- unsafe/invalid proposal rate (target: zero reaching `ready`)
- low-confidence routing rate
- average user edits per accepted item
- false missing-gap rate
- recovery success under injected faults

## 20. Delivery phases and acceptance gates

These are sequential safety gates, not a promise that every later feature belongs in version 1.0.

### Phase 0: design proof and threat model (approximately 1–2 developer weeks)

Deliver:

- architecture decision records for UI, persistence, MCP boundary, and PDF stack
- clickable/static UI prototype of rename and hierarchy-diff workflows
- filesystem threat model and operation state machine
- packaging/PDF viewer proof on Windows plus one other target OS

Gate: select a 100–500 PDF test corpus and demonstrate responsive browsing, background extraction, and no UI-thread blocking.

### Phase 1: safe PDF rename vertical slice (approximately 4–6 weeks)

Deliver:

- workspaces and multi-root inventory
- PDF extraction and viewer
- deterministic naming profiles and manual proposals
- rename review, validation, journaled commit, verification, and undo
- activity log and crash recovery
- no cloud AI required

Gate: pass fault-injection tests for rename operations and complete a user-supervised trial on copied data before touching original data.

### Phase 2: AI evidence and MCP proposal editing (approximately 3–5 weeks)

Implementation baseline completed 2026-07-15. Platform OCR quality and live-provider evaluation
remain continuing test-corpus work rather than a reason to weaken the safety gate.

Deliver:

- selective OCR and confidence routing
- one direct API provider behind the provider interface
- stdio MCP server with read and proposal-write tools
- selection scopes, revisions, structured evidence, and provider privacy controls
- adversarial prompt-injection evaluation

Gate: demonstrate that no MCP or provider path can reach commit authority, arbitrary paths, or out-of-scope items.

### Phase 3: folder plan and file moves (approximately 4–6 weeks)

Implementation baseline completed 2026-07-15. Continued copied-corpus trials remain part of the
release-hardening gate.

Deliver:

- union hierarchy diff
- folder create/rename plans
- same-volume file move proposals and commits
- projected-state collision validation
- protected code/project boundary detection

Gate: execute and undo multi-step rename/move scenarios with swaps, locks, changed inputs, and application restarts.

### Phase 4: cleanup and cross-volume safety (approximately 3–5 weeks)

Implementation baseline completed 2026-07-15. Copied-corpus recovery trials and platform-specific
recycle-bin UX remain release-hardening work; the portable baseline deliberately uses the fully
restorable AIOrganizer quarantine.

Deliver:

- empty-folder and evidence-backed build-artifact cleanup
- quarantine/recycle-bin workflows
- cleanup-specific confirmation and restore
- copy-verify-finalize cross-volume moves

Gate: no permanent delete capability; recovery tests cover partial copies and insufficient space.

### Phase 5: recurring documents (approximately 3–5 weeks)

Implementation baseline completed 2026-07-15. Broader multilingual period extraction and future
mail-connector evaluation remain corpus and integration work, not permission to auto-track or
download documents.

Deliver:

- reviewed series creation
- cadence/period detection, gap matrix, exceptions, and grace periods
- attachment-ready interfaces without Outlook dependency

Gate: false gaps are explainable and individually dismissible; no automatic downloads.

### Phase 6: Microsoft Graph email integration (approximately 5–8 weeks)

Implementation baseline completed 2026-07-15. The connector is opt-in, uses delegated device sign-in,
keeps the MSAL token cache in the OS credential store, and starts with read-only scopes. Workspace
schema v10 stores bounded message/attachment metadata, per-folder opaque delta links, security
evidence, and separately reviewed proposals. Folder, message-move, category, and rule writes require
an exact incremental permission review and typed confirmation; remote message state is checked again
before mutation. The transport exposes no send, reply, forward, delete, or permanent-delete method.

Automated fake-transport coverage is complete. A sustained real Microsoft 365 test-tenant soak is
still required before the Phase 6 release gate can be declared complete; live credentials and tenant
content are deliberately not part of the repository or ordinary development workflow.

Deliver:

- one-account-at-a-time delegated sign-in
- read-only inventory, preview, and classification first
- folder/message/rule proposals and separate write-consent flow
- Accounts & Security Evidence
- recurring attachment matching

Gate: permission review, test-tenant soak, remote-state conflict handling, and no send/permanent-delete behavior.

### Phase 7: Outlook companion and broader release work

Implementation baseline completed 2026-07-15. A validated Office.js `ReadItem` task pane exports only
selected-item header/attachment descriptors through a strict metadata handoff; the desktop imports it as
untrusted semantic evidence with no mailbox or commit authority. First-run onboarding, complete workspace
backup, privacy-separated review/diagnostic exports, text scaling, high contrast, and Qt translation-catalog
loading are present. Contributor rules, a versioned provider manifest/schema, manual native package
definitions, CycloneDX SBOMs, checksums, and GitHub provenance/SBOM attestations are documented and tested.

The fast Python loop and manual-only workflows remain unchanged. Native Windows/macOS/Linux signing hooks
require explicit release mode and real external identities, pass no certificate password on the command
line, and verify signatures after signing. No publisher certificate or Apple notarization identity was
available in development, so a certificate-backed public artifact and store/notarization review remain
external release gates rather than falsely claimed results.

Deliver:

- thin Office.js Outlook task pane if user research justifies it
- Windows installer/portable packaging, then macOS/Linux packages
- onboarding, backup/export, accessibility, localization groundwork
- contribution guide, plugin/provider contracts, SBOM, and signed releases

The rough total for one experienced full-time developer is several months, not several weekends. The visible UI is manageable; reliable transactions, recovery, privacy, platform edge cases, OCR quality, and mailbox permissions contain most of the work.

## 21. Initial backlog

The first implementation backlog, in order, should be:

1. Write ADR-001 for PySide6/Qt Widgets and ADR-002 for modular-monolith boundaries.
2. Define domain types and proposal/plan/journal state machines without Qt or SQLAlchemy imports.
3. Build path identity, containment, collision, and rename-graph property tests.
4. Prototype `QPdfView` plus a virtualized rename table against a synthetic corpus.
5. Define SQLite schema and migrations for workspace, source, snapshot, item, evidence, proposal, plan, and journal.
6. Implement inventory as a read-only durable job with exclusions and link handling.
7. Implement PDF embedded-text extraction and confidence measurement.
8. Implement naming-profile parser/renderer and manual proposal creation.
9. Implement proposal review and dry-run validation.
10. Implement journaled same-directory rename, verification, restart recovery, and undo.
11. Package a Windows alpha and run it only against copied test data.
12. Add OCR, then MCP, then direct cloud AI—each behind already-tested proposal interfaces.

## 22. Decisions deliberately deferred

- final public product name and branding
- exact open-source license (Apache-2.0 is a reasonable default, subject to dependency/license review)
- local-model runtime and supported model list
- cloud synchronization of organizer metadata
- mobile or web client
- permanent deletion
- password-manager integration
- generic IMAP support
- automated website login/document download
- language-aware source-code refactoring
- third-party executable plugins

## 23. Questions to settle during Phase 0

These do not block the architecture, but they affect defaults and release order:

1. Is Windows the first supported production OS, with macOS/Linux portability preserved but certified later?
2. Which source roots and approximate item counts represent the real first workload?
3. Which languages occur in documents and OCR?
4. Which naming dimensions are genuinely useful to search by, and which already belong in folders?
5. Are source roots local NTFS, OneDrive placeholders, network shares, removable disks, or a mixture?
6. Which document categories may be sent to a cloud model, if any?
7. Is the first mailbox Outlook.com personal, Microsoft 365 work/school, or both?
8. How long should undo/quarantine data be retained, and how much duplicate storage is acceptable for cross-volume safety?

## 24. Definition of a trustworthy first release

The first public release is trustworthy when a user can understand every proposed change, inspect the evidence, edit or reject it, prove that the plan still matches current filesystem state, apply it without overwriting anything, recover after an interruption, and undo it unless a clearly reported external conflict prevents undo. AI quality can improve after release; mutation safety cannot be postponed.

## 25. Current platform references

These references support platform-specific decisions in this plan. They should be rechecked when the corresponding phase begins because SDKs, permissions, and product surfaces can change.

- [Codex authentication](https://learn.chatgpt.com/docs/auth.md): ChatGPT subscription sign-in and API-key sign-in are distinct supported local Codex paths.
- [Codex MCP support](https://learn.chatgpt.com/docs/extend/mcp.md): local Codex clients can connect to stdio and Streamable HTTP MCP servers.
- [Codex app-server](https://learn.chatgpt.com/docs/app-server.md): supported programmatic interface for rich/custom Codex clients.
- [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk.md): TypeScript and Python interfaces over Codex workflows/app-server.
- [Qt for Python PDF module](https://doc.qt.io/qtforpython-6.8/PySide6/QtPdf/index.html): `QPdfView` and Qt PDF licensing information.
- [Qt for Python deployment](https://doc.qt.io/qtforpython-6/deployment/deployment-pyside6-deploy.html): supported packaging workflow based on Nuitka.
- [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk): Python server/client SDK and supported transports.
- [Microsoft Graph Outlook mail API](https://learn.microsoft.com/en-us/graph/api/resources/mail-api-overview?view=graph-rest-1.0): mail, folder, message-rule, and immutable-ID behavior.
- [Microsoft Graph create message rule](https://learn.microsoft.com/en-us/graph/api/mailfolder-post-messagerules?view=graph-rest-1.0): rule creation and required permissions.
- [Outlook add-ins overview](https://learn.microsoft.com/en-us/office/dev/add-ins/outlook/outlook-add-ins-overview): cross-platform web add-ins and the limits of COM/VSTO in new Outlook.
