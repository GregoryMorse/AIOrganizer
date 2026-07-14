# Contributing

Use Python 3.12 and `uv`. Before opening a pull request, run:

```text
uv run ruff check .
uv run ruff format --check .
uv run mypy -p ai_organizer.domain -p ai_organizer.application
uv run pytest --cov
```

Do not contribute personal documents, mailbox exports, credentials, model
transcripts containing private content, or fixtures derived from them. Tests use
synthetic or explicitly redistributable data.

Any code that can mutate files must be isolated behind the application commit
service and include fault-injection tests. AI/MCP adapters may only create or
revise proposals.
