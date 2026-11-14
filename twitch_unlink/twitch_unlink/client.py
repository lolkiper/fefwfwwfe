from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

from twitch_unlink.errors import ProxyConnectionError
from twitch_unlink.integrity import TWITCH_UA, TwitchIntegrity
from twitch_unlink.proxy_rotator import mask_proxy

DEFAULT_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
GQL_URL = "https://gql.twitch.tv/gql"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
# Короткий connect-timeout — быстрее перебирать мёртвые прокси (502).
REQUEST_TIMEOUT = (6, 25)


@dataclass(frozen=True)
class TokenInfo:
    client_id: str
    login: str
    user_id: str
    scopes: list[str]
    expires_in: int | None


@dataclass(frozen=True)
class OAuthApp:
    client_id: str
    name: str
    created_at: datetime | None
    scopes: list[str]

    @property
    def key(self) -> str:
        return self.client_id


@dataclass(frozen=True)
class Organization:
    org_id: str
    name: str
    role: str
    can_leave: bool
    joined_at: datetime | None

    @property
    def key(self) -> str:
        return self.org_id


@dataclass(frozen=True)
class DropGameLink:
    """Привязка игрового аккаунта для Twitch Drops (например Standoff 2)."""

    game_id: str
    name: str
    client_id: str
    campaign_name: str | None
    linked_at: datetime | None
    in_progress: bool

    @property
    def key(self) -> str:
        return self.client_id or self.game_id


@dataclass(frozen=True)
class SocialLink:
    platform_id: str
    label: str
    display: str
    linked: bool
    detail: str | None

    @property
    def key(self) -> str:
        return self.platform_id


