from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ai_organizer.domain.prompts import CompiledPrompt


class ProviderPluginManifestV1(BaseModel):
    """Reviewable provider contract. AIOrganizer does not dynamically load it yet."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_name: Literal["aiorganizer.provider-plugin/v1"] = Field(alias="schema")
    plugin_id: str = Field(pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$", max_length=100)
    name: str = Field(min_length=1, max_length=160)
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
    entry_point: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")
    execution: Literal["in_process"]
    network_access: bool
    cloud_provider: bool
    content_classes: list[
        Literal["metadata", "extracted_text", "images", "email_metadata"]
    ] = Field(max_length=4)
    capabilities: list[Literal["estimate", "analyze"]] = Field(min_length=2, max_length=2)
    mutation_authority: Literal[False]


class ProviderPluginV1(Protocol):
    api_version: Literal["1"]
    name: str

    def estimate(self, prompt: CompiledPrompt) -> dict[str, int]: ...

    def analyze(self, prompt: CompiledPrompt) -> object: ...


def load_provider_manifest(path: Path) -> ProviderPluginManifestV1:
    if path.stat().st_size > 64 * 1024:
        raise ValueError("Provider manifests are limited to 64 KiB")
    return ProviderPluginManifestV1.model_validate(json.loads(path.read_text(encoding="utf-8")))
