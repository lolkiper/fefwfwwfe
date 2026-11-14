from __future__ import annotations

import re
import time
import uuid
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from requests import Session

INTEGRITY_URL = "https://gql.twitch.tv/integrity"
TWITCH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_DEFAULT_CLIENT_VERSION = "9522504c-bfa6-4923-85c0-90a5f66638de"


def fetch_twilight_build_id() -> str:
    try:
        response = requests.get(
            "https://www.twitch.tv/drops/inventory",
            headers={"User-Agent": TWITCH_UA},
            timeout=15,
        )
        response.raise_for_status()
        match = re.search(r'twilightBuildID="([0-9a-f-]+)"', response.text)
        if match:
            return match.group(1)
    except requests.RequestException:
        pass
    return _DEFAULT_CLIENT_VERSION


class TwitchIntegrity:
    """Получение Client-Integrity для GQL-мутаций (отвязка и т.п.)."""

    def __init__(self, client_id: str, client_version: str | None = None) -> None:
        self.client_id = client_id
        self.client_version = client_version or fetch_twilight_build_id()
        self.device_id = str(uuid.uuid4()).replace("-", "")[:32]
        self.session_id = str(uuid.uuid4())
        self._token: str | None = None
        self._expires_at = 0.0

    def _base_headers(self, oauth_token: str) -> dict[str, str]:
        return {
            "Client-Id": self.client_id,
            "Authorization": f"OAuth {oauth_token}",
            "X-Device-Id": self.device_id,
            "Device-Id": self.device_id,
            "Client-Version": self.client_version,
            "Client-Session-Id": self.session_id,
            "User-Agent": TWITCH_UA,
        }

    def _refresh_http(
        self, http: Session, oauth_token: str, timeout: float | tuple[float, float]
    ) -> str:
        headers = {**self._base_headers(oauth_token), "Content-Type": "application/json"}
        response = http.post(INTEGRITY_URL, headers=headers, data=b"", timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        token = str(payload.get("token") or "")
        if not token:
            raise RuntimeError("Twitch не вернул integrity-токен.")
        expiration = payload.get("expiration")
        if isinstance(expiration, (int, float)):
            self._expires_at = float(expiration)
        else:
            self._expires_at = time.time() + 600
        self._token = token
        return token

    def _refresh_browser(self, oauth_token: str, proxy_url: str | None = None) -> str:
        raise RuntimeError("Отвязка выполняется через браузер (см. unlink_drop_game_in_browser).")

    def refresh(
        self,
        http: Session,
        oauth_token: str,
        timeout: float | tuple[float, float],
        *,
        use_browser: bool = True,
    ) -> str:
        if use_browser:
            try:
                return self._refresh_browser(oauth_token)
            except Exception:
                pass
        return self._refresh_http(http, oauth_token, timeout)

    def ensure_token(
        self,
        http: Session,
        oauth_token: str,
        timeout: float | tuple[float, float],
        *,
        use_browser: bool = True,
    ) -> str:
        if self._token and time.time() < self._expires_at - 30:
            return self._token
        return self.refresh(http, oauth_token, timeout, use_browser=use_browser)

    def gql_headers(self, token: str | None = None) -> dict[str, str]:
        value = token or self._token
        if not value:
            raise RuntimeError("Нет Client-Integrity токена.")
        return {
            "Client-Integrity": value,
            "X-Device-Id": self.device_id,
            "Device-Id": self.device_id,
            "Client-Version": self.client_version,
            "Client-Session-Id": self.session_id,
            "User-Agent": TWITCH_UA,
        }

    def invalidate(self) -> None:
        self._token = None
        self._expires_at = 0.0