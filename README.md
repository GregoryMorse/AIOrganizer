# AIOrganizer

AIOrganizer is a local-first desktop application for reviewing and safely applying
AI-assisted file organization proposals. AI can inspect evidence and revise
proposals; only the desktop application can validate and execute a user-approved
plan.

## Current alpha scope

- Saved `.aioworkspace` workspaces with multiple filesystem sources
- User-defined hierarchical categories and enforced folder roles
- Versioned, layered AI guidance for each application view
- Inventory and content evidence for common file formats
- Rename, folder-plan, and cross-source move proposals
- Focused security and organization actions
- OpenAI, Anthropic, embedded Codex, and MCP integration points
- Journaled operations, verification, recovery records, quarantine, and undo plans

The project is Windows-first and is tested on Windows, macOS, and Linux. It is
an unsigned alpha: use copied data before using it on original files.

## Development

```powershell
uv sync --all-extras
uv run pytest
uv run aiorganizer
```

Python 3.12 is the release runtime. See the
[product and architecture plan](docs/PRODUCT_AND_ARCHITECTURE_PLAN.md),
[threat model](docs/threat-model.md), and [ADRs](docs/decisions/README.md).

## Safety boundary

MCP and model providers expose proposal-only capabilities. There is no AI-facing
approve, commit, delete, arbitrary-path, or command-execution operation. File
changes require a frozen plan, fresh preflight validation, and a user action in
the desktop application.

## License

Apache-2.0. Qt/PySide and bundled third-party components retain their own
licenses; release bundles include the required notices.
