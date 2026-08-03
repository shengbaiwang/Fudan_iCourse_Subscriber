"""Small macOS Keychain adapter for the local console's remembered session."""

from __future__ import annotations

import subprocess
import sys
from typing import Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from .state import RepositorySettings, RuntimeCredentials


SERVICE_PREFIX = "com.fudan-icourse-subscriber.local-web"
ACCOUNTS = ("github-token", "stuid", "uispsw")


class CredentialStore(Protocol):
    @property
    def available(self) -> bool: ...

    def load(self, settings: "RepositorySettings") -> "RuntimeCredentials | None": ...

    def save(self, settings: "RepositorySettings", credentials: "RuntimeCredentials") -> None: ...

    def delete(self, settings: "RepositorySettings") -> None: ...


class MacOSKeychainStore:
    """Use the logged-in user's Keychain; never mirror secret values to disk."""

    @property
    def available(self) -> bool:
        return sys.platform == "darwin"

    @staticmethod
    def _service(settings: "RepositorySettings") -> str:
        return f"{SERVICE_PREFIX}:{settings.owner}/{settings.repo}"

    def _read(self, service: str, account: str) -> str | None:
        if not self.available:
            return None
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.rstrip("\n") or None

    def load(self, settings: "RepositorySettings") -> "RuntimeCredentials | None":
        from .state import RuntimeCredentials

        service = self._service(settings)
        values = {account: self._read(service, account) for account in ACCOUNTS}
        if not all(values.values()):
            return None
        return RuntimeCredentials(
            token=str(values["github-token"]),
            stuid=str(values["stuid"]),
            uispsw=str(values["uispsw"]),
        )

    def save(self, settings: "RepositorySettings", credentials: "RuntimeCredentials") -> None:
        if not self.available:
            raise OSError("当前系统不支持 macOS 钥匙串")
        service = self._service(settings)
        values = {
            "github-token": credentials.token,
            "stuid": credentials.stuid,
            "uispsw": credentials.uispsw,
        }
        for account, value in values.items():
            # The value is handed directly to the system Keychain command and
            # is never logged or written to the project/configuration files.
            result = subprocess.run(
                [
                    "security", "add-generic-password", "-U",
                    "-s", service, "-a", account, "-w", value,
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode != 0:
                raise OSError("无法写入 macOS 钥匙串")

    def delete(self, settings: "RepositorySettings") -> None:
        if not self.available:
            return
        service = self._service(settings)
        for account in ACCOUNTS:
            subprocess.run(
                ["security", "delete-generic-password", "-s", service, "-a", account],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
