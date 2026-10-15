from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .models import new_id, utc_now

READ_SCOPES = ("User.Read", "Mail.Read")
MAIL_WRITE_SCOPES = ("Mail.ReadWrite",)
RULE_WRITE_SCOPES = ("MailboxSettings.ReadWrite",)


class EmailProposalKind(StrEnum):
    FOLDER_CREATE = "folder_create"
    FOLDER_RENAME = "folder_rename"
    FOLDER_MOVE = "folder_move"
    MESSAGE_MOVE = "message_move"
    MESSAGE_CATEGORIZE = "message_categorize"
    RULE_CREATE = "rule_create"


class EmailProposalStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    BLOCKED = "blocked"
    STALE = "stale"
    COMPLETED = "completed"


class EmailConnectorKind(StrEnum):
    MICROSOFT_GRAPH = "microsoft_graph"
    CLASSIC_OUTLOOK = "classic_outlook"


@dataclass(slots=True)
class EmailAccount:
    username: str
    display_name: str
    tenant_id: str = ""
    home_account_id: str = ""
    granted_scopes: tuple[str, ...] = READ_SCOPES
    active: bool = True
    id: str = field(default_factory=lambda: new_id("email_account"))
    revision: int = 1
    updated_at: str = field(default_factory=utc_now)
    connector_kind: str = EmailConnectorKind.MICROSOFT_GRAPH.value
    connector_ref: str = ""


@dataclass(frozen=True, slots=True)
class MailFolderSnapshot:
    account_id: str
    id: str
    display_name: str
    parent_folder_id: str = ""
    child_folder_count: int = 0
    total_item_count: int = 0
    unread_item_count: int = 0
    etag: str = ""
    synced_at: str = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class MailMessageSnapshot:
    account_id: str
    id: str
    folder_id: str
    subject: str
    sender_name: str = ""
    sender_address: str = ""
    received_at: str = ""
    body_preview: str = ""
    internet_message_id: str = ""
    conversation_id: str = ""
    has_attachments: bool = False
    is_read: bool = False
    change_key: str = ""
    etag: str = ""
    categories: tuple[str, ...] = ()
    removed: bool = False
    synced_at: str = field(default_factory=utc_now)
    to_recipients: tuple[str, ...] = ()
    cc_recipients: tuple[str, ...] = ()
    sent_at: str = ""
    importance: str = "normal"
    flag_status: str = "notFlagged"


