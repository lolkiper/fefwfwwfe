from __future__ import annotations

import argparse
import getpass
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol, Sequence, TypeVar

import requests

from twitch_unlink.client import (
    OAuthApp,
    Organization,
    SocialLink,
    TokenInfo,
    TwitchUnlinkClient,
)

T = TypeVar("T")


class ConnectionItem(Protocol):
    @property
    def key(self) -> str: ...

    @property
    def name(self) -> str: ...


def _fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def _print_apps(apps: Sequence[OAuthApp]) -> None:
    print("\n=== OAuth-приложения (Other Connections) ===")
    if not apps:
        print("Список пуст.")
        return
    for i, app in enumerate(apps, start=1):
        scopes = ", ".join(app.scopes) if app.scopes else "—"
        print(f"{i:>3}. {app.name}")
        print(f"      client_id: {app.client_id}")
        print(f"      подключено: {_fmt_dt(app.created_at)}")
        print(f"      scopes: {scopes}")


def _print_orgs(orgs: Sequence[Organization]) -> None:
    print("\n=== Организации (Developer Organizations) ===")
    if not orgs:
        print("Список пуст.")
        return
    for i, org in enumerate(orgs, start=1):
        leave = "можно выйти" if org.can_leave else "выход недоступен"
        print(f"{i:>3}. {org.name}")
        print(f"      id: {org.org_id}")
        print(f"      роль: {org.role} ({leave})")
        print(f"      в организации с: {_fmt_dt(org.joined_at)}")


def _print_social(links: Sequence[SocialLink]) -> None:
    print("\n=== Социальные аккаунты ===")
    linked = [link for link in links if link.linked]
    if not linked:
        print("Нет активных привязок.")
        return
    for i, link in enumerate(linked, start=1):
        print(f"{i:>3}. {link.label} — {link.detail or 'подключено'}")


def _match_items(
    items: Sequence[T],
    query: str,
    *,
    key_fn: Callable[[T], str],
    name_fn: Callable[[T], str],
) -> list[T]:
    q = query.strip().lower()
    if not q:
        return []
    exact = [i for i in items if key_fn(i).lower() == q or name_fn(i).lower() == q]
    if exact:
        return exact
    partial = [i for i in items if q in name_fn(i).lower() or q in key_fn(i).lower()]
    return partial


def _pick_from_list(items: Sequence[T], label: str, name_fn: Callable[[T], str]) -> T | None:
    if not items:
        print("Список пуст.")
        return None
    print(f"\nВыберите {label} (номер из списка) или введите название / ID вручную.")
    print("Пустая строка — отмена.")
    choice = input("> ").strip()
    if not choice:
        return None
    if choice.isdigit():
        idx = int(choice)
        if 1 <= idx <= len(items):
            return items[idx - 1]
        print("Неверный номер.")
        return None
    matched = _match_items(
        items,
        choice,
        key_fn=lambda x: str(getattr(x, "key")),
        name_fn=name_fn,
    )
    if not matched:
        print("Совпадений не найдено.")
        return None
    if len(matched) > 1:
        print("Найдено несколько совпадений, уточните запрос:")
        for m in matched:
            print(f"  - {name_fn(m)} ({getattr(m, 'key')})")
        return None
    return matched[0]


def _resolve_token(args: argparse.Namespace) -> str:
    if args.token:
        return TwitchUnlinkClient.normalize_token(args.token)
    if args.token_file:
        content = Path(args.token_file).read_text(encoding="utf-8")
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            return TwitchUnlinkClient.parse_credentials_line(stripped)
        raise ValueError(f"В файле {args.token_file} нет токена.")
    import os

    env_token = os.environ.get("TWITCH_TOKEN") or os.environ.get("TWITCH_ACCESS_TOKEN")
    if env_token:
        return TwitchUnlinkClient.normalize_token(env_token)
    if args.non_interactive:
        raise ValueError("Укажите --token, --token-file или переменную TWITCH_TOKEN.")
    print("Введите OAuth-токен Twitch (можно строку login:password:token — будет использован только токен).")
    raw = getpass.getpass("Токен: ")
    return TwitchUnlinkClient.normalize_token(raw)


