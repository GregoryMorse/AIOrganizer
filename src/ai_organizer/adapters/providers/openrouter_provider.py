from __future__ import annotations

from typing import Any

from .deepseek_provider import OpenAIChatToolProvider


class OpenRouterProvider(OpenAIChatToolProvider):
    """OpenAI Chat-compatible OpenRouter adapter with AIOrganizer-owned tools."""

    name = "openrouter"

    def __init__(
        self,
        api_key: str,
        model: str = "openai/gpt-5.2",
        base_url: str = "https://openrouter.ai/api/v1",
    ) -> None:
        from openai import OpenAI

        self.model = model
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            timeout=45.0,
            max_retries=2,
            default_headers={
                "HTTP-Referer": "https://github.com/AIOrganizer/AIOrganizer",
                "X-OpenRouter-Title": "AIOrganizer",
            },
        )

    def _tool_request_extras(self) -> dict[str, Any]:
        # Refuse routing unless the selected upstream endpoint has a declared ZDR policy.
        return {"extra_body": {"provider": {"zdr": True}}}
