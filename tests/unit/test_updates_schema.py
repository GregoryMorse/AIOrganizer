from __future__ import annotations

import pytest
from pydantic import ValidationError

from ai_organizer.domain.updates import (
    ReleaseChannel,
    UpdateAssessment,
    UpdatePageHint,
    extract_version_with_hint,
)


def test_update_assessment_schema_is_strict_and_requires_latest_version() -> None:
    schema = UpdateAssessment.model_json_schema()
    assert schema["additionalProperties"] is False
    assert "latest_version" in schema["required"]

    with pytest.raises(ValidationError):
        UpdateAssessment.model_validate(
            {
                "entity_kind": "software",
                "entity_key": "software_1",
                "application_name": "Example",
                "current_version": "1",
                "update_available": True,
                "latest_release_channel": ReleaseChannel.FULL_RELEASE,
                "official_page_url": "https://example.com/download",
                "direct_download_url": None,
                "preferred_url_kind": "web_page",
                "result_status": "verified",
                "confidence": 1.0,
                "rationale": "Official release page",
                "evidence": [],
            }
        )


def test_update_assessment_preserves_page_and_changelog_hints() -> None:
    value = UpdateAssessment.model_validate(
        {
            "entity_kind": "software",
            "entity_key": "software_1",
            "application_name": "Example",
            "current_version": "1",
            "latest_version": "2",
            "update_available": True,
            "latest_release_channel": "full_release",
            "official_page_url": "https://example.com/releases",
            "direct_download_url": None,
            "preferred_url_kind": "web_page",
            "result_status": "verified",
            "confidence": 0.95,
            "rationale": "Official release page",
            "evidence": [],
            "update_page_hint": {
                "url": "https://example.com/releases",
                "page_kind": "release_page",
                "version_locator": "The first stable release heading",
                "validation_marker": "Example downloads",
                "status": "reused",
            },
            "changelog_hint": {
                "url": "https://example.com/changelog",
                "entry_locator": "Heading matching the discovered version",
                "latest_entry_version": "2",
            },
            "next_check_strategy": "validate_then_reuse",
        }
    )

    assert value.update_page_hint is not None
    assert value.update_page_hint.status == "reused"
    assert value.changelog_hint is not None
    assert value.changelog_hint.latest_entry_version == "2"


def test_saved_update_hint_extracts_version_without_executing_code() -> None:
    hint = UpdatePageHint.model_validate(
        {
            "url": "https://example.com/releases",
            "page_kind": "release_page",
            "version_locator": "Stable release heading",
            "version_regex": r"Stable release\s+v?([0-9]+\.[0-9]+\.[0-9]+)",
            "version_capture_group": 1,
        }
    )

    assert extract_version_with_hint("Stable release v4.2.1", hint) == "4.2.1"


def test_declarative_hint_compiles_a_bounded_version_matcher_locally() -> None:
    hint = UpdatePageHint.model_validate(
        {
            "url": "https://example.com/releases",
            "page_kind": "release_page",
            "version_locator": "Latest stable heading",
            "version_prefix": "Latest stable",
            "version_format": "dotted_numeric_with_suffix",
            # This model-authored value is deliberately unsafe and must be ignored.
            "version_regex": r"(28\.[0-9]+\.(?:[0-9]+)*)",
        }
    )

    assert "Latest\\ stable" in hint.version_regex
    assert extract_version_with_hint("Latest stable: v28.4.1-beta", hint) == "28.4.1-beta"


@pytest.mark.parametrize(
    "pattern",
    [r"Release (.*)", r"Release (?=v)([0-9.]+)", r"(a+)+", r"(v[0-9.]+)\1"],
)
def test_saved_update_hint_rejects_unsafe_regex_constructs(pattern: str) -> None:
    with pytest.raises(ValidationError):
        UpdatePageHint.model_validate(
            {
                "url": "https://example.com/releases",
                "page_kind": "release_page",
                "version_locator": "Release heading",
                "version_regex": pattern,
            }
        )