def _run_disconnect_apps(client: TwitchUnlinkClient, args: argparse.Namespace) -> None:
    apps = client.list_oauth_apps()
    _print_apps(apps)
    if args.list_only:
        return
    target_id = args.id or args.name
    if target_id:
        matched = _match_items(
            apps,
            target_id,
            key_fn=lambda a: a.client_id,
            name_fn=lambda a: a.name,
        )
        if not matched:
            raise SystemExit(f"Приложение не найдено: {target_id}")
        if len(matched) > 1:
            raise SystemExit("Найдено несколько приложений, уточните --id или --name.")
        app = matched[0]
    elif args.non_interactive:
        raise SystemExit("Укажите --id или --name для отвязки в неинтерактивном режиме.")
    else:
        app = _pick_from_list(apps, "приложение", lambda a: a.name)
        if not app:
            return

    if not args.yes:
        confirm = input(f"Отвязать «{app.name}» ({app.client_id})? [y/N] ").strip().lower()
        if confirm not in ("y", "yes", "д", "да"):
            print("Отменено.")
            return
    client.unlink_oauth_app(app.client_id)
    print(f"Готово: «{app.name}» отвязано.")


def _run_disconnect_orgs(client: TwitchUnlinkClient, token: TokenInfo, args: argparse.Namespace) -> None:
    orgs = client.list_organizations()
    _print_orgs(orgs)
    if args.list_only:
        return
    target_id = args.id or args.name
    if target_id:
        matched = _match_items(
            orgs,
            target_id,
            key_fn=lambda o: o.org_id,
            name_fn=lambda o: o.name,
        )
        if not matched:
            raise SystemExit(f"Организация не найдена: {target_id}")
        org = matched[0]
    elif args.non_interactive:
        raise SystemExit("Укажите --id или --name для выхода в неинтерактивном режиме.")
    else:
        org = _pick_from_list(orgs, "организацию", lambda o: o.name)
        if not org:
            return
    if not org.can_leave:
        raise SystemExit(f"Выход из «{org.name}» недоступен для вашей роли ({org.role}).")

    if not args.yes:
        confirm = input(f"Покинуть организацию «{org.name}»? [y/N] ").strip().lower()
        if confirm not in ("y", "yes", "д", "да"):
            print("Отменено.")
            return
    client.leave_organization(org.org_id, token.user_id)
    print(f"Готово: вы вышли из «{org.name}».")


def _run_disconnect_social(client: TwitchUnlinkClient, token: TokenInfo, args: argparse.Namespace) -> None:
    links = client.list_social_links()
    linked = [link for link in links if link.linked]
    _print_social(links)
    if args.list_only:
        return
    target = args.id or args.name
    if target:
        matched = _match_items(
            linked,
            target,
            key_fn=lambda l: l.platform_id,
            name_fn=lambda l: l.label,
        )
        if not matched:
            raise SystemExit(f"Привязка не найдена или не активна: {target}")
        link = matched[0]
    elif args.non_interactive:
        raise SystemExit("Укажите --id или --name платформы.")
    else:
        link = _pick_from_list(linked, "платформу", lambda l: l.label)
        if not link:
            return

    if not args.yes:
        confirm = input(f"Отвязать {link.label}? [y/N] ").strip().lower()
        if confirm not in ("y", "yes", "д", "да"):
            print("Отменено.")
            return
    client.unlink_social(link.platform_id, token.user_id)
    print(f"Готово: {link.label} отвязан.")


