# Contributing to AIOrganizer

AIOrganizer is safety-first desktop software. A useful AI suggestion is never worth an unreviewable or
irreversible mutation.

## Development loop

Use Python source directly:

```powershell
uv sync --extra desktop --extra analysis --extra mcp --extra email --extra dev
.\dev.cmd
```

Normal pushes do not start CI or release packaging. Before submitting a change, run:

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe dev.py --smoke-test
```

## Safety requirements

- Preserve proposal → review → freeze/preflight → apply separation.
- Never add permanent deletion, overwrite-on-collision, arbitrary shell execution, or hidden mailbox writes.
- Treat filenames, documents, archives, email, websites, model output, and imported handoffs as untrusted.
- Keep credentials and Microsoft tokens in the OS credential store; never fixtures, logs, exports, or workspaces.
- Add stale-state, partial-failure, restart/recovery, and adversarial-input tests for mutation paths.
- Do not weaken root containment, symlink, case/Unicode collision, size/hash verification, or remote-state checks.
- Preserve the fast Python loop. Packaging and native compilation must remain explicit release operations.

## Changes and reviews

Keep unrelated work out of a change. Document schema migrations and make a backup before migration.
New dependencies require license, maintenance, binary-size, platform, and supply-chain review. User-visible
safety text and translation changes require human review. See the architecture plan, threat model, ADRs, and
provider/plugin contract before changing a trust boundary.

Do not submit personal documents, mail, credentials, proprietary corpora, or generated files containing them.
Use the synthetic corpus and fake transports.