class TwitchUnlinkClient:
    def __init__(
        self,
        token: str,
        client_id: str = DEFAULT_CLIENT_ID,
        proxy: str | None = None,
    ) -> None:
        self.token = self.normalize_token(token)
        self.client_id = client_id
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Client-Id": client_id,
                "Authorization": f"OAuth {self.token}",
                "Content-Type": "application/json",
                "User-Agent": TWITCH_UA,
            }
        )
        self._integrity = TwitchIntegrity(client_id)
        self._proxy_rotator: Any = None
        self.user_id: str | None = None
        self.login: str | None = None
        self.apply_proxy(proxy)

    def attach_proxy_rotator(self, rotator: Any) -> None:
        self._proxy_rotator = rotator
        self.apply_proxy(rotator.current())

    def apply_proxy(self, proxy: str | None) -> None:
        if proxy:
            self._session.proxies = {"http": proxy, "https": proxy}
        else:
            self._session.proxies = {}

    def current_proxy_url(self) -> str | None:
        if not self._session.proxies:
            return None
        return self._session.proxies.get("https") or self._session.proxies.get("http")

    def _sync_proxy(self) -> None:
        if self._proxy_rotator is not None:
            self.apply_proxy(self._proxy_rotator.current())

    def _max_proxy_attempts(self) -> int:
        if self._proxy_rotator is not None:
            return min(15, max(1, self._proxy_rotator.total))
        return 1

    def _with_proxy_retry(self, make_request: Callable[[], requests.Response]) -> requests.Response:
        last: Exception | None = None
        attempts = self._max_proxy_attempts()
        used_proxy = self._proxy_rotator is not None and self._proxy_rotator.total > 0

        for attempt in range(attempts):
            self._sync_proxy()
            try:
                return make_request()
            except requests.exceptions.HTTPError:
                raise
            except (
                requests.exceptions.ProxyError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                OSError,
            ) as exc:
                last = exc
                if attempt + 1 >= attempts:
                    break
                if self._proxy_rotator is not None:
                    self._proxy_rotator.advance()
                else:
                    break

        if used_proxy:
            self.apply_proxy(None)
            try:
                return make_request()
            except requests.exceptions.HTTPError:
                raise
            except (
                requests.exceptions.ProxyError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                OSError,
            ) as direct_exc:
                last = direct_exc

        masked = mask_proxy(self._session.proxies.get("https") if self._session.proxies else None)
        hint = f" Последний прокси: {masked}." if masked else ""
        raise ProxyConnectionError(
            f"Не удалось связаться с Twitch ({attempts} прокси + прямое подключение).{hint} "
            "Проверьте интернет, токен или нажмите «Сброс прокси»."
        ) from last

    @staticmethod
    def split_credential_fields(raw: str) -> tuple[str | None, str]:
        """login:password:token — пароль может содержать «:», токен всегда последнее поле."""
        value = raw.strip()
        if not value:
            raise ValueError("Пустая строка.")
        if value.lower().startswith("oauth "):
            return None, value[6:].strip()
        parts = value.split(":")
        if len(parts) < 2:
            return None, parts[0]
        if len(parts) == 2:
            return parts[0].strip() or None, parts[1].strip()
        login = parts[0].strip() or None
        token = parts[-1].strip()
        return login, token

    @staticmethod
    def normalize_token(raw: str) -> str:
        _, token = TwitchUnlinkClient.split_credential_fields(raw)
        if not token:
            raise ValueError("Не удалось извлечь токен из строки.")
        return token

    @staticmethod
    def parse_credentials_line(line: str) -> str:
        line = line.strip()
        if not line or line.startswith("#"):
            raise ValueError("Пустая строка.")
        return TwitchUnlinkClient.normalize_token(line)

    def validate(self) -> TokenInfo:
        response = self._with_proxy_retry(
            lambda: self._session.get(VALIDATE_URL, timeout=REQUEST_TIMEOUT)
        )
        if response.status_code == 401:
            raise PermissionError("Токен недействителен или истёк.")
        response.raise_for_status()
        data = response.json()
        info = TokenInfo(
            client_id=str(data.get("client_id", "")),
            login=str(data.get("login", "")),
            user_id=str(data.get("user_id", "")),
            scopes=list(data.get("scopes") or []),
            expires_in=data.get("expires_in"),
        )
        self.login = info.login
        self.user_id = info.user_id
        return info

    def gql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        operation_name: str | None = None,
        *,
        integrity: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        if operation_name:
            payload["operationName"] = operation_name

        last_messages: list[str] = []
        for attempt in range(2):
            extra_headers: dict[str, str] = {}
            if integrity:
                token = self._integrity.ensure_token(
                    self._session, self.token, REQUEST_TIMEOUT, use_browser=True
                )
                extra_headers = self._integrity.gql_headers(token)

            response = self._with_proxy_retry(
                lambda: self._session.post(
                    GQL_URL,
                    data=json.dumps(payload),
                    headers=extra_headers or None,
                    timeout=REQUEST_TIMEOUT,
                )
            )
            response.raise_for_status()
            body = response.json()

            if not body.get("errors"):
                return body.get("data") or {}

            last_messages = [e.get("message", str(e)) for e in body["errors"]]
            needs_integrity = any("integrity" in m.lower() for m in last_messages)
            if integrity and needs_integrity and attempt == 0:
                self._integrity.invalidate()
                continue
            break

        raise RuntimeError("; ".join(last_messages))

    def list_oauth_apps(self) -> list[OAuthApp]:
        query = """
        query ListOAuthApps {
          currentUser {
            clientAuthorizations {
              clientID
              clientName
              createdAt
              scopes
            }
          }
        }
        """
        data = self.gql(query)
        user = data.get("currentUser")
        if not user:
            raise PermissionError("Не удалось получить currentUser — проверьте токен.")

        apps: list[OAuthApp] = []
        for item in user.get("clientAuthorizations") or []:
            apps.append(
                OAuthApp(
                    client_id=str(item.get("clientID", "")),
                    name=str(item.get("clientName") or "(без названия)"),
                    created_at=_parse_time(item.get("createdAt")),
                    scopes=[str(s) for s in (item.get("scopes") or [])],
                )
            )
        apps.sort(key=lambda a: a.name.lower())
        return apps

    def list_organizations(self) -> list[Organization]:
        query = """
        query ListOrganizations {
          currentUser {
            organizations {
              id
              name
              self {
                canLeaveOrganization
                role
                joinedAt
              }
            }
          }
        }
        """
        data = self.gql(query)
        user = data.get("currentUser")
        if not user:
            raise PermissionError("Не удалось получить currentUser — проверьте токен.")

        orgs: list[Organization] = []
        for item in user.get("organizations") or []:
            self_edge = item.get("self") or {}
            orgs.append(
                Organization(
                    org_id=str(item.get("id", "")),
                    name=str(item.get("name") or "(без названия)"),
                    role=str(self_edge.get("role") or "?"),
                    can_leave=bool(self_edge.get("canLeaveOrganization")),
                    joined_at=_parse_time(self_edge.get("joinedAt")),
                )
            )
        orgs.sort(key=lambda o: o.name.lower())
        return orgs

    def list_social_links(self) -> list[SocialLink]:
        query = """
        query ListSocialLinks {
          currentUser {
            accountConnections {
              hasConnectedAmazon
              steam { id }
              riot { id }
              amazon { id }
              twitter { username }
              youtube { __typename }
              facebook { __typename }
              blizzard { __typename }
              instagram { username }
              tiktok { __typename }
              snapchat { username }
            }
          }
        }
        """
        data = self.gql(query)
        user = data.get("currentUser")
        if not user:
            raise PermissionError("Не удалось получить currentUser — проверьте токен.")

        connections = user.get("accountConnections") or {}
        specs = [
            ("steam", "Steam", lambda c: _detail_id(c.get("steam"))),
            ("riot", "Riot Games", lambda c: _detail_id(c.get("riot"))),
            ("amazon", "Amazon / Prime", lambda c: _detail_id(c.get("amazon")) or ("да" if c.get("hasConnectedAmazon") else None)),
            ("twitter", "Twitter / X", lambda c: _detail_username(c.get("twitter"))),
            ("youtube", "YouTube", lambda c: "подключено" if c.get("youtube") else None),
            ("facebook", "Facebook", lambda c: "подключено" if c.get("facebook") else None),
            ("blizzard", "Blizzard / Battle.net", lambda c: "подключено" if c.get("blizzard") else None),
            ("instagram", "Instagram", lambda c: _detail_username(c.get("instagram"))),
            ("tiktok", "TikTok", lambda c: "подключено" if c.get("tiktok") else None),
            ("snapchat", "Snapchat", lambda c: _detail_username(c.get("snapchat"))),
        ]

        links: list[SocialLink] = []
        for platform_id, label, extractor in specs:
            detail = extractor(connections)
            links.append(
                SocialLink(
                    platform_id=platform_id,
                    label=label,
                    display=label,
                    linked=detail is not None,
                    detail=detail,
                )
            )
        return links

    def unlink_oauth_app(self, client_id: str) -> None:
        self.unlink_drop_game(client_id)

    def unlink_drop_game(self, client_id: str) -> None:
        """Отвязать игровой аккаунт Drops (OAuth client ID игры / издателя)."""
        from twitch_unlink.integrity_browser import unlink_drop_game_in_browser

        unlink_drop_game_in_browser(
            self.token,
            client_id,
            twitch_client_id=self.client_id,
            proxy_url=None,
            integrity=self._integrity,
        )

    def list_drop_game_links(self) -> list[DropGameLink]:
        query = """
        query LinkedDropGames {
          currentUser {
            clientAuthorizations {
              clientID
              clientName
              createdAt
            }
            inventory {
              dropCampaignsInProgress {
                id
                name
                self { isAccountConnected }
                game {
                  id
                  name
                  displayName
                  dropAccountLink { clientID updatedAt }
                }
              }
            }
            dropCampaigns {
              id
              name
              self { isAccountConnected }
              game {
                id
                name
                displayName
                dropAccountLink { clientID updatedAt }
              }
            }
          }
        }
        """
        data = self.gql(query)
        user = data.get("currentUser")
        if not user:
            raise PermissionError("Не удалось получить currentUser — проверьте токен.")

        campaigns: list[dict[str, Any]] = []
        inventory = user.get("inventory") or {}
        campaigns.extend(inventory.get("dropCampaignsInProgress") or [])
        campaigns.extend(user.get("dropCampaigns") or [])

        drop_client_ids: set[str] = set()
        for camp in campaigns:
            game = camp.get("game") or {}
            link = game.get("dropAccountLink") or {}
            client_id = link.get("clientID")
            if client_id:
                drop_client_ids.add(str(client_id))

        progress_ids = {
            str(c.get("id"))
            for c in (inventory.get("dropCampaignsInProgress") or [])
            if c.get("id")
        }

        by_client: dict[str, DropGameLink] = {}

        for camp in campaigns:
            self_edge = camp.get("self") or {}
            if not self_edge.get("isAccountConnected"):
                continue
            game = camp.get("game") or {}
            game_id = str(game.get("id") or "")
            if not game_id:
                continue
            link = game.get("dropAccountLink") or {}
            client_id = str(link.get("clientID") or "")
            if not client_id:
                continue
            name = str(game.get("displayName") or game.get("name") or "(игра)")
            entry = DropGameLink(
                game_id=game_id,
                name=name,
                client_id=client_id,
                campaign_name=str(camp.get("name") or ""),
                linked_at=_parse_time(link.get("updatedAt")),
                in_progress=str(camp.get("id") or "") in progress_ids,
            )
            existing = by_client.get(client_id)
            if existing is None or entry.in_progress:
                by_client[client_id] = entry

        for auth in user.get("clientAuthorizations") or []:
            client_id = str(auth.get("clientID") or "")
            if not client_id or client_id not in drop_client_ids:
                continue
            if client_id in by_client:
                continue
            by_client[client_id] = DropGameLink(
                game_id=client_id,
                name=str(auth.get("clientName") or "(игра)"),
                client_id=client_id,
                campaign_name=None,
                linked_at=_parse_time(auth.get("createdAt")),
                in_progress=False,
            )

        links = list(by_client.values())
        links.sort(key=lambda g: g.name.lower())
        return links

    def leave_organization(self, organization_id: str, user_id: str) -> None:
        mutation = """
        mutation RemoveOrganizationMember($organizationID: ID!, $userID: ID!) {
          removeOrganizationMember(input: { organizationID: $organizationID, userID: $userID }) {
            __typename
          }
        }
        """
        self.gql(
            mutation,
            {"organizationID": organization_id, "userID": user_id},
        )

    def unlink_social(self, platform_id: str, user_id: str) -> None:
        mutation_map = {
            "amazon": ("unlinkAmazonConnection", {}),
            "steam": ("unlinkSteamConnection", {"userID": user_id}),
            "riot": ("unlinkRiotConnection", {"userID": user_id}),
            "twitter": ("unlinkTwitterConnection", {"userID": user_id}),
            "youtube": ("unlinkYoutubeConnection", {"userID": user_id}),
            "facebook": ("unlinkFacebookConnection", {"userID": user_id}),
            "blizzard": ("unlinkBlizzardConnection", {"userID": user_id}),
            "instagram": ("unlinkInstagramConnection", {"userID": user_id}),
            "tiktok": ("unlinkTikTokConnection", {"userID": user_id}),
            "snapchat": ("unlinkSnapchatConnection", {"userID": user_id}),
        }
        spec = mutation_map.get(platform_id)
        if not spec:
            raise ValueError(f"Неизвестная платформа: {platform_id}")
        mutation_name, variables = spec
        mutation = f"""
        mutation UnlinkSocial {{
          {mutation_name}(input: {_gql_input_literal(variables)}) {{
            __typename
          }}
        }}
        """
        self.gql(mutation)


def _gql_input_literal(variables: dict[str, Any]) -> str:
    if not variables:
        return "{}"
    parts = []
    for key, value in variables.items():
        parts.append(f'{key}: "{value}"')
    return "{" + ", ".join(parts) + "}"


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _detail_id(node: dict[str, Any] | None) -> str | None:
    if not node:
        return None
    node_id = node.get("id")
    return str(node_id) if node_id else "подключено"


def _detail_username(node: dict[str, Any] | None) -> str | None:
    if not node:
        return None
    username = node.get("username")
    return str(username) if username else "подключено"