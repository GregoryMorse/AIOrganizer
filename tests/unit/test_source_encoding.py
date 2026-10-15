from __future__ import annotations

from pathlib import Path


def test_python_sources_do_not_contain_common_utf8_mojibake() -> None:
    source_root = Path(__file__).parents[2] / "src" / "ai_organizer"
    suspicious = ("\u00e2\u20ac", "\u00c3", "\u00c2", "\ufffd")
    failures: list[str] = []
    for path in source_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(marker in text for marker in suspicious):
            failures.append(str(path.relative_to(source_root)))
    assert not failures, f"Possible UTF-8 mojibake in: {', '.join(failures)}"
