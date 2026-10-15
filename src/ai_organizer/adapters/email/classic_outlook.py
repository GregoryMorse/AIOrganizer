from __future__ import annotations

import hashlib
import platform
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import Any

from ai_organizer.domain.email import (
    MailAttachmentSnapshot,
    MailFolderSnapshot,
    MailMessageSnapshot,
)

_INBOX_FOLDER = 6
_MAIL_ITEM_CLASS = 43
_PR_INTERNET_MESSAGE_ID = "http://schemas.microsoft.com/mapi/proptag/0x1035001F"


@dataclass(frozen=True, slots=True)
class ClassicOutlookMailbox:
    store_id: str
    username: str
    display_name: str

    @property
    def account_id(self) -> str:
        digest = hashlib.sha256(self.store_id.encode("utf-8", errors="replace")).hexdigest()[:24]
        return f"classic-outlook:{digest}"


@dataclass(frozen=True, slots=True)
class ClassicOutlookSnapshot:
    folders: tuple[MailFolderSnapshot, ...]
    messages: tuple[MailMessageSnapshot, ...]
    attachments: tuple[MailAttachmentSnapshot, ...]
    truncated: bool = False


class ClassicOutlookClient:
    """Read-only access to mail stores in the current Classic Outlook profile."""

    def __init__(
        self,
        *,
        dispatch: Callable[[str], Any] | None = None,
        co_initialize: Callable[[], None] | None = None,
        co_uninitialize: Callable[[], None] | None = None,
        max_messages_per_folder: int = 500,
        max_messages_total: int = 5_000,
    ) -> None:
        if max_messages_per_folder < 1 or max_messages_total < 1:
            raise ValueError("Classic Outlook message bounds must be positive")
        self._dispatch = dispatch
        self._co_initialize = co_initialize
        self._co_uninitialize = co_uninitialize
        self.max_messages_per_folder = max_messages_per_folder
        self.max_messages_total = max_messages_total

    @staticmethod
    def available() -> bool:
        if platform.system() != "Windows":
            return False
        try:
            import win32com.client  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            return False
        return True

    def discover_mailboxes(self) -> list[ClassicOutlookMailbox]:
        with self._mapi_session() as namespace:
            accounts_by_store: dict[str, tuple[str, str]] = {}
            for account in _collection(namespace.Accounts):
                store_id = _text(_property(_property(account, "DeliveryStore"), "StoreID"))
                if not store_id:
                    continue
                username = _text(_property(account, "SmtpAddress"))
                display_name = _text(_property(account, "DisplayName")) or username
                accounts_by_store[store_id] = (username, display_name)

            mailboxes: list[ClassicOutlookMailbox] = []
            for store in _collection(namespace.Stores):
                store_id = _text(_property(store, "StoreID"))
                if not store_id or not _has_inbox(store):
                    continue
                store_name = _text(_property(store, "DisplayName")) or "Outlook mailbox"
                username, account_name = accounts_by_store.get(store_id, ("", ""))
                mailboxes.append(
                    ClassicOutlookMailbox(
                        store_id,
                        username or store_name,
                        account_name or store_name,
                    )
                )
            return sorted(
                mailboxes,
                key=lambda value: (value.display_name.casefold(), value.username.casefold()),
            )

    def snapshot(
        self,
        account_id: str,
        store_id: str,
        folder_ids: tuple[str, ...] = (),
    ) -> ClassicOutlookSnapshot:
        if not account_id or not store_id:
            raise ValueError("A Classic Outlook account and store reference are required")
        with self._mapi_session() as namespace:
            store = _find_store(namespace, store_id)
            root = store.GetRootFolder()
            folders = list(_walk_folders(root, account_id))
            folder_objects = {snapshot.id: folder for snapshot, folder in folders}
            snapshots = tuple(snapshot for snapshot, _folder in folders)
            if folder_ids:
                selected_ids = [value for value in folder_ids if value in folder_objects]
            else:
                inbox_id = _text(_property(store.GetDefaultFolder(_INBOX_FOLDER), "EntryID"))
                selected_ids = [inbox_id] if inbox_id in folder_objects else []

            messages: list[MailMessageSnapshot] = []
            attachments: list[MailAttachmentSnapshot] = []
            truncated = False
            for folder_id in selected_ids:
                remaining = self.max_messages_total - len(messages)
                if remaining <= 0:
                    truncated = True
                    break
                folder_messages, folder_attachments, folder_truncated = _read_folder(
                    folder_objects[folder_id],
                    account_id,
                    folder_id,
                    min(self.max_messages_per_folder, remaining),
                )
                messages.extend(folder_messages)
                attachments.extend(folder_attachments)
                truncated = truncated or folder_truncated
            return ClassicOutlookSnapshot(
                snapshots,
                tuple(messages),
                tuple(attachments),
                truncated,
            )

    @contextmanager
    def _mapi_session(self) -> Iterator[Any]:
        dispatch = self._dispatch
        co_initialize = self._co_initialize
        co_uninitialize = self._co_uninitialize
        if dispatch is None:
            if platform.system() != "Windows":
                raise RuntimeError("Classic Outlook integration is available only on Windows")
            try:
                import pythoncom  # type: ignore[import-not-found]
                import win32com.client  # type: ignore[import-not-found]
            except ImportError as error:
                raise RuntimeError(
                    "Classic Outlook support is not installed. Install AIOrganizer's email extra."
                ) from error
            dispatch = win32com.client.Dispatch
            co_initialize = pythoncom.CoInitialize
            co_uninitialize = pythoncom.CoUninitialize
        if co_initialize:
            co_initialize()
        try:
            try:
                application = dispatch("Outlook.Application")
                namespace = application.GetNamespace("MAPI")
            except Exception as error:
                raise RuntimeError(
                    "Classic Outlook could not be opened. Confirm Outlook (classic) is installed "
                    "and that its mail profile opens normally."
                ) from error
            yield namespace
        finally:
            if co_uninitialize:
                co_uninitialize()


