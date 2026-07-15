from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from PySide6.QtGui import QIcon


def brand_asset(name: str = "aiorganizer.svg") -> Path:
    return Path(__file__).resolve().parents[1] / "resources" / "icons" / name


def application_icon() -> QIcon:
    return QIcon(str(brand_asset()))


def application_version() -> str:
    try:
        return version("aiorganizer")
    except PackageNotFoundError:
        return "0.1.0a1"
