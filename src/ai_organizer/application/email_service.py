from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Any

from ai_organizer.adapters.email import GraphClient, RemoteConflict
from ai_organizer.adapters.persistence import WorkspaceStore
from ai_organizer.domain.email import (
    AccountSecurityEvidence,
    EmailAccount,
    EmailProposal,
    EmailProposalKind,
    EmailProposalStatus,
    PermissionReview,
    permission_review,
    sanitized_preview,
    security_categories,
)
from ai_organizer.domain.models import utc_now
from ai_organizer.domain.recurrence import (
    AttachmentMatch,
    AttachmentMatcher,
    AttachmentMetadata,
    RecurrenceSeries,
)


class EmailService:
    """Orchestrates the read cache and narrowly typed, user-approved Graph writes."""

    def __init__(self, store: WorkspaceStore, graph: GraphClient) -> None:
        self.store = store
        self.graph = graph

    def register_active_account(
        self,
        profile: dict[str, Any],
        *,
        granted_scopes: tuple[str, ...],
        home_account_id: str = "",
        tenant_id: str = "",
    ) -> EmailAccount:
        username = str(profile.get("mail") or profile.get("userPrincipalName") or "")
        if not username:
            raise RuntimeError("Microsoft Graph did not return a mailbox identity")
        account = EmailAccount(
            username,
            str(profile.get("displayName") or username),
            tenant_id,
            home_account_id,
            tuple(sorted(set(granted_scopes))),
            True,
            str(profile.get("id") or "") or EmailAccount(username, username).id,
        )
        self.store.save_email_account(account)
        self.store.activity("email.account", f"Activated delegated mailbox {username}")
        return account

    def sync_read_only(
        self,
        account: EmailAccount,
        token: str,
        folder_ids: tuple[str, ...] = (),
        *,
        include_attachment_metadata: bool = True,
    ) -> dict[str, int]:
        folders = [
            replace(folder, account_id=account.id) for folder in self.graph.list_folders(token)
        ]
        self.store.save_mail_folders(folders)
        selected = folder_ids or tuple(folder.id for folder in folders)
        messages_seen = 0
        attachments_seen = 0
        for folder_id in selected:
            cursor = self.store.mail_delta_token(account.id, folder_id)
            messages, next_cursor = self.graph.sync_folder_delta(
                token, folder_id, delta_link=cursor
            )
            messages = [
                replace(
                    message,
                    account_id=account.id,
                    subject=sanitized_preview(message.subject, 300),
                    sender_name=sanitized_preview(message.sender_name, 180),
                    body_preview=sanitized_preview(message.body_preview),
                )
                for message in messages
            ]
            self.store.apply_mail_delta(messages)
            self.store.save_mail_delta_token(account.id, folder_id, next_cursor)
            messages_seen += len(messages)
            if include_attachment_metadata:
                for message in messages:
                    if message.removed or not message.has_attachments:
                        continue
                    metadata = self.graph.list_attachment_metadata(token, message.id)
                    attachments = [
                        replace(
                            attachment,
                            account_id=account.id,
                            filename=sanitized_preview(attachment.filename, 255),
                            received_at=message.received_at,
                            sanitized_subject=message.subject,
                        )
                        for attachment in metadata
                    ]
                    self.store.save_mail_attachments(attachments)
                    attachments_seen += len(attachments)
        evidence = self._account_evidence(account)
        self.store.save_account_security_evidence(account.id, evidence)
        self.store.activity(
            "email.sync",
            f"Read-only mailbox refresh recorded {messages_seen} delta item(s)",
            {"folders": len(selected), "attachments": attachments_seen},
        )
        return {
            "folders": len(folders),
            "messages": messages_seen,
            "attachments": attachments_seen,
            "security_evidence": len(evidence),
        }

    def propose(self, proposal: EmailProposal) -> EmailProposal:
        active = self.active_account()
        if active is None or active.id != proposal.account_id:
            raise ValueError("Email proposals must target the one active account")
        proposal.validate()
        if proposal.kind == EmailProposalKind.RULE_CREATE:
            known_messages = {
                str(value["id"]) for value in self.store.list_mail_messages(active.id)
            }
            samples = {str(value) for value in proposal.payload["sample_message_ids"]}
            if not samples.issubset(known_messages):
                raise ValueError("Every rule sample must reference cached historical mail")
        self.store.save_email_proposal(proposal)
        self.store.activity("email.proposal", f"Staged {proposal.kind.value} proposal")
        return proposal

    def permission_review(self, proposal_ids: set[str]) -> PermissionReview:
        account = self.active_account()
        if account is None:
            raise RuntimeError("No active email account")
        proposals = [
            _proposal_from_payload(value)
            for value in self.store.list_email_proposals(account.id)
            if value["id"] in proposal_ids
        ]
        if not proposals:
            raise ValueError("Select at least one email proposal")
        return permission_review(proposals, account.granted_scopes)

    def apply(
        self,
        proposal_ids: set[str],
        token: str,
        granted_scopes: tuple[str, ...],
        *,
        confirmation: str,
    ) -> list[dict[str, str]]:
        if confirmation != "APPLY EMAIL CHANGES":
            raise ValueError("Type APPLY EMAIL CHANGES to apply reviewed email proposals")
        account = self.active_account()
        if account is None:
            raise RuntimeError("No active email account")
        payloads = self.store.list_email_proposals(account.id)
        proposals = [
            _proposal_from_payload(value) for value in payloads if value["id"] in proposal_ids
        ]
        if {proposal.id for proposal in proposals} != proposal_ids:
            raise ValueError("One or more selected email proposals no longer exist")
        folder_targets = [
            str(proposal.payload["folder_id"])
            for proposal in proposals
            if proposal.kind in {EmailProposalKind.FOLDER_RENAME, EmailProposalKind.FOLDER_MOVE}
        ]
        if len(folder_targets) != len(set(folder_targets)):
            raise ValueError(
                "Apply at most one rename or move proposal per mail folder in each batch"
            )
        review = permission_review(proposals, tuple(granted_scopes))
        if review.additional_scopes:
            raise PermissionError(
                "Additional delegated consent is required: " + ", ".join(review.additional_scopes)
            )
        results: list[dict[str, str]] = []
        for proposal in proposals:
            proposal.validate()
            if proposal.status not in {EmailProposalStatus.ACCEPTED, EmailProposalStatus.PROPOSED}:
                raise ValueError(
                    f"Proposal {proposal.id} is not applicable in state {proposal.status}"
                )
            try:
                result = self._apply_one(token, proposal)
            except RemoteConflict:
                proposal.status = EmailProposalStatus.STALE
                proposal.revision += 1
                proposal.updated_at = utc_now()
                self.store.save_email_proposal(proposal)
                self.store.activity(
                    "email.conflict", f"Remote state changed for proposal {proposal.id}"
                )
                raise
            proposal.status = EmailProposalStatus.COMPLETED
            proposal.revision += 1
            proposal.updated_at = utc_now()
            self.store.save_email_proposal(proposal)
            results.append({"proposal_id": proposal.id, "remote_id": str(result.get("id", ""))})
        self.store.activity("email.apply", f"Applied {len(results)} reviewed email proposal(s)")
        return results

    def active_account(self) -> EmailAccount | None:
        payload = next(
            (value for value in self.store.list_email_accounts() if value["active"]), None
        )
        return _account_from_payload(payload) if payload else None

    def recurring_attachment_matches(
        self, account_id: str, series: RecurrenceSeries, missing_periods: set[str]
    ) -> list[AttachmentMatch]:
        matcher = AttachmentMatcher()
        matches = []
        for value in self.store.list_mail_attachments(account_id):
            metadata = AttachmentMetadata(
                account_id,
                str(value["message_id"]),
                str(value["id"]),
                str(value["filename"]),
                str(value["mime_type"]),
                int(value["size"]),
                str(value.get("received_at", "")),
                str(value.get("sanitized_subject", "")),
            )
            match = matcher.match(metadata, series, missing_periods)
            if match:
                matches.append(match)
        return matches

    def _apply_one(self, token: str, proposal: EmailProposal) -> dict[str, Any]:
        if proposal.kind == EmailProposalKind.FOLDER_CREATE:
            return self.graph.create_folder(
                token,
                str(proposal.payload["parent_folder_id"]),
                str(proposal.payload["display_name"]),
            )
        if proposal.kind == EmailProposalKind.FOLDER_RENAME:
            return self.graph.rename_folder(
                token,
                str(proposal.payload["folder_id"]),
                str(proposal.payload["display_name"]),
                expected_display_name=str(proposal.expected_remote["display_name"]),
                expected_parent_folder_id=str(proposal.expected_remote["parent_folder_id"]),
                expected_etag=str(proposal.expected_remote.get("etag", "")),
            )
        if proposal.kind == EmailProposalKind.FOLDER_MOVE:
            return self.graph.move_folder(
                token,
                str(proposal.payload["folder_id"]),
                str(proposal.payload["destination_folder_id"]),
                expected_display_name=str(proposal.expected_remote["display_name"]),
                expected_parent_folder_id=str(proposal.expected_remote["parent_folder_id"]),
                expected_etag=str(proposal.expected_remote.get("etag", "")),
            )
        if proposal.kind == EmailProposalKind.MESSAGE_MOVE:
            return self.graph.move_message(
                token,
                str(proposal.payload["message_id"]),
                str(proposal.payload["destination_folder_id"]),
                expected_folder_id=str(proposal.expected_remote["folder_id"]),
                expected_change_key=str(proposal.expected_remote["change_key"]),
                expected_etag=str(proposal.expected_remote.get("etag", "")),
            )
        if proposal.kind == EmailProposalKind.MESSAGE_CATEGORIZE:
            return self.graph.assign_categories(
                token,
                str(proposal.payload["message_id"]),
                [str(value) for value in proposal.payload["categories"]],
                expected_folder_id=str(proposal.expected_remote["folder_id"]),
                expected_change_key=str(proposal.expected_remote["change_key"]),
                expected_etag=str(proposal.expected_remote.get("etag", "")),
            )
        if proposal.kind == EmailProposalKind.RULE_CREATE:
            return self.graph.create_rule(token, _graph_rule(proposal.payload))
        raise ValueError("Unsupported email operation")

    def _account_evidence(self, account: EmailAccount) -> list[AccountSecurityEvidence]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for message in self.store.list_mail_messages(account.id):
            categories = security_categories(
                str(message.get("subject", "")), str(message.get("sender_name", ""))
            )
            if not categories:
                continue
            sender = str(message.get("sender_address", ""))
            domain = sender.rsplit("@", 1)[-1].casefold() if "@" in sender else sender.casefold()
            key = domain or str(message.get("sender_name", "unknown")).casefold()
            groups[key].append({**message, "security_categories": categories})
        results = []
        for key, messages in groups.items():
            ordered = sorted(messages, key=lambda value: str(value.get("received_at", "")))
            categories = tuple(
                sorted({item for message in messages for item in message["security_categories"]})
            )
            results.append(
                AccountSecurityEvidence(
                    account.id,
                    key,
                    key,
                    account.username,
                    str(ordered[0].get("received_at", "")),
                    str(ordered[-1].get("received_at", "")),
                    categories,
                    tuple(str(value["id"]) for value in ordered[-25:]),
                    "Sender identity and sanitized subject patterns indicate account or security activity.",
                )
            )
        return results


def _graph_rule(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "displayName": payload["display_name"],
        "sequence": int(payload["priority"]),
        "isEnabled": True,
        "conditions": payload["conditions"],
        "exceptions": payload["exceptions"],
        "actions": payload["actions"],
    }


def _account_from_payload(value: dict[str, Any]) -> EmailAccount:
    return EmailAccount(
        str(value["username"]),
        str(value["display_name"]),
        str(value.get("tenant_id", "")),
        str(value.get("home_account_id", "")),
        tuple(value.get("granted_scopes", ())),
        bool(value.get("active", False)),
        str(value["id"]),
        int(value.get("revision", 1)),
        str(value.get("updated_at", utc_now())),
    )


def _proposal_from_payload(value: dict[str, Any]) -> EmailProposal:
    return EmailProposal(
        str(value["account_id"]),
        EmailProposalKind(value["kind"]),
        dict(value["payload"]),
        dict(value.get("expected_remote", {})),
        str(value["rationale"]),
        float(value["confidence"]),
        EmailProposalStatus(value.get("status", "proposed")),
        str(value["id"]),
        int(value.get("revision", 1)),
        str(value.get("created_at", utc_now())),
        str(value.get("updated_at", utc_now())),
    )
