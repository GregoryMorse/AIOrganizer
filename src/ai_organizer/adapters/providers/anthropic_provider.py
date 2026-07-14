from __future__ import annotations

from ai_organizer.domain.prompts import CompiledPrompt

from .base import AnalysisResult, ProviderError, finding_schema, parse_findings, redact


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str = "claude-sonnet-5") -> None:
        from anthropic import Anthropic

        self.model = model
        self._client = Anthropic(api_key=api_key)

    def estimate(self, prompt: CompiledPrompt) -> dict[str, int]:
        return {
            "input_characters": len(prompt.text),
            "estimated_input_tokens": len(prompt.text) // 4,
        }

    def analyze(self, prompt: CompiledPrompt) -> AnalysisResult:
        try:
            response = self._client.messages.create(
                model=prompt.model or self.model,
                max_tokens=8_000,
                system="Return a schema-valid AIOrganizer proposal analysis.",
                messages=[{"role": "user", "content": redact(prompt.text)}],
                output_config={
                    "format": {
                        "type": "json_schema",
                        "schema": finding_schema(),
                    }
                },
            )
            text = "".join(
                str(block.text)
                for block in response.content
                if getattr(block, "type", "") == "text"
            )
            return AnalysisResult(
                parse_findings(text),
                {
                    "input_tokens": int(getattr(response.usage, "input_tokens", 0)),
                    "output_tokens": int(getattr(response.usage, "output_tokens", 0)),
                },
                str(response.id),
            )
        except ProviderError:
            raise
        except Exception as error:
            raise ProviderError(f"Anthropic analysis failed: {type(error).__name__}") from error
