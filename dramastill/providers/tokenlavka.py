from __future__ import annotations

import httpx

PORTAL_URL = "https://tokenlavka.com/api/portal"


def fetch_portal(api_key: str, *, timeout: float = 12.0, transport: httpx.BaseTransport | None = None) -> dict:
    """Остаток из кабинета Tokenlavka. Ключ уходит только в теле запроса и не возвращается."""
    with httpx.Client(timeout=timeout, transport=transport) as client:
        response = client.post(PORTAL_URL, json={"key": api_key})
    if response.status_code == 404:
        raise LookupError("missing")
    if response.status_code >= 400:
        raise RuntimeError(f"status {response.status_code}")
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("shape")
    return data


def balance_view(api_key: str, *, transport: httpx.BaseTransport | None = None) -> tuple[str, str]:
    try:
        data = fetch_portal(api_key, transport=transport)
    except LookupError:
        return "Баланс: ключ не найден", "Tokenlavka не узнала ключ"
    except Exception:
        return "Баланс: нет связи", "Не удалось получить остаток с tokenlavka.com"
    return format_balance(data)


def format_balance(data: dict) -> tuple[str, str]:
    key = data.get("key") if isinstance(data, dict) else None
    if not isinstance(key, dict) or key.get("balance") is None:
        return "Баланс: нет данных", "Кабинет Tokenlavka не прислал остаток"
    try:
        left = float(key.get("balance"))
    except (TypeError, ValueError):
        return "Баланс: нет данных", "Кабинет Tokenlavka не прислал остаток"
    shown = _group(left)
    reserved = key.get("reserved") or 0
    try:
        reserved_value = float(reserved)
    except (TypeError, ValueError):
        reserved_value = 0.0
    tip = f"Остаток Tokenlavka: {shown}. Тот же остаток, что в кабинете."
    if reserved_value > 0:
        tip += f" В резерве на текущие запросы: {_group(reserved_value)}."
    return f"Баланс: {shown}", tip


def _group(value: float) -> str:
    if abs(value - round(value)) < 1e-6:
        raw = f"{int(round(value)):,}"
    else:
        raw = f"{value:,.2f}"
    return raw.replace(",", " ")
