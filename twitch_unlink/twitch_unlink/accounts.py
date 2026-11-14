from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from twitch_unlink.client import TwitchUnlinkClient


@dataclass
class AccountLine:
    line_number: int
    file_login: str | None
    token: str
    raw_preview: str

    @property
    def display_name(self) -> str:
        if self.file_login:
            return self.file_login
        return f"строка {self.line_number}"


def parse_account_line(line: str, line_number: int) -> AccountLine | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.count(":") < 2 and not stripped.lower().startswith("oauth"):
        return None
    try:
        file_login, token = TwitchUnlinkClient.split_credential_fields(stripped)
    except ValueError:
        return None
    if not token:
        return None
    preview = stripped if len(stripped) <= 48 else stripped[:45] + "…"
    return AccountLine(
        line_number=line_number,
        file_login=file_login,
        token=token,
        raw_preview=preview,
    )


def load_accounts_file(path: str | Path) -> list[AccountLine]:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    accounts: list[AccountLine] = []
    for i, line in enumerate(text.splitlines(), start=1):
        parsed = parse_account_line(line, i)
        if parsed:
            accounts.append(parsed)
    if not accounts:
        raise ValueError("В файле нет строк аккаунтов (формат login:password:token).")
    return accounts