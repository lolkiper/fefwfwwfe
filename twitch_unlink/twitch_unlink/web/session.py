from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from twitch_unlink.client import OAuthApp, Organization, SocialLink, TwitchUnlinkClient
from twitch_unlink.proxy_rotator import ProxyRotator


@dataclass
class UserSession:
    session_id: str
    client: TwitchUnlinkClient
    rotator: ProxyRotator | None
    login: str
    user_id: str
    created_at: float

    def proxy_status(self) -> dict[str, Any]:
        if self.rotator:
            return self.rotator.status().to_dict()
        return {
            "enabled": False,
            "current": None,
            "index": 0,
            "total": 0,
            "interval_sec": 60,
            "seconds_until_rotation": 0,
        }


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, UserSession] = {}
        self._lock = threading.Lock()

    def create(
        self,
        token: str,
        proxies: list[str],
        proxy_interval_sec: float = 60.0,
    ) -> UserSession:
        session_id = str(uuid.uuid4())
        client = TwitchUnlinkClient(token)
        rotator: ProxyRotator | None = None

        if proxies:
            def on_rotate(proxy: str | None) -> None:
                client.apply_proxy(proxy)

            rotator = ProxyRotator(proxies, interval_sec=proxy_interval_sec, on_rotate=on_rotate)
            rotator.start()
            client.attach_proxy_rotator(rotator)
            client.apply_proxy(rotator.current())

        info = client.validate()
        session = UserSession(
            session_id=session_id,
            client=client,
            rotator=rotator,
            login=info.login,
            user_id=info.user_id,
            created_at=time.time(),
        )
        with self._lock:
            self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> UserSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def destroy(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session and session.rotator:
            session.rotator.stop()


def _dt_iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def serialize_app(app: OAuthApp) -> dict[str, Any]:
    return {
        "id": app.client_id,
        "name": app.name,
        "created_at": _dt_iso(app.created_at),
        "scopes": app.scopes,
        "kind": "app",
    }


def serialize_org(org: Organization) -> dict[str, Any]:
    return {
        "id": org.org_id,
        "name": org.name,
        "role": org.role,
        "can_leave": org.can_leave,
        "joined_at": _dt_iso(org.joined_at),
        "kind": "org",
    }


def serialize_social(link: SocialLink) -> dict[str, Any]:
    return {
        "id": link.platform_id,
        "name": link.label,
        "linked": link.linked,
        "detail": link.detail,
        "kind": "social",
    }