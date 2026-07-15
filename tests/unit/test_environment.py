from __future__ import annotations

import os
from pathlib import Path

from ai_organizer.bootstrap.environment import load_development_env


def test_env_file_is_fallback_and_does_not_override_process(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / ".env"
    path.write_text("EXISTING=from-file\nNEW_VALUE='loaded'\n# ignored\n", encoding="utf-8")
    monkeypatch.setenv("EXISTING", "from-process")
    monkeypatch.delenv("NEW_VALUE", raising=False)

    assert load_development_env(path) == path

    assert os.environ["EXISTING"] == "from-process"
    assert os.environ["NEW_VALUE"] == "loaded"
