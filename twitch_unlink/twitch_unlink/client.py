from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

DEFAULT_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
GQL_URL = "https://gql.twitch.tv/gql"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"


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
    def __init__(self, token: str, client_id: str = DEFAULT_CLIENT_ID) -> None:
        self.token = self.normalize_token(token)
        self.client_id = client_id
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Client-Id": client_id,
                "Authorization": f"OAuth {self.token}",
                "Content-Type": "application/json",
            }
        )

    @staticmethod
    def normalize_token(raw: str) -> str:
        value = raw.strip()
        if not value:
            raise ValueError("Токен пустой.")
        if value.lower().startswith("oauth "):
            value = value[6:].strip()
        if ":" in value:
            # login:password:token
            value = value.rsplit(":", 1)[-1].strip()
        if not value:
            raise ValueError("Не удалось извлечь токен из строки.")
        return value

    @staticmethod
    def parse_credentials_line(line: str) -> str:
        line = line.strip()
        if not line or line.startswith("#"):
            raise ValueError("Пустая строка.")
        return TwitchUnlinkClient.normalize_token(line)

    def validate(self) -> TokenInfo:
        response = self._session.get(VALIDATE_URL, timeout=30)
        if response.status_code == 401:
            raise PermissionError("Токен недействителен или истёк.")
        response.raise_for_status()
        data = response.json()
        return TokenInfo(
            client_id=str(data.get("client_id", "")),
            login=str(data.get("login", "")),
            user_id=str(data.get("user_id", "")),
            scopes=list(data.get("scopes") or []),
            expires_in=data.get("expires_in"),
        )

    def gql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        operation_name: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        if operation_name:
            payload["operationName"] = operation_name

        response = self._session.post(GQL_URL, data=json.dumps(payload), timeout=30)
        response.raise_for_status()
        body = response.json()

        if body.get("errors"):
            messages = [e.get("message", str(e)) for e in body["errors"]]
            raise RuntimeError("; ".join(messages))
        return body.get("data") or {}

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
        mutation = """
        mutation DeleteOauthApp($id: ID!) {
          deleteOauthApp(input: { id: $id }) {
            __typename
          }
        }
        """
        self.gql(mutation, {"id": client_id})

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