def _run_interactive(client: TwitchUnlinkClient, token: TokenInfo) -> None:
    while True:
        print("\n--- Меню ---")
        print("1 — Список и отвязка OAuth-приложений (компании / Other Connections)")
        print("2 — Список и отвязка социальных аккаунтов")
        print("3 — Список и выход из организаций")
        print("4 — Показать всё")
        print("0 — Выход")
        choice = input("> ").strip()
        if choice == "0":
            return
        if choice == "1":
            ns = argparse.Namespace(list_only=False, id=None, name=None, yes=False, non_interactive=False)
            _run_disconnect_apps(client, ns)
        elif choice == "2":
            ns = argparse.Namespace(list_only=False, id=None, name=None, yes=False, non_interactive=False)
            _run_disconnect_social(client, token, ns)
        elif choice == "3":
            ns = argparse.Namespace(list_only=False, id=None, name=None, yes=False, non_interactive=False)
            _run_disconnect_orgs(client, token, ns)
        elif choice == "4":
            _print_apps(client.list_oauth_apps())
            _print_social(client.list_social_links())
            _print_orgs(client.list_organizations())
        else:
            print("Неизвестный пункт.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Просмотр и отвязка подключений Twitch по OAuth-токену.",
    )
    parser.add_argument(
        "--token",
        help="OAuth-токен (или login:password:token — будет взят только токен).",
    )
    parser.add_argument(
        "--token-file",
        help="Файл с токеном или строкой login:password:token (первая непустая строка).",
    )
    parser.add_argument(
        "--client-id",
        default=None,
        help=f"Client-Id для GQL (по умолчанию публичный ID сайта Twitch).",
    )
    sub = parser.add_subparsers(dest="command")

    apps = sub.add_parser("apps", help="OAuth-приложения (Other Connections)")
    apps.add_argument("--list", dest="list_only", action="store_true", help="Только список")
    apps.add_argument("--id", help="client_id приложения")
    apps.add_argument("--name", help="название приложения (частичное совпадение)")
    apps.add_argument("--yes", "-y", action="store_true", help="Без подтверждения")

    social = sub.add_parser("social", help="Социальные привязки (Steam, Amazon, …)")
    social.add_argument("--list", dest="list_only", action="store_true")
    social.add_argument("--id", help="идентификатор платформы (steam, amazon, …)")
    social.add_argument("--name", help="название платформы")
    social.add_argument("--yes", "-y", action="store_true")

    orgs = sub.add_parser("orgs", help="Организации разработчика Twitch")
    orgs.add_argument("--list", dest="list_only", action="store_true")
    orgs.add_argument("--id", help="id организации")
    orgs.add_argument("--name", help="название организации")
    orgs.add_argument("--yes", "-y", action="store_true")

    sub.add_parser("interactive", help="Интерактивное меню (по умолчанию)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.non_interactive = bool(args.token or args.token_file)

    try:
        token = _resolve_token(args)
    except (ValueError, OSError) as exc:
        print(f"Ошибка токена: {exc}", file=sys.stderr)
        return 2

    client_id = args.client_id
    from twitch_unlink.client import DEFAULT_CLIENT_ID

    client = TwitchUnlinkClient(token, client_id=client_id or DEFAULT_CLIENT_ID)

    try:
        token_info = client.validate()
    except PermissionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except requests.HTTPError as exc:
        print(f"Ошибка проверки токена: {exc}", file=sys.stderr)
        return 1

    print(f"Аккаунт: {token_info.login} (id {token_info.user_id})")

    command = args.command or "interactive"
    try:
        if command == "interactive":
            _run_interactive(client, token_info)
        elif command == "apps":
            _run_disconnect_apps(client, args)
        elif command == "social":
            _run_disconnect_social(client, token_info, args)
        elif command == "orgs":
            _run_disconnect_orgs(client, token_info, args)
        else:
            parser.print_help()
            return 2
    except RuntimeError as exc:
        print(f"Ошибка Twitch API: {exc}", file=sys.stderr)
        return 1
    except PermissionError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())