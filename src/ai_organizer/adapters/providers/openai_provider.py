from __future__ import annotations

from ai_organizer.domain.prompts import CompiledPrompt

from .base import AnalysisResult, ProviderError, finding_schema, parse_findings, redact


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str, model: str = "gpt-5.6-terra") -> None:
        from openai import OpenAI

        self.model = model
        self._client = OpenAI(api_key=api_key)

    def estimate(self, prompt: CompiledPrompt) -> dict[str, int]:
        return {
            "input_characters": len(prompt.text),
            "estimated_input_tokens": len(prompt.text) // 4,
        }

    def analyze(self, prompt: CompiledPrompt) -> AnalysisResult:
        try:
            response = self._client.responses.create(
                model=prompt.model or self.model,
                input=redact(prompt.text),
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "aiorganizer_findings",
                        "strict": True,
                        "schema": finding_schema(),
                    }
                },
            )
            output = response.output_text
            usage = getattr(response, "usage", None)
            return AnalysisResult(
                parse_findings(output),
                {
                    "input_tokens": int(getattr(usage, "input_tokens", 0)),
                    "output_tokens": int(getattr(usage, "output_tokens", 0)),
                },
                str(getattr(response, "id", "")),
            )
        except ProviderError:
            raise
        except Exception as error:
            raise ProviderError(f"OpenAI analysis failed: {type(error).__name__}") from error
