# ADR-002: Workspace persistence

Status: accepted

Each workspace is a local SQLite `.aioworkspace` file. WAL is used while open;
Save As uses SQLite backup. Large evidence remains in an external UUID-keyed
cache, and secrets remain in the operating-system credential store.
