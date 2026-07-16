from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .models import utc_now


class ReleaseChannel(StrEnum):
    FULL_RELEASE = "full_release"
    PRE_RELEASE = "pre_release"
    BETA = "beta"
    ALPHA = "alpha"


class UpdateUrlKind(StrEnum):
    WEB_PAGE = "web_page"
    DIRECT_FILE = "direct_file"
    NONE = "none"


class UpdateEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, Field(max_length=500)]
    url: AnyHttpUrl
    observed_version: Annotated[str, Field(max_length=200)] = ""
    observed_channel: ReleaseChannel = ReleaseChannel.FULL_RELEASE


class UpdatePageHint(BaseModel):
    """Durable instructions for checking an official update source again."""

    model_config = ConfigDict(extra="forbid")

    url: AnyHttpUrl
    page_kind: Literal[
        "release_page",
        "download_page",
        "release_feed",
        "repository_releases",
        "vendor_product_page",
    ]
    version_locator: Annotated[str, Field(min_length=1, max_length=1_000)]
    version_prefix: Annotated[str, Field(max_length=200)] = ""
    version_format: (
        Literal[
            "dotted_numeric",
            "dotted_numeric_with_suffix",
            "integer",
            "date_yyyymmdd",
        ]
        | None
    ) = None
    version_regex: Annotated[str, Field(max_length=300)] = ""
    version_capture_group: Annotated[int, Field(ge=0, le=10)] = 1
    version_normalization: Literal["trim", "strip_v_prefix"] = "strip_v_prefix"
    download_locator: Annotated[str, Field(max_length=1_000)] = ""
    release_date_locator: Annotated[str, Field(max_length=1_000)] = ""
    validation_marker: Annotated[str, Field(max_length=500)] = ""
    status: Literal["new", "reused", "revised", "relocated"] = "new"
    previous_url: AnyHttpUrl | None = None
    notes: Annotated[str, Field(max_length=1_000)] = ""

    @model_validator(mode="before")
    @classmethod
    def prefer_declarative_version_locator(cls, value: object) -> object:
        """Never validate or retain model-authored regex when a safe locator is present."""
        if isinstance(value, dict) and value.get("version_prefix") and value.get("version_format"):
            value = dict(value)
            value["version_regex"] = ""
            value["version_capture_group"] = 1
        return value

    @field_validator("version_regex")
    @classmethod
    def validate_safe_regex(cls, value: str) -> str:
        if not value:
            return value
        if any(token in value for token in ("(?", ".*", ".+")):
            raise ValueError("Version regex contains an unsafe unbounded construct")
        if re.search(r"\\[1-9]", value):
            raise ValueError("Version regex backreferences are not allowed")
        if re.search(r"\([^)]*[+*][^)]*\)[+*{]", value):
            raise ValueError("Version regex nested quantifiers are not allowed")
        try:
            re.compile(value)
        except re.error as error:
            raise ValueError("Version regex is invalid") from error
        return value

    @model_validator(mode="after")
    def compile_declarative_version_locator(self) -> UpdatePageHint:
        if self.version_format is not None:
            if not self.version_prefix.strip():
                raise ValueError("Declarative version locator requires a literal prefix")
            self.version_regex = compile_version_regex(self.version_prefix, self.version_format)
            self.version_capture_group = 1
        return self


def compile_version_regex(
    prefix: str,
    version_format: Literal[
        "dotted_numeric",
        "dotted_numeric_with_suffix",
        "integer",
        "date_yyyymmdd",
    ],
) -> str:
    """Compile a bounded matcher from declarative, non-executable AI output."""
    escaped_prefix = re.escape(prefix.strip())
    separator = r"[\s:=#-]{0,24}[vV]?"
    if version_format == "integer":
        version = r"([0-9]{1,12})"
    elif version_format == "date_yyyymmdd":
        version = r"([12][0-9]{3}[._-]?[01][0-9][._-]?[0-3][0-9])"
    else:
        dotted = r"[0-9]+(?:\.[0-9]+){0,5}"
        if version_format == "dotted_numeric_with_suffix":
            dotted += r"(?:[-+._][A-Za-z0-9][A-Za-z0-9._-]{0,39})?"
        version = f"({dotted})"
    return escaped_prefix + separator + version


class ChangelogHint(BaseModel):
    """A stable changelog location and enough context to find the matching entry."""

    model_config = ConfigDict(extra="forbid")

    url: AnyHttpUrl
    entry_locator: Annotated[str, Field(min_length=1, max_length=1_000)]
    latest_entry_version: Annotated[str, Field(max_length=200)] = ""
    latest_entry_summary: Annotated[str, Field(max_length=2_000)] = ""


class UpdateAssessment(BaseModel):
    """Strict structured result accepted from an AI update-research tool call."""

    model_config = ConfigDict(extra="forbid")

    entity_kind: Literal["software", "download"]
    entity_key: Annotated[str, Field(min_length=1, max_length=500)]
    application_name: Annotated[str, Field(min_length=1, max_length=500)]
    current_version: Annotated[str, Field(max_length=200)]
    latest_version: Annotated[str, Field(max_length=200)]
    update_available: bool
    latest_release_channel: ReleaseChannel
    official_page_url: AnyHttpUrl | None
    direct_download_url: AnyHttpUrl | None
    preferred_url_kind: UpdateUrlKind
    release_date: Annotated[str, Field(max_length=100)] | None = None
    result_status: Literal["verified", "no_update", "uncertain", "not_found"]
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    rationale: Annotated[str, Field(max_length=2_000)]
    evidence: Annotated[list[UpdateEvidence], Field(max_length=10)]
    update_page_hint: UpdatePageHint | None = None
    changelog_hint: ChangelogHint | None = None
    discovery_changed: bool = False
    next_check_strategy: Literal[
        "reuse_hint", "validate_then_reuse", "fresh_search", "manual_review"
    ] = "validate_then_reuse"
    checked_at: str = Field(default_factory=utc_now)


def release_channel_included(channel: ReleaseChannel, policy: ReleaseChannel) -> bool:
    order = {
        ReleaseChannel.FULL_RELEASE: 0,
        ReleaseChannel.PRE_RELEASE: 1,
        ReleaseChannel.BETA: 2,
        ReleaseChannel.ALPHA: 3,
    }
    return order[channel] <= order[policy]


def extract_version_with_hint(text: str, hint: UpdatePageHint) -> str:
    """Deterministically extract a version from bounded page text without executing code."""
    if not hint.version_regex:
        raise ValueError("Saved update hint has no deterministic version regex")
    match = re.search(hint.version_regex, text[:500_000], flags=re.IGNORECASE)
    if match is None:
        raise ValueError("Saved version regex no longer matches the update page")
    try:
        value = match.group(hint.version_capture_group).strip()
    except IndexError as error:
        raise ValueError("Saved version capture group does not exist") from error
    if hint.version_normalization == "strip_v_prefix":
        value = value.removeprefix("v").removeprefix("V").strip()
    if not value or len(value) > 200:
        raise ValueError("Saved version regex returned an invalid version")
    return value
