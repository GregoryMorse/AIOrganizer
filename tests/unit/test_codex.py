from __future__ import annotations

import json

from ai_organizer.adapters.providers import CodexProvider, CodexRuntime


def test_codex_configuration_disables_search_network_and_unscoped_mcp(tmp_path) -> None:
    workspace = tmp_path / "example.aioworkspace"
    provider = CodexProvider(CodexRuntime(("codex",), "installed", "test", True), str(workspace))
    overrides = provider._config_overrides()
    assert 'web_search="disabled"' in overrides
    assert "sandbox_workspace_write.network_access=false" in overrides
    assert "mcp_servers={}" in overrides
    assert any(value.startswith("mcp_servers.aiorganizer.command=") for value in overrides)
    args_override = next(
        value for value in overrides if value.startswith("mcp_servers.aiorganizer.args=")
    )
    assert str(workspace) in json.loads(args_override.split("=", 1)[1])