@dataclass(frozen=True, slots=True)
class MailAttachmentSnapshot:
    account_id: str
    message_id: str
    id: str
    filename: str
    mime_type: str
    size: int
    is_inline: bool = False
    received_at: str = ""
    sanitized_subject: str = ""
    synced_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class EmailProposal:
    account_id: str
    kind: EmailProposalKind
    payload: dict[str, Any]
    expected_remote: dict[str, str]
    rationale: str
    confidence: float
    status: EmailProposalStatus = EmailProposalStatus.PROPOSED
    id: str = field(default_factory=lambda: new_id("email_proposal"))
    revision: int = 1
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def validate(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError("Email proposal confidence must be between 0 and 1")
        if not self.rationale.strip():
            raise ValueError("Email proposals require a rationale")
        if self.kind == EmailProposalKind.FOLDER_CREATE:
            _require_keys(self.payload, {"parent_folder_id", "display_name"})
            if not str(self.payload["display_name"]).strip():
                raise ValueError("Folder display name is required")
        elif self.kind == EmailProposalKind.FOLDER_RENAME:
            _require_keys(self.payload, {"folder_id", "display_name"})
            _require_keys(self.expected_remote, {"display_name", "parent_folder_id"})
            if not str(self.payload["display_name"]).strip():
                raise ValueError("Folder display name is required")
        elif self.kind == EmailProposalKind.FOLDER_MOVE:
            _require_keys(self.payload, {"folder_id", "destination_folder_id"})
            _require_keys(self.expected_remote, {"display_name", "parent_folder_id"})
            if self.payload["folder_id"] == self.payload["destination_folder_id"]:
                raise ValueError("A mail folder cannot be moved into itself")
        elif self.kind == EmailProposalKind.MESSAGE_MOVE:
            _require_keys(self.payload, {"message_id", "destination_folder_id"})
            _require_keys(self.expected_remote, {"folder_id", "change_key"})
        elif self.kind == EmailProposalKind.MESSAGE_CATEGORIZE:
            _require_keys(self.payload, {"message_id", "categories"})
            _require_keys(self.expected_remote, {"folder_id", "change_key"})
            if not isinstance(self.payload["categories"], list):
                raise ValueError("Message categories must be a reviewed list")
        elif self.kind == EmailProposalKind.RULE_CREATE:
            _validate_rule(self.payload)
        else:
            raise ValueError("Unsupported email proposal operation")

    @property
    def required_scopes(self) -> tuple[str, ...]:
        if self.kind == EmailProposalKind.RULE_CREATE:
            return RULE_WRITE_SCOPES
        return MAIL_WRITE_SCOPES


@dataclass(frozen=True, slots=True)
class PermissionReview:
    current_scopes: tuple[str, ...]
    requested_scopes: tuple[str, ...]
    additional_scopes: tuple[str, ...]
    actions: tuple[str, ...]
    sending_allowed: bool = False
    permanent_delete_allowed: bool = False


@dataclass(frozen=True, slots=True)
class AccountSecurityEvidence:
    account_id: str
    service_key: str
    display_name: str
    mailbox: str
    first_evidence_at: str
    last_evidence_at: str
    categories: tuple[str, ...]
    message_ids: tuple[str, ...]
    rationale: str
    id: str = field(default_factory=lambda: new_id("account_evidence"))


_SECURITY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("registration", re.compile(r"\b(register(?:ed|ation)?|account created)\b", re.I)),
    ("welcome", re.compile(r"\bwelcome\b", re.I)),
    ("verification", re.compile(r"\b(verif(?:y|ication)|confirm (?:your )?email)\b", re.I)),
    ("security_alert", re.compile(r"\b(security alert|new sign[- ]?in|unusual activity)\b", re.I)),
    ("password_reset", re.compile(r"\b(password reset|reset your password)\b", re.I)),
    ("mfa", re.compile(r"\b(mfa|two[- ]factor|2fa|authentication code)\b", re.I)),
    ("billing", re.compile(r"\b(invoice|receipt|billing|payment)\b", re.I)),
    ("cancellation", re.compile(r"\b(cancel(?:led|lation)?|subscription ended)\b", re.I)),
)


def security_categories(subject: str, sender_name: str = "") -> tuple[str, ...]:
    text = f"{subject} {sender_name}"
    return tuple(name for name, pattern in _SECURITY_PATTERNS if pattern.search(text))


def sanitized_preview(value: str, limit: int = 512) -> str:
    """Bound untrusted previews and remove likely links, tokens, and one-time codes."""
    cleaned = re.sub(r"https?://\S+", "[link redacted]", value)
    cleaned = re.sub(
        r"(?i)\b(token|code|otp|password|secret)\s*[:=]\s*[A-Za-z0-9._~+/=-]{4,}",
        r"\1: [redacted]",
        cleaned,
    )
    cleaned = re.sub(r"\b\d{6,8}\b", "[code redacted]", cleaned)
    return " ".join(cleaned.split())[:limit]


_TASK_PATTERN = re.compile(
    r"\b(action required|approval required|deadline|due(?: date)?|overdue|reminder|"
    r"please respond|please review|please sign|urgent|task|todo|to-do)\b",
    re.I,
)
_REGISTRATION_PATTERN = re.compile(
    r"\b(account (?:created|registration)|activate your account|confirm (?:your )?email|"
    r"complete (?:your )?registration|registration (?:complete|confirmation)|"
    r"sign[- ]?up (?:complete|confirmation)|verify (?:your )?(?:account|email)|welcome to)\b",
    re.I,
)
_INVOICE_PATTERN = re.compile(
    r"\b(invoice|receipt|billing statement|order confirmation|payment (?:confirmation|received)|"
    r"purchase confirmation|tax invoice)\b",
    re.I,
)
_RENEWAL_PATTERN = re.compile(
    r"\b(auto[- ]?renew|renewal|subscription|membership).{0,24}"
    r"(?:due|end|ending|expire|expiring|renew|trial)|"
    r"\b(?:trial|subscription|membership).{0,24}(?:end|ending|expire|expiring|renew)\b",
    re.I,
)
_SECURITY_CHANGE_PATTERN = re.compile(
    r"\b(account (?:change|changed|recovery|update)|email (?:address )?changed|"
    r"new sign[- ]?in|password (?:change|changed|reset)|recovery (?:email|phone)|"
    r"security alert|two[- ]factor|2fa|mfa|unusual activity)\b",
    re.I,
)


