# Translation catalogs

AIOrganizer uses Qt translation catalogs named `aiorganizer_<locale>.qm` in this directory.
Source strings should use `QCoreApplication.translate`/`self.tr`, preserve accelerator markers,
and include translator comments where safety terminology could be ambiguous. English is the source
language and fallback. Compiled catalogs are release assets; no machine-translated safety text is
accepted without human review.
