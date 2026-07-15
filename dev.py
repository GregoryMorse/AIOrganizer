"""Run AIOrganizer directly from the Python source tree.

This is the development entry point.  It deliberately does not build or install
the application, so edits under ``src/`` are picked up on the next launch.
"""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))


if __name__ == "__main__":
    main = import_module("ai_organizer.bootstrap.main").main
    raise SystemExit(main())
