# Phase 0 feasibility record

Date: 2026-07-14

The reproducible generator was run with 10,000 requested mixed files and 500 PDF
paths. It produced multilingual text, office containers, archives, email, images,
audio headers, malformed PDFs, prompt injection, secret-like patterns, 25 detected
code projects, build outputs, and virtual environments.

Local Windows development-machine proof measurements:

- corpus inventory: 10,033 visible atomic items in 0.305 seconds (32,942 items/s);
- PDFs represented: 500;
- protected project bundles represented without internal traversal: 25;
- Qt model reset plus event processing for 10,000 review rows: below 1 ms at the
  timer resolution used;
- automated verification: see CI and the current local pytest report.

These numbers are a feasibility observation, not a release performance guarantee.
CI regenerates representative fixtures and exercises Windows, macOS, and Linux.
