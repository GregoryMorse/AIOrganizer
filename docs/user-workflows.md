# User workflows

1. Create a local workspace and add source roots. New roots intentionally start unclassified.
2. Inventory them, then manually assign semantic categories, facet tags, and routing roles or approve
   those fields in Audit. Unclassified roots are withheld from operational tools.
3. Inventory and inspect evidence.
4. Configure per-view AI guidance and cloud policy.
5. Run Document Repair when PDFs need OCR or image optimization, then separately generate and review
   Rename, Folder Plan, Move, or Focused Action proposals.
6. Accept individual items, freeze a plan, inspect preflight, and commit.
7. Review verification and retain the generated undo plan.

Use copied data for the alpha. Cross-volume moved originals remain in quarantine
until a later reviewed Cleanup operation.
# Onboarding, backup, and support export

On first launch, review the safety tour and begin with copied data. A complete workspace backup uses
**File → Backup workspace**. A review export is intended for the workspace owner and can contain private
paths and operation history. A diagnostic export is suitable for initial support triage because it excludes
paths, filenames, content/evidence, email metadata, semantic facts, proposal/journal payloads, and secrets.
Every export contains a manifest with per-file SHA-256 digests.

# Bulk review and update research

Choose **Files & Folders**, **Mail**, or **System** from the mutually exclusive **Mode** menu. In
Files & Folders mode, Inventory and proposal pages expose a shared **Focus** box: check one or more
sources, then optionally filter by a path fragment and comma-separated extension/MIME patterns such
as `pdf, .docx, image/*`. Proposal generation uses only this focused slice.

Data-table headers are clickable for typed sorting, draggable for reordering, and resizable. Use
Ctrl/Shift-click (or Ctrl+A) for row batches. In Rename, Folder Plan, Move, Audit, and Focused Actions,
right-click a selection to remove it from the current review batch or ask AI to re-propose only those
items with a short correction prompt; AI corrections return unchecked.

In Files & Folders Updates, select Download-category archives, papers, articles, or publications and
choose **Run AI research on selected**. In System > Applications, use the same reviewed research flow
for installed software. System > Drivers and Windows Update use local Windows inventory and Windows
Update Agent checks; System > Health reports storage/volume health and offers explicit read-only
fragmentation analysis for one selected volume.
DeepSeek or OpenRouter receives bounded metadata, the release-channel policy, and any preserved
official-page or changelog hint. It can use the app's bounded public HTTPS search/page-text tools; returned assessments
must pass the strict local JSON Schema. A verified page supplies a literal prefix and version-format enum;
the app compiles the bounded matcher locally. **Recheck saved hints** fetches that page and parses the version without another
AI call; a failed hint is shown in the table and can be repaired with **AI re-audit selected**. Defender,
software inventory, AI calls, and large apply/restore operations run behind modal progress dialogs.

# Tool-driven Folder Plan and evidence

Set the Folder Plan provider/model and guidance under **Settings > Folder Plan**. Provider/model
defaults are saved per view; **Settings > Updates** has the equivalent update-research configuration.
The same Folder Plan settings tab defines a preferred hierarchy depth and a hard maximum. Adaptive
reasoning may recommend fewer levels from source size and folder distribution, but neither AI nor a
commit may exceed the workspace or category proposal ceiling. This limit applies only to Folder Plan
proposals; source inventory and folder-tree browsing have no default depth limit.
On Folder Plan, choose **Propose with selected AI**. A tool-capable provider first queries bounded extension/MIME
summaries and the existing folder hierarchy, and may then search cached metadata. Participating sources
must permit at least **Metadata only** cloud processing. Returned folders are always unchecked.

# Organization profiles, categories, tags, and roles

Under **Sources & Categories**, choose **Install/refresh general defaults** to merge the general-purpose
taxonomy into an existing workspace. The operation adds missing definitions and never deletes files,
assignments, or user-created vocabulary. New workspaces receive it automatically.

- Categories describe the semantic domain, such as Finance, Teaching, Research, or Dependency Sources.
- Tags describe orthogonal content, lifecycle, state, origin, technology, and audience properties.
- Roles control workflow authority: Inbox, Downloads, Destination, Archive, Protected, and Excluded.

Adding a source asks only for its folder and records no categories, tags, or routing roles. The source
can be inventoried and audited immediately, but it is not offered to Rename, Document Repair, Folder
Plan, Move, Cleanup, or other operational tools until classification is manually assigned or explicitly
approved in Audit. Reusable source profiles remain available during manual editing. Tags may also be
inherited from a source/folder assignment or assigned directly to multiple Inventory rows. Category
default tags do not force a physical folder. Only categories
explicitly marked as folder templates are offered by the deterministic Folder Plan, and only below a source
assigned to their parent semantic category.

Audit proposals have immutable identities in the review table. Its preview follows the exact current
proposal through selection changes and sorting, and applying reviewed source-policy rows replaces the
source's categories, tags, and routing roles without changing any filesystem content.

The MCP inventory surface includes recursive glob and extension search, counts by extension/MIME/top-level
folder, a bounded current-folder tree, direct children, full cached item metadata, archive members, and
the approved organization taxonomy/depth policy through `organization_get_taxonomy`.
For document content, select files in Inventory and choose **Create MCP evidence scope**. Within that
30-minute opaque-ID scope, the AI can request cached or on-demand extraction, PDF text with local OCR,
and an individually rendered PDF page for a VLM. Every content read revalidates size and modified time;
there is no arbitrary-path tool.

# Outlook selection companion

Mail mode uses its left navigation as the tool list; it is not an Email tab among filesystem tools.
Its primary tools are **Folder Proposals**, **Rule Proposals**, and **Focused Actions**. Each uses a
multi-column review list beside the same tabbed inspector for the bounded message preview, metadata,
attachment metadata, and proposal/finding rationale.

Folder Proposals shows the existing mailbox hierarchy together with reviewed create, rename, and move
proposals. Rule Proposals requires an explicit historical sample and creates only inspected Inbox rules.
Focused Actions locally finds flagged/task-like mail, registration or security evidence, attachments
that have no AIOrganizer save record, and imported Outlook selections. Findings are reminders for human
review; they do not prove something was forgotten or unsaved.

Each mode's **Settings** shows General, Accessibility & Language, Privacy & Redaction, and Providers.
Mail adds mail-only guidance and Outlook permissions; System adds Windows assessment guidance and
safety policy. File-, mail-, and system-only settings do not leak into the other modes.

The optional Office.js task pane exports metadata for only the item visible in Outlook. Import that JSON
with **File → Import Outlook selection metadata**. Imported values remain untrusted and appear under
**Mail mode → Focused Actions**; local MCP can list the same bounded records. The handoff cannot approve,
apply, send, move, delete, download an attachment, or cause a Graph request.
