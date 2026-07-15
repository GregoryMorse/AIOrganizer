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
    MESSAGE_MOVE = "message_move"
    MESSAGE_CATEGORIZE = "message_categorize"
    RULE_CREATE = "rule_create"


class EmailProposalStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    BLOCKED = "blocked"
    STALE = "stale"
    COMPLETED = "completed"


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


def permission_review(
    proposals: list[EmailProposal], current_scopes: tuple[str, ...]
) -> PermissionReview:
    requested = sorted({scope for proposal in proposals for scope in proposal.required_scopes})
    current = set(current_scopes)
    actions = tuple(
        {
            EmailProposalKind.FOLDER_CREATE: "Create reviewed mail folders",
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
    _require_keys(payload, {"display_name", "conditions", "exceptions", "actions", "priority", "sample_message_ids"})
    actions = payload["actions"]
    if not isinstance(actions, dict) or not actions:
        raise ValueError("A rule requires explicit actions")
    forbidden = {"delete", "permanentDelete", "forwardTo", "redirectTo", "markAsRead"}
    if forbidden.intersection(actions):
        raise ValueError("Deleting, forwarding, redirecting, and hidden broad actions are not allowed")
    allowed = {"moveToFolder", "assignCategories", "stopProcessingRules"}
    if not set(actions).issubset(allowed):
        raise ValueError("Rule contains an unsupported action")
    if not isinstance(payload["sample_message_ids"], list) or not payload["sample_message_ids"]:
        raise ValueError("Rule proposals require a historical sample")
