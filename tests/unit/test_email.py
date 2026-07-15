from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ai_organizer.adapters.email import (
    GraphClient,
    GraphResponse,
    RemoteConflict,
    UrllibGraphTransport,
)
from ai_organizer.adapters.persistence import WorkspaceStore
from ai_organizer.application.email_service import EmailService
from ai_organizer.domain.email import (
    EmailAccount,
    EmailProposal,
    EmailProposalKind,
    EmailProposalStatus,
    MailAttachmentSnapshot,
    MailFolderSnapshot,
    MailMessageSnapshot,
    sanitized_preview,
)


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.responses: dict[str, GraphResponse] = {}

    def request(
        self,
        method: str,
        url_or_path: str,
        access_token: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> GraphResponse:
        del access_token, headers
        self.calls.append((method, url_or_path, json_body))
        return self.responses[url_or_path]


class FakeGraph:
    def __init__(self) -> None:
        self.move_conflict = False
        self.move_calls = 0

    def list_folders(self, token: str) -> list[MailFolderSnapshot]:
        del token
        return [MailFolderSnapshot("", "inbox", "Inbox", total_item_count=1)]

    def sync_folder_delta(
        self, token: str, folder_id: str, *, delta_link: str = ""
    ) -> tuple[list[MailMessageSnapshot], str]:
        del token, delta_link
        return (
            [
                MailMessageSnapshot(
                    "",
                    "message-1",
                    folder_id,
                    "Security alert: code 123456 https://bad.invalid/reset?token=abc",
                    "Example",
                    "security@example.com",
                    "2026-07-01T10:00:00Z",
                    "Your OTP code: 123456 token=secret-value",
                    has_attachments=True,
                    change_key="change-1",
                    etag='W/"etag-1"',
                )
            ],
            "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken=opaque",
        )

    def list_attachment_metadata(
        self, token: str, message_id: str
    ) -> list[MailAttachmentSnapshot]:
        del token
        return [MailAttachmentSnapshot("", message_id, "attachment-1", "Statement-2026-07.pdf", "application/pdf", 42)]

    def create_folder(self, token: str, parent: str, name: str) -> dict[str, Any]:
        del token, parent, name
        return {"id": "folder-new"}

    def move_message(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        self.move_calls += 1
        if self.move_conflict:
            raise RemoteConflict("changed")
        return {"id": "message-new"}

    def create_rule(self, token: str, rule: dict[str, Any]) -> dict[str, Any]:
        del token, rule
        return {"id": "rule-new"}


def test_preview_redacts_links_codes_and_tokens() -> None:
    value = sanitized_preview("Reset code: 123456 at https://example.invalid/x token=abcdef")
    assert "123456" not in value
    assert "https://" not in value
    assert "abcdef" not in value
    assert "redacted" in value


def test_transport_blocks_delete_send_and_foreign_hosts() -> None:
    transport = UrllibGraphTransport()
    with pytest.raises(ValueError, match="only GET"):
        transport.request("DELETE", "/me/messages/1", "token")
    with pytest.raises(ValueError, match="out of scope"):
        transport.request("POST", "/me/sendMail", "token", json_body={})
    with pytest.raises(ValueError, match="Microsoft Graph"):
        transport.request("GET", "https://example.com/v1.0/me", "token")


def test_graph_delta_follows_opaque_link_and_bounds_preview() -> None:
    transport = FakeTransport()
    initial = "/me/mailFolders/inbox/messages/delta?$select=id,parentFolderId,subject,from,receivedDateTime,bodyPreview,internetMessageId,conversationId,hasAttachments,isRead,changeKey,categories"
    next_link = "https://graph.microsoft.com/v1.0/next?opaque=1"
    transport.responses[initial] = GraphResponse(
        200,
        {
            "value": [{"id": "one", "subject": "One", "bodyPreview": "a" * 1000}],
            "@odata.nextLink": next_link,
        },
        {},
    )
    transport.responses[next_link] = GraphResponse(
        200,
        {
            "value": [{"id": "two", "@removed": {"reason": "deleted"}}],
            "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta?opaque=2",
        },
        {},
    )
    messages, cursor = GraphClient(transport).sync_folder_delta("token", "inbox")
    assert [value.id for value in messages] == ["one", "two"]
    assert len(messages[0].body_preview) == 512
    assert messages[1].removed
    assert cursor.endswith("opaque=2")


def test_read_sync_persists_delta_metadata_and_security_evidence(tmp_path: Path) -> None:
    store = WorkspaceStore.create(tmp_path / "email.aioworkspace", "Email")
    graph = FakeGraph()
    service = EmailService(store, graph)  # type: ignore[arg-type]
    account = EmailAccount("person@example.com", "Person", id="account")
    store.save_email_account(account)

    result = service.sync_read_only(account, "read-token", ("inbox",))

    assert result == {"folders": 1, "messages": 1, "attachments": 1, "security_evidence": 1}
    assert store.mail_delta_token("account", "inbox").endswith("opaque")
    assert store.list_mail_attachments("account")[0]["filename"] == "Statement-2026-07.pdf"
    evidence = store.list_account_security_evidence("account")[0]
    assert evidence["categories"] == ["security_alert"]
    cached_message = store.list_mail_messages("account")[0]
    assert "123456" not in cached_message["subject"]
    assert "123456" not in cached_message["body_preview"]
    assert "secret-value" not in cached_message["body_preview"]
    serialized = store.path.read_bytes()
    assert b"secret-value" not in serialized
    assert b"123456" not in serialized
    store.close()


def test_permission_review_and_conflict_prevent_unreviewed_apply(tmp_path: Path) -> None:
    store = WorkspaceStore.create(tmp_path / "proposal.aioworkspace", "Proposal")
    graph = FakeGraph()
    service = EmailService(store, graph)  # type: ignore[arg-type]
    account = EmailAccount("person@example.com", "Person", id="account")
    store.save_email_account(account)
    proposal = EmailProposal(
        "account",
        EmailProposalKind.MESSAGE_MOVE,
        {"message_id": "message-1", "destination_folder_id": "archive"},
        {"folder_id": "inbox", "change_key": "change-1", "etag": 'W/"one"'},
        "Reviewed message move",
        0.9,
    )
    service.propose(proposal)
    review = service.permission_review({proposal.id})
    assert review.additional_scopes == ("Mail.ReadWrite",)
    assert not review.sending_allowed
    assert not review.permanent_delete_allowed
    with pytest.raises(PermissionError):
        service.apply(
            {proposal.id}, "token", account.granted_scopes, confirmation="APPLY EMAIL CHANGES"
        )
    with pytest.raises(ValueError, match="Type APPLY"):
        service.apply(
            {proposal.id}, "token", (*account.granted_scopes, "Mail.ReadWrite"), confirmation="yes"
        )

    graph.move_conflict = True
    with pytest.raises(RemoteConflict):
        service.apply(
            {proposal.id},
            "token",
            (*account.granted_scopes, "Mail.ReadWrite"),
            confirmation="APPLY EMAIL CHANGES",
        )
    saved = store.list_email_proposals("account")[0]
    assert saved["status"] == EmailProposalStatus.STALE.value
    assert graph.move_calls == 1
    store.close()


def test_only_one_email_account_can_be_active(tmp_path: Path) -> None:
    store = WorkspaceStore.create(tmp_path / "accounts.aioworkspace", "Accounts")
    store.save_email_account(EmailAccount("one@example.com", "One", id="one"))
    store.save_email_account(EmailAccount("two@example.com", "Two", id="two"))
    accounts = store.list_email_accounts()
    assert [value["id"] for value in accounts if value["active"]] == ["two"]
    store.close()


def test_rule_validation_rejects_forward_or_delete() -> None:
    proposal = EmailProposal(
        "account",
        EmailProposalKind.RULE_CREATE,
        {
            "display_name": "Unsafe",
            "conditions": {"senderContains": ["x@example.com"]},
            "exceptions": {},
            "actions": {"forwardTo": [{"emailAddress": {"address": "x@example.com"}}]},
            "priority": 1,
            "sample_message_ids": ["sample"],
        },
        {},
        "Unsafe rule",
        0.5,
    )
    with pytest.raises(ValueError, match="not allowed"):
        proposal.validate()
