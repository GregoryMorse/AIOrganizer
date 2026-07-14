# ADR-005: Filesystem transactions

Status: accepted

Every mutation is prepared in a durable journal and revalidated against an
immutable snapshot. Same-volume changes use rename scheduling. Cross-volume moves
copy to a partial target, flush, hash-verify, finalize, and quarantine the source
indefinitely. Undo is a separately validated plan and never overwrites conflicts.
