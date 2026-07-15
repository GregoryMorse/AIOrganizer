from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from .models import new_id, utc_now


class EvidenceClass(StrEnum):
    METADATA = "metadata"
    EXTRACTED_TEXT = "extracted_text"
    VISUAL_CONTENT = "visual_content"
    SECRET_LIKE = "secret_like"


class ConfidenceRoute(StrEnum):
    HIGH_CONFIDENCE = "high_confidence"
    NEEDS_REVIEW = "needs_review"
    OCR_REQUIRED = "ocr_required"
    OCR_UNAVAILABLE = "ocr_unavailable"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class SelectionScope:
    item_ids: tuple[str, ...]
    proposal_set_id: str | None = None
    id: str = field(default_factory=lambda: new_id("scope"))
    created_at: str = field(default_factory=utc_now)
    expires_at: str = field(
        default_factory=lambda: (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
    )
    status: str = "active"

    def active(self, now: datetime | None = None) -> bool:
        instant = now or datetime.now(UTC)
        return self.status == "active" and datetime.fromisoformat(self.expires_at) > instant


@dataclass(frozen=True, slots=True)
class ProviderRequestPreview:
    provider: str
    model: str
    item_ids: tuple[str, ...]
    content_classes: tuple[EvidenceClass, ...]
    redaction_count: int
    estimated_characters: int
    estimated_tokens: int
    source_policies: dict[str, str]
    allowed: bool
    blocked_reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "item_count": len(self.item_ids),
            "item_ids": list(self.item_ids),
            "content_classes": [value.value for value in self.content_classes],
            "redaction_count": self.redaction_count,
            "estimated_characters": self.estimated_characters,
            "estimated_tokens": self.estimated_tokens,
            "source_policies": self.source_policies,
            "allowed": self.allowed,
            "blocked_reasons": list(self.blocked_reasons),
        }
