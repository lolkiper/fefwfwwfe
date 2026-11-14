from __future__ import annotations

import requests

class ProxyConnectionError(RuntimeError):
    """Все прокси из списка не сработали."""


def format_user_error(exc: BaseException) -> str:
    if isinstance(exc, ProxyConnectionError):
        return str(exc)
    if isinstance(exc, PermissionError):
        return str(exc)
    if isinstance(exc, RuntimeError):
        msg = str(exc)
        if "failed integrity check" in msg.lower() or "integrity" in msg.lower():
            return (
                "Twitch отклонил integrity. Установите 1.3.6+, Google Chrome. "
                "Нажмите «Сброс прокси» — отвязка в Chrome всегда без прокси из файла. "
                "Не закрывайте окно Chrome, дождитесь завершения (до ~30 сек)."
            )
        if msg:
            return msg
    if isinstance(exc, requests.exceptions.ProxyError):
        return (
            "Прокси не отвечает (ошибка туннеля / 502). "
            "Программа попробует другие прокси из файла; если ошибка повторяется — замените список или нажмите «Сброс прокси»."
        )
    if isinstance(exc, requests.exceptions.ConnectionError):
        text = str(exc).lower()
        if "proxy" in text or "tunnel" in text or "502" in text:
            return (
                "Нет связи через прокси (502 Bad Gateway или обрыв). "
                "Проверьте прокси или отключите их кнопкой «Сброс прокси»."
            )
        return "Нет соединения с Twitch. Проверьте интернет или прокси."
    if isinstance(exc, requests.exceptions.Timeout):
        return "Превышено время ожидания ответа (прокси или Twitch не отвечают)."
    if isinstance(exc, requests.exceptions.HTTPError):
        return f"Ошибка HTTP Twitch: {exc}"
    return str(exc) or exc.__class__.__name__