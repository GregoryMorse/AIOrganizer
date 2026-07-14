# ADR-008: Focused-action predicates

Status: accepted

Focused Actions are versioned presets built from a closed set of typed predicates.
They cannot contain scripts, expressions, filesystem traversal, or tool schemas.
Security presets default to findings-only. An action can narrow scope or cloud use
but cannot relax root, category, privacy, destination, or commit policy.
