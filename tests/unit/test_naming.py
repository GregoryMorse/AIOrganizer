from __future__ import annotations

from ai_organizer.domain.naming import builtin_naming_profiles, disambiguate


def test_readable_profile_omits_unknowns_and_preserves_extension() -> None:
    profile = builtin_naming_profiles()[0]
    name = profile.render(
        {
            "date": "2026-07-14",
            "entity": "Example Bank",
            "document_type": "Statement",
            "descriptor": None,
            "period": "2026-06",
        },
        ".pdf",
    )
    assert name.endswith(".pdf")
    assert "Example Bank" in name
    assert "--" not in name


def test_disambiguation_is_stable_and_case_insensitive() -> None:
    assert disambiguate(["Report.pdf", "report.PDF", "other.pdf"]) == [
        "Report.pdf",
        "report - 02.PDF",
        "other.pdf",
    ]
