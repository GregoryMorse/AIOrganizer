from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .models import utc_now


@dataclass(slots=True)
class SemanticRecord:
    entity_kind: str
    entity_key: str
    namespace: str
    facts: dict[str, Any]
    source_fingerprint: str = ""
    confidence: float = 0.0
    provenance: str = "user"
    evidence_item_ids: list[str] = field(default_factory=list)
    status: str = "current"
    updated_at: str = field(default_factory=utc_now)
    checked_at: str = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class SoftwarePackage:
    id: str
    name: str
    publisher: str
    version: str
    source: str
    scope: str = "system"
    install_date: str = ""
    install_location: str = ""
    status: str = "installed"

    @property
    def identity_fingerprint(self) -> str:
        """Stable across versions so update-site hints survive an upgrade."""
        return semantic_fingerprint(
            {"name": self.name.casefold().strip(), "publisher": self.publisher.casefold().strip()}
        )

    @property
    def version_fingerprint(self) -> str:
        return semantic_fingerprint(
            {
                "identity": self.identity_fingerprint,
                "version": self.version,
                "source": self.source,
            }
        )


def software_package_id(name: str, publisher: str) -> str:
    value = f"{publisher.casefold().strip()}\0{name.casefold().strip()}"
    return f"software_{hashlib.sha256(value.encode()).hexdigest()[:24]}"


def semantic_fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()
