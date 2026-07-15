from __future__ import annotations

from ai_organizer.bootstrap.mcp_registration import ensure_codex_mcp_registration


def test_mcp_auto_registration_can_be_disabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AIORGANIZER_AUTO_REGISTER_MCP", "0")

    assert ensure_codex_mcp_registration() == "disabled"
