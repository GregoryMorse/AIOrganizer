from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_organizer.adapters.email import ClassicOutlookClient
from ai_organizer.adapters.persistence import WorkspaceStore
from ai_organizer.application.email_service import EmailService
from ai_organizer.domain.email import EmailConnectorKind


class _Collection:
    def __init__(self, *values: Any) -> None:
        self.values = list(values)
        self.Count = len(values)

    def Item(self, index: int) -> Any:
        return self.values[index - 1]


class _Items(_Collection):
    def Sort(self, _field: str, _descending: bool) -> None:
        return None


class _PropertyAccessor:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}

    def GetProperty(self, name: str) -> str:
        return self.values.get(name, "")


class _Attachment:
    FileName = "receipt.pdf"
    Size = 1234
    Position = 0


class _Message:
    Class = 43
    EntryID = "message-1"
    Subject = "Your receipt"
    SenderName = "Sender"
    SenderEmailAddress = "sender@example.com"
    SenderEmailType = "SMTP"
    ReceivedTime = datetime(2026, 8, 15, 10, 30, tzinfo=UTC)
    LastModificationTime = ReceivedTime
    SentOn = ReceivedTime
    Body = "A bounded local preview."
    ConversationID = "conversation-1"
    Attachments = _Collection(_Attachment())
    UnRead = True
    Categories = "Finance, Receipts"
    To = "person@example.com"
    CC = ""
    Importance = 2
    FlagStatus = 2
    PropertyAccessor = _PropertyAccessor(
        {
            "http://schemas.microsoft.com/mapi/proptag/0x1035001F": "<message@example.com>"
        }
    )


class _Folder:
    def __init__(self, entry_id: str, name: str, items: _Items, *children: Any) -> None:
        self.EntryID = entry_id
        self.Name = name
        self.Items = items
        self.Folders = _Collection(*children)
        self.UnReadItemCount = 1 if entry_id == "inbox" else 0


class _Store:
    StoreID = "store-1"
    DisplayName = "Local Mailbox"

    def __init__(self, root: _Folder, inbox: _Folder) -> None:
        self.root = root
        self.inbox = inbox

    def GetRootFolder(self) -> _Folder:
        return self.root

    def GetDefaultFolder(self, folder_type: int) -> _Folder:
        assert folder_type == 6
        return self.inbox


class _Account:
    SmtpAddress = "person@example.com"
    DisplayName = "Person"

    def __init__(self, store: _Store) -> None:
        self.DeliveryStore = store


class _Namespace:
    def __init__(self, store: _Store) -> None:
        self.Stores = _Collection(store)
        self.Accounts = _Collection(_Account(store))


class _Application:
    def __init__(self, namespace: _Namespace) -> None:
        self.namespace = namespace

    def GetNamespace(self, name: str) -> _Namespace:
        assert name == "MAPI"
        return self.namespace


def _client() -> ClassicOutlookClient:
    second = _Message()
    second.EntryID = "message-2"
    inbox = _Folder("inbox", "Inbox", _Items(_Message(), second))
    root = _Folder("root", "Mailbox", _Items(), inbox)
    namespace = _Namespace(_Store(root, inbox))
    return ClassicOutlookClient(
        dispatch=lambda prog_id: (
            _Application(namespace)
            if prog_id == "Outlook.Application"
            else (_ for _ in ()).throw(AssertionError(prog_id))
        ),
        max_messages_per_folder=1,
    )


def test_classic_outlook_discovery_and_bounded_read_only_sync(tmp_path: Path) -> None:
    client = _client()
    mailboxes = client.discover_mailboxes()

    assert [(value.username, value.display_name) for value in mailboxes] == [
        ("person@example.com", "Person")
    ]
    assert mailboxes[0].account_id.startswith("classic-outlook:")

    store = WorkspaceStore.create(tmp_path / "classic.aioworkspace", "Classic")
    service = EmailService(store, object())  # type: ignore[arg-type]
    account = service.register_classic_outlook_accounts(mailboxes)[0]
    reconnected = service.register_classic_outlook_accounts(mailboxes)[0]
    assert reconnected.id == account.id
    assert service.active_account() is not None
    assert service.active_account().id == account.id
    account = reconnected
    result = service.sync_classic_outlook_read_only(account, client)

    assert account.connector_kind == EmailConnectorKind.CLASSIC_OUTLOOK.value
    assert account.connector_ref == "store-1"
    assert result == {
        "folders": 2,
        "messages": 1,
        "attachments": 1,
        "security_evidence": 1,
        "truncated": True,
    }
    messages = store.list_mail_messages(account.id)
    assert messages[0]["subject"] == "Your receipt"
    assert messages[0]["body_preview"] == "A bounded local preview."
    assert messages[0]["flag_status"] == "flagged"
    assert store.list_mail_attachments(account.id)[0]["filename"] == "receipt.pdf"
    store.close()