def _message_finding(
    message: dict[str, Any],
    kind: str,
    reason: str,
    confidence: float,
) -> dict[str, Any]:
    message_id = str(message.get("id", ""))
    return {
        "id": f"mail-focus:{kind}:{message_id}",
        "kind": kind,
        "message_id": message_id,
        "title": str(message.get("subject", "Untitled message")),
        "sender": str(message.get("sender_name") or message.get("sender_address", "")),
        "received_at": str(message.get("received_at", "")),
        "reason": reason,
        "confidence": confidence,
    }


def focused_mail_findings(
    messages: list[dict[str, Any]],
    attachments: list[dict[str, Any]],
    security_evidence: list[dict[str, Any]],
    *,
    included_kinds: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Find local review candidates without claiming that an action was forgotten."""
    findings: list[dict[str, Any]] = []
    message_ids = {str(value.get("id", "")) for value in messages}
    message_by_id = {str(value.get("id", "")): value for value in messages}

    def include(kind: str) -> bool:
        return included_kinds is None or kind in included_kinds

    registration_evidence_message_ids = {
        str(message_id)
        for evidence in security_evidence
        if {str(value) for value in evidence.get("categories", [])}.intersection(
            {"registration", "welcome", "verification"}
        )
        for message_id in evidence.get("message_ids", [])
    }
    for message in messages:
        message_id = str(message.get("id", ""))
        text = f"{message.get('subject', '')} {message.get('body_preview', '')}"
        flagged = str(message.get("flag_status", "")).casefold() not in {
            "",
            "notflagged",
            "complete",
        }
        important = str(message.get("importance", "")).casefold() == "high"
        if not message_id:
            continue
        if include("forgotten_task") and (flagged or important or _TASK_PATTERN.search(text)):
            findings.append(
                _message_finding(
                    message,
                    "forgotten_task",
                    "The message is flagged, high importance, or contains task-like wording; "
                    "verify whether it still needs action.",
                    0.82 if flagged else (0.75 if important else 0.62),
                )
            )
        if (
            include("registration_data")
            and message_id not in registration_evidence_message_ids
            and _REGISTRATION_PATTERN.search(text)
        ):
            findings.append(
                _message_finding(
                    message,
                    "registration_data",
                    "The cached sender and redacted preview may document an account registration; "
                    "review it for the account inventory.",
                    0.76,
                )
            )
        if include("invoice_or_receipt") and _INVOICE_PATTERN.search(text):
            findings.append(
                _message_finding(
                    message,
                    "invoice_or_receipt",
                    "Invoice, receipt, order, or payment wording was found in the local mail cache.",
                    0.74,
                )
            )
        if include("renewal_or_subscription") and _RENEWAL_PATTERN.search(text):
            findings.append(
                _message_finding(
                    message,
                    "renewal_or_subscription",
                    "The message may describe a subscription, trial, membership, or renewal date.",
                    0.68,
                )
            )
        if include("security_or_account_change") and _SECURITY_CHANGE_PATTERN.search(text):
            findings.append(
                _message_finding(
                    message,
                    "security_or_account_change",
                    "The message may record a security event or account-detail change; verify it "
                    "against the account inventory.",
                    0.76,
                )
            )
    for evidence in security_evidence:
        categories = {str(value) for value in evidence.get("categories", [])}
        candidates = [
            str(value) for value in evidence.get("message_ids", []) if str(value) in message_ids
        ]
        message_id = candidates[-1] if candidates else ""
        if include("registration_data") and categories.intersection(
            {"registration", "welcome", "verification"}
        ):
            findings.append(
                {
                    "id": f"mail-focus:registration:{evidence.get('id', evidence.get('service_key', ''))}",
                    "kind": "registration_data",
                    "message_id": message_id,
                    "title": f"Registration/account evidence for {evidence.get('display_name', '')}",
                    "sender": str(evidence.get("display_name", "")),
                    "received_at": str(evidence.get("last_evidence_at", "")),
                    "reason": (
                        "Cached sender and subject patterns may document an account registration; "
                        "review it for the account inventory."
                    ),
                    "confidence": 0.72,
                }
            )
        if include("security_or_account_change") and categories.intersection(
            {"security_alert", "password_reset", "mfa"}
        ):
            findings.append(
                {
                    "id": f"mail-focus:security:{evidence.get('id', evidence.get('service_key', ''))}",
                    "kind": "security_or_account_change",
                    "message_id": message_id,
                    "title": f"Security evidence for {evidence.get('display_name', '')}",
                    "sender": str(evidence.get("display_name", "")),
                    "received_at": str(evidence.get("last_evidence_at", "")),
                    "reason": (
                        "Cached sender and subject patterns may document a security or account "
                        "change; verify it against the account inventory."
                    ),
                    "confidence": 0.72,
                }
            )
    for attachment in attachments:
        if attachment.get("is_inline"):
            continue
        attachment_id = str(attachment.get("id", ""))
        message_id = str(attachment.get("message_id", ""))
        if not attachment_id:
            continue
        filename = str(attachment.get("filename", "Attachment"))
        if include("attachment_not_recorded_as_saved"):
            findings.append(
                {
                    "id": f"mail-focus:attachment:{attachment_id}",
                    "kind": "attachment_not_recorded_as_saved",
                    "message_id": message_id,
                    "attachment_id": attachment_id,
                    "title": filename,
                    "received_at": str(attachment.get("received_at", "")),
                    "reason": (
                        "The mailbox cache contains this attachment, but AIOrganizer has no reviewed "
                        "record that it was saved. This is a reminder candidate, not proof it is missing."
                    ),
                    "confidence": 0.55,
                }
            )
        if include("invoice_or_receipt") and _INVOICE_PATTERN.search(filename):
            message = message_by_id.get(message_id, {})
            findings.append(
                {
                    "id": f"mail-focus:invoice-attachment:{attachment_id}",
                    "kind": "invoice_or_receipt",
                    "message_id": message_id,
                    "attachment_id": attachment_id,
                    "title": filename,
                    "sender": str(
                        message.get("sender_name") or message.get("sender_address", "")
                    ),
                    "received_at": str(attachment.get("received_at", "")),
                    "reason": "The attachment filename suggests an invoice or receipt.",
                    "confidence": 0.7,
                }
            )
    return sorted(
        findings,
        key=lambda value: (str(value.get("received_at", "")), str(value["id"])),
        reverse=True,
    )[:1_000]


def permission_review(
    proposals: list[EmailProposal], current_scopes: tuple[str, ...]
) -> PermissionReview:
    requested = sorted({scope for proposal in proposals for scope in proposal.required_scopes})
    current = set(current_scopes)
    actions = tuple(
        {
            EmailProposalKind.FOLDER_CREATE: "Create reviewed mail folders",
            EmailProposalKind.FOLDER_RENAME: "Rename reviewed mail folders",
            EmailProposalKind.FOLDER_MOVE: "Move reviewed mail folders",
            EmailProposalKind.MESSAGE_MOVE: "Move individually accepted messages",
            EmailProposalKind.MESSAGE_CATEGORIZE: "Assign reviewed message categories",
            EmailProposalKind.RULE_CREATE: "Create inspected inbox rules",
        }[kind]
        for kind in dict.fromkeys(proposal.kind for proposal in proposals)
    )
    return PermissionReview(
        tuple(sorted(current)),
        tuple(requested),
        tuple(scope for scope in requested if scope not in current),
        actions,
    )


def _require_keys(payload: dict[str, Any], required: set[str]) -> None:
    missing = required - payload.keys()
    if missing:
        raise ValueError(f"Email proposal is missing: {', '.join(sorted(missing))}")


def _validate_rule(payload: dict[str, Any]) -> None:
    _require_keys(
        payload,
        {"display_name", "conditions", "exceptions", "actions", "priority", "sample_message_ids"},
    )
    actions = payload["actions"]
    if not isinstance(actions, dict) or not actions:
        raise ValueError("A rule requires explicit actions")
    forbidden = {"delete", "permanentDelete", "forwardTo", "redirectTo", "markAsRead"}
    if forbidden.intersection(actions):
        raise ValueError(
            "Deleting, forwarding, redirecting, and hidden broad actions are not allowed"
        )
    allowed = {"moveToFolder", "assignCategories", "stopProcessingRules"}
    if not set(actions).issubset(allowed):
        raise ValueError("Rule contains an unsupported action")
    if not isinstance(payload["sample_message_ids"], list) or not payload["sample_message_ids"]:
        raise ValueError("Rule proposals require a historical sample")
