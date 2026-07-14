# ADR-004: Proposal-only AI and MCP

Status: accepted

AI providers, embedded Codex, and MCP clients can read scoped evidence and write
proposal revisions. They cannot approve, freeze, execute, delete, access arbitrary
paths, or run commands. Commit authority exists only in the desktop application.
