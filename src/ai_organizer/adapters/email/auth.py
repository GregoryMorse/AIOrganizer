from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from ai_organizer.adapters.secrets import SecretStore


@dataclass(frozen=True, slots=True)
class DeviceFlowPrompt:
    message: str
    user_code: str
    verification_uri: str
    flow: dict[str, Any]


class MsalDeviceAuth:
    """Delegated device authentication with the serialized token cache in the OS keyring."""

    def __init__(
        self,
        client_id: str | None = None,
        *,
        authority: str | None = None,
        secrets: SecretStore | None = None,
    ) -> None:
        self.client_id = client_id or os.getenv("AIORGANIZER_GRAPH_CLIENT_ID", "")
        if not self.client_id:
            raise RuntimeError("Set AIORGANIZER_GRAPH_CLIENT_ID before signing in to Outlook")
        self.authority = authority or os.getenv(
            "AIORGANIZER_GRAPH_AUTHORITY", "https://login.microsoftonline.com/common"
        )
        self.secrets = secrets or SecretStore()
        try:
            import msal
        except ImportError as error:
            raise RuntimeError(
                "Install the optional email dependencies: uv sync --extra email"
            ) from error
        self._msal = msal
        self._cache = msal.SerializableTokenCache()
        cached = self._load_cache()
        if cached:
            self._cache.deserialize(cached)
        self._app = msal.PublicClientApplication(
            self.client_id,
            authority=self.authority,
            token_cache=self._cache,
        )

    @property
    def _cache_name(self) -> str:
        return f"graph_token_cache:{self.client_id}"

    def _load_cache(self) -> str | None:
        count_value = self.secrets.get(f"{self._cache_name}:chunks")
        if count_value and count_value.isdigit():
            parts = [
                self.secrets.get(f"{self._cache_name}:{index}") for index in range(int(count_value))
            ]
            if all(part is not None for part in parts):
                return "".join(str(part) for part in parts)
        return self.secrets.get(self._cache_name)

    def accounts(self) -> list[dict[str, Any]]:
        return list(self._app.get_accounts())

    def acquire_silent(self, scopes: tuple[str, ...], home_account_id: str = "") -> str | None:
        accounts = self.accounts()
        account = next(
            (value for value in accounts if value.get("home_account_id") == home_account_id),
            accounts[0] if len(accounts) == 1 else None,
        )
        if account is None:
            return None
        result = self._app.acquire_token_silent(list(scopes), account=account)
        self._persist()
        return str(result["access_token"]) if result and "access_token" in result else None

    def begin_device_flow(self, scopes: tuple[str, ...]) -> DeviceFlowPrompt:
        flow = self._app.initiate_device_flow(scopes=list(scopes))
        if "user_code" not in flow:
            raise RuntimeError(str(flow.get("error_description", "Could not start device sign-in")))
        return DeviceFlowPrompt(
            str(flow.get("message", "Open the Microsoft sign-in page and enter the code.")),
            str(flow["user_code"]),
            str(flow.get("verification_uri", flow.get("verification_url", ""))),
            flow,
        )

    def complete_device_flow(self, prompt: DeviceFlowPrompt) -> dict[str, Any]:
        result = self._app.acquire_token_by_device_flow(prompt.flow)
        self._persist()
        if "access_token" not in result:
            raise RuntimeError(str(result.get("error_description", "Microsoft sign-in failed")))
        return dict(result)

    def _persist(self) -> None:
        if self._cache.has_state_changed:
            serialized = self._cache.serialize()
            # Windows Credential Manager has a small per-secret payload limit. Keep the
            # serialized cache in independently protected chunks rather than falling back
            # to an unencrypted workspace or dotfile.
            chunks = [serialized[index : index + 1800] for index in range(0, len(serialized), 1800)]
            old_count_value = self.secrets.get(f"{self._cache_name}:chunks")
            old_count = int(old_count_value) if old_count_value and old_count_value.isdigit() else 0
            for index, chunk in enumerate(chunks):
                self.secrets.set(f"{self._cache_name}:{index}", chunk)
            self.secrets.set(f"{self._cache_name}:chunks", str(len(chunks)))
            for index in range(len(chunks), old_count):
                self.secrets.delete(f"{self._cache_name}:{index}")
            self.secrets.delete(self._cache_name)
