from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_ -]?key|token|password|secret)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"\b\d{12,19}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\b\d{6}\b"),
]


class ProviderError(RuntimeError):
    pass


@dataclass(slots=True)
class AnalysisResult:
    findings: list[dict[str, Any]]
    usage: dict[str, int] = field(default_factory=dict)
    raw_id: str = ""


def redact(text: str) -> str:
    output = text
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 2:
            output = pattern.sub(lambda match: f"{match.group(1)}=[REDACTED]", output)
        else:
            output = pattern.sub(_mask_number_or_secret, output)
    return output


def detect_secret_kinds(text: str) -> list[str]:
    """Return finding types without retaining or returning matched values."""
    labels = ["credential_assignment", "account_identifier", "private_key", "short_code"]
    return [
        label
        for label, pattern in zip(labels, SECRET_PATTERNS, strict=True)
        if pattern.search(text)
    ]


def parse_findings(text: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ProviderError("Provider returned invalid structured JSON") from error
    findings = payload.get("findings") if isinstance(payload, dict) else None
    if not isinstance(findings, list):
        raise ProviderError("Provider response lacks a findings array")
    return [finding for finding in findings if isinstance(finding, dict)]


def finding_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "item_id": {"type": "string"},
                        "category": {"type": "string"},
                        "suggestion": {"type": "string"},
                        "rationale": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["item_id", "category", "suggestion", "rationale", "confidence"],
                },
            }
        },
        "required": ["findings"],
    }


def _mask_number_or_secret(match: re.Match[str]) -> str:
    value = match.group(0)
    if value.isdigit() and len(value) > 6:
        return f"[MASKED…{value[-4:]}]"
    return "[REDACTED]"
