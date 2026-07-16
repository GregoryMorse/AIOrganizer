from __future__ import annotations

from datetime import date

from ai_organizer.domain.recurrence import (
    AttachmentMatcher,
    AttachmentMetadata,
    AttachmentMetadataSource,
    Cadence,
    GapMatrix,
    GapStatus,
    RecurrenceException,
    RecurrenceSeries,
    SeriesCandidateBuilder,
    SeriesObservation,
    detect_cadence,
    parse_period,
)


def observation(item_id: str, period: str, confidence: float = 0.85) -> SeriesObservation:
    return SeriesObservation(item_id, period, confidence, ("reviewed period evidence",))


def series(*observations: SeriesObservation) -> RecurrenceSeries:
    return RecurrenceSeries(
        "Acme statements",
        "Acme Bank",
        "Statement",
        "••••1234",
        Cadence.MONTHLY,
        "2026-01-01",
        None,
        14,
        observations,
        "fingerprint",
        id="series",
    )


def test_period_and_cadence_detection_support_common_period_tokens() -> None:
    assert parse_period("Statement 2026-04.pdf") == date(2026, 4, 1)
    assert parse_period("Statement 2026Q3.pdf") == date(2026, 7, 1)
    assert parse_period("Statement March 2026.pdf") == date(2026, 3, 1)
    cadence, confidence = detect_cadence([date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)])
    assert cadence == Cadence.MONTHLY
    assert confidence == 1.0


def test_candidate_requires_repeated_periods_and_remains_reviewable() -> None:
    items = [
        {
            "id": "jan",
            "name": "Acme Bank Statement Account 1234 2026-01.pdf",
            "relative_path": "Acme Bank Statement Account 1234 2026-01.pdf",
            "extension": ".pdf",
        },
        {
            "id": "feb",
            "name": "Acme Bank Statement Account 1234 2026-02.pdf",
            "relative_path": "Acme Bank Statement Account 1234 2026-02.pdf",
            "extension": ".pdf",
        },
    ]
    candidates = SeriesCandidateBuilder().build(items)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.issuer == "Acme"
    assert candidate.document_type == "Bank Statement"
    assert candidate.masked_account_id == "••••1234"
    assert candidate.cadence == Cadence.MONTHLY
    assert {value.period_start for value in candidate.observations} == {
        "2026-01-01",
        "2026-02-01",
    }
    assert "untracked until reviewed" in " ".join(candidate.rationale)


def test_named_months_share_a_stable_candidate_fingerprint() -> None:
    items = [
        {"id": "mar", "name": "Acme Invoice March 2026.pdf"},
        {"id": "apr", "name": "Acme Invoice April 2026.pdf"},
    ]
    candidates = SeriesCandidateBuilder().build(items)
    assert len(candidates) == 1
    assert candidates[0].cadence == Cadence.MONTHLY


def test_gap_matrix_explains_present_ambiguous_missing_grace_and_not_due() -> None:
    tracked = series(
        observation("jan", "2026-01-01"),
        observation("mar", "2026-03-01", 0.65),
    )
    rows = GapMatrix().build(tracked, as_of=date(2026, 6, 10))
    statuses = {row.period_label: row.status for row in rows}
    assert statuses == {
        "2026-01": GapStatus.PRESENT_VERIFIED,
        "2026-02": GapStatus.MISSING,
        "2026-03": GapStatus.PROBABLY_PRESENT,
        "2026-04": GapStatus.MISSING,
        "2026-05": GapStatus.WITHIN_GRACE,
        "2026-06": GapStatus.NOT_DUE,
    }
    assert all(row.explanation for row in rows)
    assert "grace deadline" in next(
        row.explanation for row in rows if row.period_label == "2026-02"
    )


def test_individual_gap_exception_is_preserved_and_explained() -> None:
    exception = RecurrenceException(
        "series", "2026-02-01", GapStatus.IGNORED, "Provider issued no statement"
    )
    rows = GapMatrix().build(
        series(observation("jan", "2026-01-01")),
        [exception],
        as_of=date(2026, 3, 20),
    )
    february = next(row for row in rows if row.period_label == "2026-02")
    assert february.status == GapStatus.IGNORED
    assert "Provider issued no statement" in february.explanation


def test_attachment_matcher_is_metadata_only_and_targets_missing_period() -> None:
    attachment = AttachmentMetadata(
        "outlook-future",
        "message",
        "attachment",
        "Acme Bank Statement 1234 2026-02.pdf",
        "application/pdf",
        100,
        "2026-03-03T00:00:00+00:00",
        "Your Acme Bank statement",
    )
    match = AttachmentMatcher().match(
        attachment,
        series(observation("jan", "2026-01-01")),
        {"2026-02-01"},
    )
    assert match is not None
    assert match.period_start == "2026-02-01"
    assert match.confidence >= 0.85
    assert not hasattr(AttachmentMetadataSource, "download")