def _collection(value: Any) -> Iterator[Any]:
    count = int(_property(value, "Count", 0) or 0)
    for index in range(1, count + 1):
        yield value.Item(index)


def _find_store(namespace: Any, store_id: str) -> Any:
    for store in _collection(namespace.Stores):
        if _text(_property(store, "StoreID")) == store_id:
            return store
    raise RuntimeError("The selected Classic Outlook mailbox is no longer in the current profile")


def _has_inbox(store: Any) -> bool:
    try:
        return bool(_property(store.GetDefaultFolder(_INBOX_FOLDER), "EntryID"))
    except Exception:
        return False


def _walk_folders(
    root: Any,
    account_id: str,
    *,
    parent_id: str = "",
    depth: int = 0,
) -> Iterator[tuple[MailFolderSnapshot, Any]]:
    if depth > 64:
        return
    folder_id = _text(_property(root, "EntryID"))
    if not folder_id:
        return
    children = _property(root, "Folders")
    child_count = int(_property(children, "Count", 0) or 0)
    items = _property(root, "Items")
    snapshot = MailFolderSnapshot(
        account_id,
        folder_id,
        _text(_property(root, "Name")) or "Outlook folder",
        parent_id,
        child_count,
        int(_property(items, "Count", 0) or 0),
        int(_property(root, "UnReadItemCount", 0) or 0),
    )
    yield snapshot, root
    if children is None:
        return
    for child in _collection(children):
        yield from _walk_folders(child, account_id, parent_id=folder_id, depth=depth + 1)


def _read_folder(
    folder: Any,
    account_id: str,
    folder_id: str,
    limit: int,
) -> tuple[list[MailMessageSnapshot], list[MailAttachmentSnapshot], bool]:
    items = folder.Items
    with suppress(Exception):
        items.Sort("[ReceivedTime]", True)
    count = int(_property(items, "Count", 0) or 0)
    messages: list[MailMessageSnapshot] = []
    attachments: list[MailAttachmentSnapshot] = []
    scanned = 0
    scan_limit = min(count, max(limit * 4, limit + 100))
    for index in range(1, scan_limit + 1):
        if len(messages) >= limit:
            break
        scanned += 1
        try:
            item = items.Item(index)
            if int(_property(item, "Class", 0) or 0) != _MAIL_ITEM_CLASS:
                continue
            entry_id = _text(_property(item, "EntryID"))
            if not entry_id:
                continue
            received_at = _iso(_property(item, "ReceivedTime"))
            message = MailMessageSnapshot(
                account_id,
                entry_id,
                folder_id,
                _text(_property(item, "Subject")),
                _text(_property(item, "SenderName")),
                _sender_address(item),
                received_at,
                _text(_property(item, "Body"))[:8_000],
                _mapi_property(item, _PR_INTERNET_MESSAGE_ID),
                _text(_property(item, "ConversationID")),
                int(_property(_property(item, "Attachments"), "Count", 0) or 0) > 0,
                not bool(_property(item, "UnRead", False)),
                _iso(_property(item, "LastModificationTime")),
                "",
                _categories(_text(_property(item, "Categories"))),
                False,
                to_recipients=_recipients(_text(_property(item, "To"))),
                cc_recipients=_recipients(_text(_property(item, "CC"))),
                sent_at=_iso(_property(item, "SentOn")),
                importance={0: "low", 2: "high"}.get(
                    int(_property(item, "Importance", 1) or 1), "normal"
                ),
                flag_status={1: "complete", 2: "flagged"}.get(
                    int(_property(item, "FlagStatus", 0) or 0), "notFlagged"
                ),
            )
            messages.append(message)
            attachments.extend(_attachment_snapshots(item, message))
        except Exception:
            continue
    return messages, attachments, scanned < count


def _attachment_snapshots(
    item: Any, message: MailMessageSnapshot
) -> list[MailAttachmentSnapshot]:
    result: list[MailAttachmentSnapshot] = []
    values = _property(item, "Attachments")
    for index, attachment in enumerate(_collection(values), start=1):
        filename = _text(_property(attachment, "FileName")) or f"Attachment {index}"
        result.append(
            MailAttachmentSnapshot(
                message.account_id,
                message.id,
                f"{message.id}:attachment:{index}",
                filename,
                "application/octet-stream",
                int(_property(attachment, "Size", 0) or 0),
                int(_property(attachment, "Position", 0) or 0) > 0,
                message.received_at,
                message.subject,
            )
        )
    return result


def _sender_address(item: Any) -> str:
    address = _text(_property(item, "SenderEmailAddress"))
    if _text(_property(item, "SenderEmailType")).casefold() != "ex":
        return address
    try:
        exchange_user = item.Sender.GetExchangeUser()
        return _text(_property(exchange_user, "PrimarySmtpAddress")) or address
    except Exception:
        return address


def _mapi_property(item: Any, name: str) -> str:
    try:
        return _text(item.PropertyAccessor.GetProperty(name))
    except Exception:
        return ""


def _property(value: Any, name: str, default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return getattr(value, name)
    except Exception:
        return default


def _text(value: Any) -> str:
    return str(value or "").strip()


def _iso(value: Any) -> str:
    try:
        return str(value.isoformat())
    except (AttributeError, ValueError):
        return _text(value)


def _categories(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.replace(";", ",").split(",") if part.strip())


def _recipients(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(";") if part.strip())
