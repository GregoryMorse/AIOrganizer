from __future__ import annotations

import os

import pytest

from ai_organizer.domain.prompts import PromptCompiler


@pytest.mark.live_provider
@pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="OPENAI_API_KEY is not configured")
def test_openai_smoke() -> None:
    from ai_organizer.adapters.providers import OpenAIProvider

    prompt = PromptCompiler().compile(
        provider="openai",
        model="gpt-5.6-terra",
        evidence='{"item_id":"item_test","name":"2026 invoice.pdf"}',
    )
    assert OpenAIProvider(os.environ["OPENAI_API_KEY"]).analyze(prompt).findings


@pytest.mark.live_provider
@pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY is not configured"
)
def test_anthropic_smoke() -> None:
    from ai_organizer.adapters.providers import AnthropicProvider

    prompt = PromptCompiler().compile(
        provider="anthropic",
        model="claude-sonnet-5",
        evidence='{"item_id":"item_test","name":"2026 invoice.pdf"}',
    )
    assert AnthropicProvider(os.environ["ANTHROPIC_API_KEY"]).analyze(prompt).findings


@pytest.mark.live_provider
@pytest.mark.skipif(
    os.getenv("AIORGANIZER_CODEX_LIVE") != "1",
    reason="AIORGANIZER_CODEX_LIVE is not enabled on an authenticated runner",
)
def test_codex_subscription_smoke() -> None:
    from ai_organizer.adapters.providers import CodexProvider, CodexRuntimeDetector

    runtime = CodexRuntimeDetector().detect()
    assert runtime and runtime.compatible
    prompt = PromptCompiler().compile(
        provider="codex",
        model="user-default",
        evidence='{"item_id":"item_test","name":"2026 invoice.pdf"}',
    )
    assert CodexProvider(runtime).analyze(prompt).findings
