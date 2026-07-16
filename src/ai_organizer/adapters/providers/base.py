from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ai_organizer.domain.prompts import redact_sensitive

SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_ -]?key|token|password|secret)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?<!\d)(?:\d[ -]?){8,18}\d(?!\d)"),
    re.compile(r"(?i)(?<![A-Z0-9])[A-Z]{2}\d{2}(?:[ -]?[A-Z0-9]){11,30}(?![A-Z0-9])"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"(?i)\b(?:otp|mfa|verification|reset)\s*(?:code)?\s*[:=#-]?\s*\d{4,8}\b"),
]


class ProviderError(RuntimeError):
    pass


@dataclass(slots=True)
class AnalysisResult:
    findings: list[dict[str, Any]]
    usage: dict[str, int] = field(default_factory=dict)
    raw_id: str = ""


class ProviderFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    item_id: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=100)
    suggestion: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=2_000)
    confidence: float = Field(ge=0, le=1)


class FindingsEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    findings: list[ProviderFinding] = Field(max_length=250)


def redact(text: str) -> str:
    private_terms = private_redaction_terms()
    if private_terms:
        from ai_organizer.domain.prompts import PromptCompiler

        return PromptCompiler(private_terms).redact(text)
    return redact_sensitive(text)


def private_redaction_terms() -> list[str]:
    """Load exact user-supplied private values without exposing them in logs or errors."""
    private_terms: list[str] = []
    try:
        from ai_organizer.adapters.secrets import SecretStore

        raw = SecretStore().get("private_redaction_terms")
        values = json.loads(raw) if raw else []
        if isinstance(values, list):
            private_terms = [value for value in values if isinstance(value, str)]
    except (TypeError, json.JSONDecodeError):
        private_terms = []
    return private_terms


def detect_secret_kinds(text: str) -> list[str]:
    """Return finding types without retaining or returning matched values."""
    labels = [
        "credential_assignment",
        "account_identifier",
        "iban",
        "private_key",
        "short_code",
    ]
    return [
        label
        for label, pattern in zip(labels, SECRET_PATTERNS, strict=True)
        if pattern.search(text)
    ]


def parse_findings(text: str) -> list[dict[str, Any]]:
    try:
        return [
            finding.model_dump(mode="json")
            for finding in FindingsEnvelope.model_validate_json(text).findings
        ]
    except ValidationError as error:
        raise ProviderError("Provider response failed the strict findings schema") from error


def finding_schema() -> dict[str, Any]:
    return FindingsEnvelope.model_json_schema()


def _mask_number_or_secret(match: re.Match[str]) -> str:
    value = match.group(0)
    if value.isdigit() and len(value) > 6:
        return f"[MASKED…{value[-4:]}]"
    return "[REDACTED]"
