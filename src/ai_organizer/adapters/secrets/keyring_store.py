from __future__ import annotations

from contextlib import suppress


class SecretStore:
    SERVICE = "AIOrganizer"

    def set(self, name: str, value: str) -> None:
        import keyring

        keyring.set_password(self.SERVICE, name, value)

    def get(self, name: str) -> str | None:
        import keyring

        return keyring.get_password(self.SERVICE, name)

    def delete(self, name: str) -> None:
        import keyring

        with suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(self.SERVICE, name)
