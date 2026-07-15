from __future__ import annotations

from contextlib import suppress


class SecretStore:
    SERVICE = "AIOrganizer"

    def set(self, name: str, value: str) -> None:
        import keyring

        keyring.set_password(self.SERVICE, name, value)

    def get(self, name: str) -> str | None:
        try:
            import keyring
        except ImportError:
            return None

        try:
            return keyring.get_password(self.SERVICE, name)
        except keyring.errors.KeyringError:
            return None

    def delete(self, name: str) -> None:
        import keyring

        with suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(self.SERVICE, name)
