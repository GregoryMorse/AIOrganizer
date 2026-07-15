from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from ai_organizer.domain.email import (
    MailAttachmentSnapshot,
    MailFolderSnapshot,
    MailMessageSnapshot,
    sanitized_preview,
)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_BLOCKED_SEGMENTS = {
    "sendmail",
    "send",
    "reply",
    "replyall",
    "forward",
    "createreply",
    "createreplyall",
    "createforward",
    "permanentdelete",
}


@dataclass(frozen=True, slots=True)
class GraphResponse:
    status: int
    payload: dict[str, Any]
    headers: dict[str, str]


class GraphTransport(Protocol):
    def request(
        self,
        method: str,
        url_or_path: str,
        access_token: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> GraphResponse: ...


class RemoteConflict(RuntimeError):
    pass


class UrllibGraphTransport:
    """Narrow Graph transport. It deliberately cannot issue DELETE or mail-send calls."""

    def request(
        self,
        method: str,
        url_or_path: str,
        access_token: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> GraphResponse:
        normalized_method = method.upper()
        if normalized_method not in {"GET", "POST", "PATCH"}:
            raise ValueError("The email connector permits only GET, POST, and PATCH")
        url = _graph_url(url_or_path)
        segments = {segment.casefold() for segment in urlparse(url).path.split("/") if segment}
        if segments.intersection(_BLOCKED_SEGMENTS):
            raise ValueError("Sending, replying, forwarding, and permanent deletion are out of scope")
        request_headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            **(headers or {}),
        }
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=request_headers, method=normalized_method)
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read(4 * 1024 * 1024)
                payload = json.loads(raw) if raw else {}
                return GraphResponse(response.status, payload, dict(response.headers.items()))
        except HTTPError as error:
            raw = error.read(256 * 1024)
            details = raw.decode("utf-8", errors="replace")
            raise RuntimeError(f"Microsoft Graph returned HTTP {error.code}: {details}") from error


