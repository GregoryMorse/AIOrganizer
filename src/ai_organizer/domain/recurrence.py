from __future__ import annotations

import hashlib
import re
import statistics
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from itertools import pairwise
from typing import Any, Protocol

from .models import new_id, utc_now


class Cadence(StrEnum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"


class GapStatus(StrEnum):
    PRESENT_VERIFIED = "present_verified"
    PROBABLY_PRESENT = "probably_present"
    MISSING = "missing"
    NOT_DUE = "not_due"
    WITHIN_GRACE = "within_grace"
    SKIPPED = "intentionally_skipped"
    IGNORED = "ignored"


@dataclass(frozen=True, slots=True)
class SeriesObservation:
    item_id: str
    period_start: str
    confidence: float
    evidence: tuple[str, ...]
    root_id: str = ""
    relative_path: str = ""
    source_fingerprint: str = ""


@dataclass(frozen=True, slots=True)
class SeriesCandidate:
    name: str
    issuer: str
    document_type: str
    masked_account_id: str
    cadence: Cadence
    cadence_confidence: float
    observations: tuple[SeriesObservation, ...]
    stable_fingerprint: str
    rationale: tuple[str, ...]
    id: str = field(default_factory=lambda: new_id("series_candidate"))


@dataclass(slots=True)
class RecurrenceSeries:
    name: str
    issuer: str
    document_type: str
    masked_account_id: str
    cadence: Cadence
    start_period: str
    end_period: str | None
    grace_days: int
    observations: tuple[SeriesObservation, ...]
    stable_fingerprint: str
    id: str = field(default_factory=lambda: new_id("series"))
    status: str = "reviewed"
    revision: int = 1
    created_at: str = field(default_factory=utc_now)

    def validate(self) -> None:
        start = date.fromisoformat(self.start_period)
        end = date.fromisoformat(self.end_period) if self.end_period else None
        if end and end < start:
            raise ValueError("Series end period cannot precede its start period")
        if not 0 <= self.grace_days <= 180:
            raise ValueError("Grace period must be between 0 and 180 days")
        if not self.name.strip() or not self.issuer.strip() or not self.document_type.strip():
            raise ValueError("Series name, issuer, and document type are required")
        if self.masked_account_id and not re.fullmatch(
            r"(?:••••|\*{4})\d{2,6}", self.masked_account_id
        ):
            raise ValueError("Account identifiers must contain only a masked suffix")


@dataclass(frozen=True, slots=True)
class RecurrenceException:
    series_id: str
    period_start: str
    status: GapStatus
    reason: str
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if self.status not in {GapStatus.SKIPPED, GapStatus.IGNORED}:
            raise ValueError("Only skipped or ignored recurrence exceptions may be saved")
        date.fromisoformat(self.period_start)


@dataclass(frozen=True, slots=True)
class GapRow:
    period_start: str
    period_label: str
    due_date: str
    status: GapStatus
    item_ids: tuple[str, ...]
    explanation: str


@dataclass(frozen=True, slots=True)
class AttachmentMetadata:
    connector_id: str
    message_id: str
    attachment_id: str
    filename: str
    mime_type: str
    size: int
    received_at: str
    sanitized_subject: str = ""


class AttachmentMetadataSource(Protocol):
    """Read-only metadata boundary; deliberately has no content/download operation."""

    def list_attachment_metadata(self) -> Iterable[AttachmentMetadata]: ...


@dataclass(frozen=True, slots=True)
class AttachmentMatch:
    series_id: str
    attachment_id: str
    period_start: str
    confidence: float
    reasons: tuple[str, ...]


class SeriesCandidateBuilder:
    def build(
        self,
        items: Iterable[dict[str, Any]],
        evidence_by_item: dict[str, list[dict[str, Any]]] | None = None,
    ) -> list[SeriesCandidate]:
        evidence_by_item = evidence_by_item or {}
        grouped: dict[str, list[tuple[dict[str, Any], date, float, tuple[str, ...]]]] = defaultdict(
            list
        )
        identities: dict[str, tuple[str, str, str, str]] = {}
        for item in items:
            if item.get("is_dir"):
                continue
            filename = str(
                item.get("name") or str(item.get("relative_path", "")).rsplit("/", 1)[-1]
            )
            period, period_evidence = _period_from_item(
                filename, evidence_by_item.get(str(item.get("id", "")), [])
            )
            if period is None:
                continue
            stem = filename.rsplit(".", 1)[0]
            normalized = _without_period(stem)
            document_type = _document_type(normalized)
            if not document_type:
                continue
            issuer = _issuer(normalized, document_type)
            if not issuer:
                continue
            masked = _masked_account(
                " ".join(
                    [
                        filename,
                        *[
                            str(value.get("summary", ""))
                            for value in evidence_by_item.get(str(item.get("id", "")), [])
                        ],
                    ]
                )
            )
            fingerprint = hashlib.sha256(
                f"{issuer.casefold()}|{document_type.casefold()}|{masked}|{_stable_words(normalized)}".encode()
            ).hexdigest()[:24]
            confidence = 0.82 if period_evidence.startswith("reviewed evidence") else 0.68
            grouped[fingerprint].append(
                (item, period, confidence, (period_evidence, "stable normalized filename pattern"))
            )
            identities[fingerprint] = (issuer, document_type, masked, normalized)

        candidates: list[SeriesCandidate] = []
        for fingerprint, members in grouped.items():
            distinct_periods = sorted({member[1] for member in members})
            if len(distinct_periods) < 2:
                continue
            cadence, cadence_confidence = detect_cadence(distinct_periods)
            if cadence is None:
                continue
            issuer, document_type, masked, _normalized = identities[fingerprint]
            observations = tuple(
                SeriesObservation(
                    str(item["id"]),
                    normalize_period(period, cadence).isoformat(),
                    confidence,
                    evidence,
                    str(item.get("root_id", "")),
                    str(item.get("relative_path", "")),
                    f"{item.get('size', 0)}:{item.get('modified_ns', 0)}",
                )
                for item, period, confidence, evidence in sorted(
                    members, key=lambda value: value[1]
                )
            )
            candidates.append(
                SeriesCandidate(
                    f"{issuer} — {document_type}",
                    issuer,
                    document_type,
                    masked,
                    cadence,
                    cadence_confidence,
                    observations,
                    fingerprint,
                    (
                        f"{len(observations)} documents cover {len(distinct_periods)} distinct periods",
                        "membership uses period evidence plus a stable normalized filename fingerprint",
                        "candidate remains untracked until reviewed and confirmed",
                    ),
                )
            )
        return sorted(candidates, key=lambda value: value.name.casefold())


class GapMatrix:
    def build(
        self,
        series: RecurrenceSeries,
        exceptions: Iterable[RecurrenceException] = (),
        *,
        as_of: date | None = None,
    ) -> list[GapRow]:
        series.validate()
        today = as_of or date.today()
        start = normalize_period(date.fromisoformat(series.start_period), series.cadence)
        requested_end = (
            normalize_period(date.fromisoformat(series.end_period), series.cadence)
            if series.end_period
            else normalize_period(today, series.cadence)
        )
        exception_lookup = {value.period_start: value for value in exceptions}
        observations: dict[str, list[SeriesObservation]] = defaultdict(list)
        for observation in series.observations:
            period = normalize_period(
                date.fromisoformat(observation.period_start), series.cadence
            ).isoformat()
            observations[period].append(observation)
        rows: list[GapRow] = []
        current = start
        while current <= requested_end:
            key = current.isoformat()
            period_end = advance_period(current, series.cadence) - timedelta(days=1)
            due = period_end + timedelta(days=series.grace_days)
            members = observations.get(key, [])
            exception = exception_lookup.get(key)
            if members:
                verified = len(members) == 1 and members[0].confidence >= 0.8
                status = GapStatus.PRESENT_VERIFIED if verified else GapStatus.PROBABLY_PRESENT
                explanation = (
                    "One reviewed observation has confidence at or above 0.80."
                    if verified
                    else "An observation exists but is ambiguous, duplicated, or below 0.80 confidence."
                )
            elif exception:
                status = exception.status
                explanation = f"User exception: {exception.reason}"
            elif today <= period_end:
                status = GapStatus.NOT_DUE
                explanation = f"The {period_label(current, series.cadence)} period has not ended."
            elif today <= due:
                status = GapStatus.WITHIN_GRACE
                explanation = (
                    f"The period ended but remains inside the {series.grace_days}-day grace window."
                )
            else:
                status = GapStatus.MISSING
                explanation = (
                    "No reviewed observation or exception covers this expected period; "
                    f"its grace deadline was {due.isoformat()}."
                )
            rows.append(
                GapRow(
                    key,
                    period_label(current, series.cadence),
                    due.isoformat(),
                    status,
                    tuple(value.item_id for value in members),
                    explanation,
                )
            )
            current = advance_period(current, series.cadence)
        return rows


class AttachmentMatcher:
    def match(
        self,
        attachment: AttachmentMetadata,
        series: RecurrenceSeries,
        missing_periods: set[str],
    ) -> AttachmentMatch | None:
        period = parse_period(attachment.filename)
        if period is None:
            return None
        normalized_period = normalize_period(period, series.cadence).isoformat()
        if normalized_period not in missing_periods:
            return None
        haystack = f"{attachment.filename} {attachment.sanitized_subject}".casefold()
        issuer_words = [word for word in _words(series.issuer) if len(word) >= 3]
        matched_words = [word for word in issuer_words if word in haystack]
        if issuer_words and not matched_words:
            return None
        reasons = ["attachment period matches an individually missing series period"]
        confidence = 0.65
        if matched_words:
            confidence += 0.2
            reasons.append("issuer token matches attachment metadata")
        suffix = re.sub(r"\D", "", series.masked_account_id)[-4:]
        if suffix and suffix in haystack:
            confidence += 0.1
            reasons.append("masked account suffix matches")
        return AttachmentMatch(
            series.id,
            attachment.attachment_id,
            normalized_period,
            min(confidence, 0.95),
            tuple(reasons),
        )


def detect_cadence(periods: list[date]) -> tuple[Cadence | None, float]:
    ordered = sorted(set(periods))
    if len(ordered) < 2:
        return None, 0.0
    gaps = [
        (right.year - left.year) * 12 + right.month - left.month
        for left, right in pairwise(ordered)
    ]
    median = statistics.median(gaps)
    cadence = min(
        (Cadence.MONTHLY, Cadence.QUARTERLY, Cadence.ANNUAL),
        key=lambda value: abs(_cadence_months(value) - median),
    )
    expected = _cadence_months(cadence)
    confidence = sum(abs(gap - expected) <= max(1, expected // 4) for gap in gaps) / len(gaps)
    return (cadence, confidence) if confidence >= 0.5 else (None, confidence)


def parse_period(value: str) -> date | None:
    quarter = re.search(r"(?<!\d)(20\d{2})[\s._-]*Q([1-4])(?!\d)", value, re.IGNORECASE)
    if quarter:
        return date(int(quarter.group(1)), (int(quarter.group(2)) - 1) * 3 + 1, 1)
    numeric = re.search(r"(?<!\d)(20\d{2})[\s._-]?(0[1-9]|1[0-2])(?:[\s._-]\d{2})?(?!\d)", value)
    if numeric:
        return date(int(numeric.group(1)), int(numeric.group(2)), 1)
    months = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    named = re.search(
        r"(?i)(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        r"[\s._-]+(20\d{2})",
        value,
    )
    if named:
        return date(int(named.group(2)), months[named.group(1)[:3].casefold()], 1)
    year = re.search(r"(?<!\d)(20\d{2})(?!\d)", value)
    return date(int(year.group(1)), 1, 1) if year else None


def normalize_period(value: date, cadence: Cadence) -> date:
    if cadence == Cadence.MONTHLY:
        return value.replace(day=1)
    if cadence == Cadence.QUARTERLY:
        return date(value.year, ((value.month - 1) // 3) * 3 + 1, 1)
    return date(value.year, 1, 1)


def advance_period(value: date, cadence: Cadence) -> date:
    months = _cadence_months(cadence)
    total = value.year * 12 + value.month - 1 + months
    return date(total // 12, total % 12 + 1, 1)


def period_label(value: date, cadence: Cadence) -> str:
    if cadence == Cadence.MONTHLY:
        return value.strftime("%Y-%m")
    if cadence == Cadence.QUARTERLY:
        return f"{value.year}-Q{(value.month - 1) // 3 + 1}"
    return str(value.year)


def _period_from_item(filename: str, evidence: list[dict[str, Any]]) -> tuple[date | None, str]:
    for record in evidence:
        facts = record.get("facts", {})
        for key in ("statement_period", "coverage_period", "period", "period_start"):
            value = facts.get(key)
            if value and (parsed := parse_period(str(value))):
                return parsed, f"extracted evidence field {key}; requires review"
    return parse_period(filename), "filename period token; requires review"


def _without_period(value: str) -> str:
    value = re.sub(r"(?i)(20\d{2})[\s._-]*Q[1-4]", " ", value)
    value = re.sub(
        r"(?i)(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        r"[\s._-]+20\d{2}",
        " ",
        value,
    )
    value = re.sub(r"(?<!\d)20\d{2}[\s._-]?(?:0[1-9]|1[0-2])(?:[\s._-]\d{2})?(?!\d)", " ", value)
    value = re.sub(r"(?<!\d)20\d{2}(?!\d)", " ", value)
    return " ".join(re.sub(r"[_-]+", " ", value).split())


def _document_type(value: str) -> str:
    folded = value.casefold()
    for kind in (
        "bank statement",
        "credit card statement",
        "statement",
        "invoice",
        "payslip",
        "pay stub",
        "bill",
        "report",
        "receipt",
    ):
        if kind in folded:
            return kind.title()
    return ""


def _issuer(value: str, document_type: str) -> str:
    cleaned = re.sub(re.escape(document_type), " ", value, flags=re.IGNORECASE)
    cleaned = re.sub(r"(?i)\b(?:account|acct)\b[\s#*-]*\d+", " ", cleaned)
    words = [word for word in _words(cleaned) if not word.isdigit()]
    return " ".join(word.title() for word in words[:6])


def _masked_account(value: str) -> str:
    match = re.search(r"(?i)\b(?:account|acct)\b[^\d]{0,8}(\d{2,16})", value)
    return f"••••{match.group(1)[-4:]}" if match else ""


def _stable_words(value: str) -> str:
    return " ".join(word for word in _words(value) if not word.isdigit() and len(word) > 1)


def _words(value: str) -> list[str]:
    return re.findall(r"[\w]+", value.casefold(), re.UNICODE)


def _cadence_months(value: Cadence) -> int:
    return {Cadence.MONTHLY: 1, Cadence.QUARTERLY: 3, Cadence.ANNUAL: 12}[value]


def recurrence_series_from_payload(payload: dict[str, Any]) -> RecurrenceSeries:
    return RecurrenceSeries(
        name=str(payload["name"]),
        issuer=str(payload["issuer"]),
        document_type=str(payload["document_type"]),
        masked_account_id=str(payload.get("masked_account_id", "")),
        cadence=Cadence(str(payload["cadence"])),
        start_period=str(payload["start_period"]),
        end_period=str(payload["end_period"]) if payload.get("end_period") else None,
        grace_days=int(payload["grace_days"]),
        observations=tuple(
            SeriesObservation(
                str(value["item_id"]),
                str(value["period_start"]),
                float(value["confidence"]),
                tuple(str(item) for item in value.get("evidence", [])),
                str(value.get("root_id", "")),
                str(value.get("relative_path", "")),
                str(value.get("source_fingerprint", "")),
            )
            for value in payload.get("observations", [])
        ),
        stable_fingerprint=str(payload["stable_fingerprint"]),
        id=str(payload["id"]),
        status=str(payload.get("status", "reviewed")),
        revision=int(payload.get("revision", 1)),
        created_at=str(payload.get("created_at", "")),
    )


def rebind_observations(
    observations: Iterable[SeriesObservation], items: Iterable[dict[str, Any]]
) -> tuple[SeriesObservation, ...]:
    current_by_path = {(str(item["root_id"]), str(item["relative_path"])): item for item in items}
    rebound: list[SeriesObservation] = []
    for observation in observations:
        if not observation.root_id or not observation.relative_path:
            rebound.append(observation)
            continue
        item = current_by_path.get((observation.root_id, observation.relative_path))
        if item is None:
            continue
        fingerprint = f"{item.get('size', 0)}:{item.get('modified_ns', 0)}"
        changed = bool(
            observation.source_fingerprint and observation.source_fingerprint != fingerprint
        )
        rebound.append(
            SeriesObservation(
                str(item["id"]),
                observation.period_start,
                min(observation.confidence, 0.65) if changed else observation.confidence,
                observation.evidence
                + (("source fingerprint changed; membership needs review",) if changed else ()),
                observation.root_id,
                observation.relative_path,
                fingerprint,
            )
        )
    return tuple(rebound)
