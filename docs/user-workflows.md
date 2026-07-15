# User workflows

1. Create a local workspace and add source roots.
2. Assign semantic categories, facet tags, and operational roles; approve any AI suggestions.
3. Inventory and inspect evidence.
4. Configure per-view AI guidance and cloud policy.
5. Generate and review Rename, Folder Plan, Move, or Focused Action proposals.
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

Data-table headers are clickable for typed sorting, draggable for reordering, and resizable. Use
Ctrl/Shift-click (or Ctrl+A) for row batches. In Rename, Folder Plan, Move, Audit, and Focused Actions,
right-click a selection to remove it from the current review batch or ask AI to re-propose only those
items with a short correction prompt; AI corrections return unchecked.

In Updates, select software or Download-category rows and choose **Run AI research on selected**.
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
commit may exceed the workspace, source, or category ceiling.
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

When adding a source, reusable source profiles configure suitable categories, tags, roles, privacy, and
depth overrides for common cases. Tags may also be inherited from a source/folder assignment or assigned
directly to multiple Inventory rows. Category default tags do not force a physical folder. Only categories
explicitly marked as folder templates are offered by the deterministic Folder Plan, and only below a source
assigned to their parent semantic category.

The MCP inventory surface includes recursive glob and extension search, counts by extension/MIME/top-level
folder, a bounded current-folder tree, direct children, full cached item metadata, archive members, and
the approved organization taxonomy/depth policy through `organization_get_taxonomy`.
For document content, select files in Inventory and choose **Create MCP evidence scope**. Within that
30-minute opaque-ID scope, the AI can request cached or on-demand extraction, PDF text with local OCR,
and an individually rendered PDF page for a VLM. Every content read revalidates size and modified time;
there is no arbitrary-path tool.

# Outlook selection companion

The optional Office.js task pane exports metadata for only the item visible in Outlook. Import that JSON
with **File → Import Outlook selection metadata**. Imported values remain untrusted and appear under
**Email → Outlook selections**; local MCP can list the same bounded records. The handoff cannot approve,
apply, send, move, delete, download an attachment, or cause a Graph request.