class GraphClient:
    def __init__(self, transport: GraphTransport) -> None:
        self.transport = transport

    def profile(self, token: str) -> dict[str, Any]:
        return self._get(token, "/me?$select=id,displayName,userPrincipalName,mail")

    def list_folders(self, token: str, *, max_items: int = 500) -> list[MailFolderSnapshot]:
        path = (
            "/me/mailFolders?includeHiddenFolders=true&"
            "$select=id,displayName,parentFolderId,childFolderCount,totalItemCount,unreadItemCount"
        )
        values, _ = self._paged(token, path, max_items=max_items)
        pending = [str(value["id"]) for value in values if value.get("id") and value.get("childFolderCount")]
        visited = {str(value.get("id", "")) for value in values}
        while pending and len(values) < max_items:
            parent = pending.pop(0)
            children, _ = self._paged(
                token,
                f"/me/mailFolders/{quote(parent, safe='')}/childFolders?"
                "$select=id,displayName,parentFolderId,childFolderCount,totalItemCount,unreadItemCount",
                max_items=max_items - len(values),
            )
            for child in children:
                child_id = str(child.get("id", ""))
                if not child_id or child_id in visited:
                    continue
                visited.add(child_id)
                values.append(child)
                if child.get("childFolderCount"):
                    pending.append(child_id)
        return [
            MailFolderSnapshot(
                "",
                str(value.get("id", "")),
                sanitized_preview(str(value.get("displayName", "")), 180),
                str(value.get("parentFolderId", "")),
                int(value.get("childFolderCount", 0)),
                int(value.get("totalItemCount", 0)),
                int(value.get("unreadItemCount", 0)),
                str(value.get("@odata.etag", "")),
            )
            for value in values
            if value.get("id")
        ]

    def sync_folder_delta(
        self,
        token: str,
        folder_id: str,
        *,
        delta_link: str = "",
        max_items: int = 5_000,
    ) -> tuple[list[MailMessageSnapshot], str]:
        path = delta_link or (
            f"/me/mailFolders/{quote(folder_id, safe='')}/messages/delta?"
            "$select=id,parentFolderId,subject,from,receivedDateTime,bodyPreview,"
            "internetMessageId,conversationId,hasAttachments,isRead,changeKey,categories"
        )
        values, final_delta = self._paged(
            token,
            path,
            max_items=max_items,
            headers={"Prefer": 'outlook.body-content-type="text",odata.maxpagesize=100'},
        )
        messages = []
        for value in values:
            sender = value.get("from", {}).get("emailAddress", {})
            messages.append(
                MailMessageSnapshot(
                    "",
                    str(value.get("id", "")),
                    str(value.get("parentFolderId", folder_id)),
                    sanitized_preview(str(value.get("subject", "")), 300),
                    sanitized_preview(str(sender.get("name", "")), 180),
                    str(sender.get("address", ""))[:320],
                    str(value.get("receivedDateTime", "")),
                    sanitized_preview(str(value.get("bodyPreview", ""))),
                    str(value.get("internetMessageId", ""))[:1000],
                    str(value.get("conversationId", "")),
                    bool(value.get("hasAttachments", False)),
                    bool(value.get("isRead", False)),
                    str(value.get("changeKey", "")),
                    str(value.get("@odata.etag", "")),
                    tuple(str(item)[:255] for item in value.get("categories", [])),
                    "@removed" in value,
                )
            )
        return messages, final_delta

    def list_attachment_metadata(
        self,
        token: str,
        message_id: str,
        *,
        max_items: int = 100,
    ) -> list[MailAttachmentSnapshot]:
        path = (
            f"/me/messages/{quote(message_id, safe='')}/attachments?"
            "$select=id,name,contentType,size,isInline"
        )
        values, _ = self._paged(token, path, max_items=max_items)
        return [
            MailAttachmentSnapshot(
                "",
                message_id,
                str(value.get("id", "")),
                sanitized_preview(str(value.get("name", "")), 255),
                str(value.get("contentType", ""))[:255],
                int(value.get("size", 0)),
                bool(value.get("isInline", False)),
            )
            for value in values
            if value.get("id")
        ]

    def create_folder(
        self, token: str, parent_folder_id: str, display_name: str
    ) -> dict[str, Any]:
        existing, _ = self._paged(
            token,
            f"/me/mailFolders/{quote(parent_folder_id, safe='')}/childFolders?"
            "$select=id,displayName",
            max_items=500,
        )
        if any(str(value.get("displayName", "")).casefold() == display_name.casefold() for value in existing):
            raise RemoteConflict("A folder with this name now exists under the proposed parent")
        return self._post(
            token,
            f"/me/mailFolders/{quote(parent_folder_id, safe='')}/childFolders",
            {"displayName": display_name},
        )

    def move_message(
        self,
        token: str,
        message_id: str,
        destination_folder_id: str,
        *,
        expected_folder_id: str,
        expected_change_key: str,
        expected_etag: str = "",
    ) -> dict[str, Any]:
        current = self._get(
            token,
            f"/me/messages/{quote(message_id, safe='')}?"
            "$select=id,parentFolderId,changeKey",
        )
        if str(current.get("parentFolderId", "")) != expected_folder_id:
            raise RemoteConflict("Message moved since the proposal was created")
        if str(current.get("changeKey", "")) != expected_change_key:
            raise RemoteConflict("Message changed since the proposal was created")
        headers = {"If-Match": expected_etag} if expected_etag else None
        return self._post(
            token,
            f"/me/messages/{quote(message_id, safe='')}/move",
            {"destinationId": destination_folder_id},
            headers=headers,
        )

    def create_rule(self, token: str, rule: dict[str, Any]) -> dict[str, Any]:
        existing, _ = self._paged(
            token,
            "/me/mailFolders/inbox/messageRules?$select=id,displayName",
            max_items=500,
        )
        display_name = str(rule.get("displayName", ""))
        if any(
            str(value.get("displayName", "")).casefold() == display_name.casefold()
            for value in existing
        ):
            raise RemoteConflict("An inbox rule with this display name now exists")
        return self._post(token, "/me/mailFolders/inbox/messageRules", rule)

    def assign_categories(
        self,
        token: str,
        message_id: str,
        categories: list[str],
        *,
        expected_folder_id: str,
        expected_change_key: str,
        expected_etag: str = "",
    ) -> dict[str, Any]:
        current = self._get(
            token,
            f"/me/messages/{quote(message_id, safe='')}?$select=id,parentFolderId,changeKey",
        )
        if str(current.get("parentFolderId", "")) != expected_folder_id:
            raise RemoteConflict("Message moved since the proposal was created")
        if str(current.get("changeKey", "")) != expected_change_key:
            raise RemoteConflict("Message changed since the proposal was created")
        headers = {"If-Match": expected_etag} if expected_etag else None
        response = self.transport.request(
            "PATCH",
            f"/me/messages/{quote(message_id, safe='')}",
            token,
            json_body={"categories": categories},
            headers=headers,
        )
        return response.payload

    def _get(self, token: str, path: str) -> dict[str, Any]:
        response = self.transport.request("GET", path, token)
        return response.payload

    def _post(
        self,
        token: str,
        path: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = self.transport.request("POST", path, token, json_body=payload, headers=headers)
        return response.payload

    def _paged(
        self,
        token: str,
        path: str,
        *,
        max_items: int,
        headers: dict[str, str] | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        values: list[dict[str, Any]] = []
        next_path = path
        delta_link = ""
        pages = 0
        while next_path and len(values) < max_items:
            pages += 1
            if pages > 200:
                raise RuntimeError("Microsoft Graph pagination exceeded its safety bound")
            response = self.transport.request("GET", next_path, token, headers=headers)
            page_values = response.payload.get("value", [])
            if not isinstance(page_values, list):
                raise RuntimeError("Microsoft Graph returned an invalid collection")
            values.extend(value for value in page_values if isinstance(value, dict))
            delta_link = str(response.payload.get("@odata.deltaLink", delta_link))
            next_path = str(response.payload.get("@odata.nextLink", ""))
        return values[:max_items], delta_link


def _graph_url(url_or_path: str) -> str:
    if url_or_path.startswith("/"):
        return f"{GRAPH_BASE}{url_or_path}"
    parsed = urlparse(url_or_path)
    if parsed.scheme != "https" or parsed.hostname != "graph.microsoft.com":
        raise ValueError("Only HTTPS Microsoft Graph URLs may be followed")
    if not parsed.path.startswith("/v1.0/"):
        raise ValueError("Unexpected Microsoft Graph API path")
    return url_or_path
