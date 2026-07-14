from __future__ import annotations

import pytest

from ai_organizer.domain.actions import (
    ActionEngine,
    ActionFilter,
    ActionPreset,
    ActionRun,
    FilterOperator,
)
from ai_organizer.domain.models import ActionOutputMode


def test_action_filters_are_structured_and_bounded() -> None:
    preset = ActionPreset(
        "Large PDFs",
        "Review large PDFs",
        [
            ActionFilter("extension", FilterOperator.EQUALS, ".pdf"),
            ActionFilter("size", FilterOperator.GREATER_THAN, 100),
        ],
        "Explain why each is selected.",
    )
    run = ActionRun(preset.id, 1, ActionOutputMode.FINDINGS, "workspace", 10)
    findings = ActionEngine().evaluate(
        preset,
        [
            {"id": "one", "extension": ".pdf", "size": 200},
            {"id": "two", "extension": ".txt", "size": 500},
        ],
        run,
    )
    assert [finding.item_id for finding in findings.findings] == ["one"]


def test_unknown_filter_field_is_rejected() -> None:
    preset = ActionPreset("Bad", "Bad", [ActionFilter("python", FilterOperator.EQUALS, "exec")], "")
    with pytest.raises(ValueError):
        ActionEngine().validate(preset)
