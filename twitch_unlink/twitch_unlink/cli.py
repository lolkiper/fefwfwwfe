from __future__ import annotations

import argparse
import getpass
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence, TypeVar

import requests

from twitch_unlink.client import DropGameLink, TokenInfo, TwitchUnlinkClient

T = TypeVar("T")


def _fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def _print_drop_games(games: Sequence[DropGameLink]) -> None:
    print("\n=== Игры Twitch Drops (привязанные аккаунты) ===")
    if not games:
        print("Нет активных привязок. Укажите client_id вручную для отвязки.")
        return
    for i, g in enumerate(games, start=1):
        flag = " [в инвентаре]" if g.in_progress else ""
        print(f"{i:>3}. {g.name}{flag}")
        print(f"      client_id: {g.client_id}")
        if g.campaign_name:
            print(f"      кампания: {g.campaign_name}")
        print(f"      привязано: {_fmt_dt(g.linked_at)}")


def _match_items(items: Sequence[T], query: str, name_fn: Callable[[T], str], key_fn: Callable[[T], str]) -> list[T]:
    q = query.strip().lower()
    if not q:
        return []
    exact = [i for i in items if key_fn(i).lower() == q or name_fn(i).lower() == q]
    if exact:
        return exact
    return [i for i in items if q in name_fn(i).lower() or q in key_fn(i).lower()]


def _pick_from_list(items: Sequence[T], label: str, name_fn: Callable[[T], str], key_fn: Callable[[T], str]) -> T | None:
    if not items:
        print("Список пуст.")
        return None
    print(f"\nВыберите {label} (номер) или введите название / client_id.")
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
    matched = _match_items(items, choice, name_fn, key_fn)
    if not matched:
        print("Совпадений не найдено.")
        return None
    if len(matched) > 1:
        print("Несколько совпадений:")
        for m in matched:
            print(f"  - {name_fn(m)} ({key_fn(m)})")
        return None
    return matched[0]


def _resolve_token(args: argparse.Namespace) -> str:
    if args.token:
        return TwitchUnlinkClient.normalize_token(args.token)
    if args.token_file:
        content = Path(args.token_file).read_text(encoding="utf-8")
        for line in content.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                return TwitchUnlinkClient.parse_credentials_line(stripped)
        raise ValueError(f"В файле {args.token_file} нет токена.")
    import os

    env_token = os.environ.get("TWITCH_TOKEN") or os.environ.get("TWITCH_ACCESS_TOKEN")
    if env_token:
        return TwitchUnlinkClient.normalize_token(env_token)
    if args.non_interactive:
        raise ValueError("Укажите --token, --token-file или TWITCH_TOKEN.")
    print("OAuth-токен (можно login:password:token — берётся только токен).")
    raw = getpass.getpass("Токен: ")
    return TwitchUnlinkClient.normalize_token(raw)


def _run_drops(client: TwitchUnlinkClient, args: argparse.Namespace) -> None:
    games = client.list_drop_game_links()
    _print_drop_games(games)
    if args.list_only:
        return
    target = args.id or args.name
    if target:
        matched = _match_items(
            games,
            target,
            name_fn=lambda g: g.name,
            key_fn=lambda g: g.client_id,
        )
        if not matched:
            if args.id or (target.isalnum() and len(target) > 8):
                game = DropGameLink(
                    game_id=target,
                    name=target,
                    client_id=target,
                    campaign_name=None,
                    linked_at=None,
                    in_progress=False,
                )
            else:
                raise SystemExit(f"Игра не найдена: {target}")
        else:
            if len(matched) > 1:
                raise SystemExit("Уточните --id или --name")
            game = matched[0]
    elif args.non_interactive:
        raise SystemExit("Укажите --id (client_id) или --name для отвязки.")
    else:
        picked = _pick_from_list(
            games,
            "игру",
            lambda g: g.name,
            lambda g: g.client_id,
        )
        if not picked:
            return
        game = picked

    if not args.yes:
        confirm = input(f"Отвязать «{game.name}» ({game.client_id})? [y/N] ").strip().lower()
        if confirm not in ("y", "yes", "д", "да"):
            print("Отменено.")
            return
    client.unlink_drop_game(game.client_id)
    print(f"Готово: «{game.name}» отвязана от Drops.")


def _run_interactive(client: TwitchUnlinkClient) -> None:
    while True:
        print("\n--- Drops: отвязка игр ---")
        print("1 — Список привязанных игр и отвязка")
        print("2 — Только список")
        print("0 — Выход")
        choice = input("> ").strip()
        if choice == "0":
            return
        if choice == "1":
            ns = argparse.Namespace(list_only=False, id=None, name=None, yes=False, non_interactive=False)
            _run_drops(client, ns)
        elif choice == "2":
            _print_drop_games(client.list_drop_game_links())
        else:
            print("Неизвестный пункт.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Отвязка игровых аккаунтов Twitch Drops (Standoff 2 и др.) по OAuth-токену.",
    )
    parser.add_argument("--token", help="OAuth-токен или login:password:token")
    parser.add_argument("--token-file", help="Файл с токеном")
    parser.add_argument("--client-id", default=None, help="Client-Id для GQL")
    sub = parser.add_subparsers(dest="command")

    drops = sub.add_parser("drops", help="Список и отвязка игр Drops")
    drops.add_argument("--list", dest="list_only", action="store_true")
    drops.add_argument("--id", help="client_id игры")
    drops.add_argument("--name", help="название игры (например Standoff 2)")
    drops.add_argument("--yes", "-y", action="store_true")
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

    from twitch_unlink.client import DEFAULT_CLIENT_ID

    client = TwitchUnlinkClient(token, client_id=args.client_id or DEFAULT_CLIENT_ID)

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
            _run_interactive(client)
        elif command == "drops":
            _run_drops(client, args)
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