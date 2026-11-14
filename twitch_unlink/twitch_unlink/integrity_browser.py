"""Client-Integrity через настоящий Chrome/Edge (обход Kasada)."""

from __future__ import annotations

import sys
import time

from twitch_unlink.integrity import TWITCH_UA

KASADA_SCRIPT = (
    "https://k.twitchcdn.net/149e9513-01fa-4fb0-aad4-566afd725d1b/"
    "2d206a39-8ed7-437e-a3be-862e0f06eea3/p.js"
)

_FETCH_INTEGRITY_JS = """
async (cfg) => {
  const headers = {
    "Client-Id": cfg.clientId,
    "Authorization": "OAuth " + cfg.oauthToken,
    "X-Device-Id": cfg.deviceId,
    "Client-Version": cfg.clientVersion,
    "Client-Session-Id": cfg.clientSessionId,
  };
  const resp = await fetch("https://gql.twitch.tv/integrity", {
    method: "POST",
    headers,
    credentials: "include",
    body: null,
  });
  if (!resp.ok) {
    throw new Error("integrity HTTP " + resp.status);
  }
  return await resp.json();
}
"""

_KASADA_INTEGRITY_JS = """
async (cfg) => {
  const headers = {
    "Client-Id": cfg.clientId,
    "Authorization": "OAuth " + cfg.oauthToken,
    "X-Device-Id": cfg.deviceId,
    "Client-Version": cfg.clientVersion,
    "Client-Session-Id": cfg.clientSessionId,
  };
  return await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Kasada timeout")), cfg.timeoutMs);
    function configureKPSDK() {
      try {
        window.KPSDK.configure([{
          protocol: "https:",
          method: "POST",
          domain: "gql.twitch.tv",
          path: "/integrity",
        }]);
      } catch (e) { /* ignore */ }
    }
    async function run() {
      try {
        const resp = await fetch("https://gql.twitch.tv/integrity", {
          method: "POST",
          headers,
          credentials: "include",
        });
        if (!resp.ok) throw new Error("integrity HTTP " + resp.status);
        clearTimeout(timeout);
        resolve(await resp.json());
      } catch (e) {
        clearTimeout(timeout);
        reject(e);
      }
    }
    document.addEventListener("kpsdk-load", configureKPSDK, { once: true });
    document.addEventListener("kpsdk-ready", () => run(), { once: true });
    const script = document.createElement("script");
    script.src = cfg.kasadaScript;
    script.onerror = () => { clearTimeout(timeout); reject(new Error("Kasada script load failed")); };
    document.body.appendChild(script);
  });
}
"""


def _launch_browser(playwright):  # noqa: ANN001
    launch_kwargs: dict = {
        "headless": False,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    channels: list[str | None]
    if sys.platform == "win32":
        channels = ["chrome", "msedge", None]
    elif sys.platform == "darwin":
        channels = ["chrome", None]
    else:
        channels = [None]
    last_err: Exception | None = None
    for channel in channels:
        opts = dict(launch_kwargs)
        if channel:
            opts["channel"] = channel
        try:
            return playwright.chromium.launch(**opts), channel or "chromium"
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    raise RuntimeError(
        "Не найден Chrome или Edge. Установите Google Chrome и повторите."
    ) from last_err


def fetch_integrity_via_browser(
    oauth_token: str,
    *,
    client_id: str,
    client_version: str,
    device_id: str,
    session_id: str,
    timeout_sec: float = 90.0,
) -> tuple[str, float]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Нужен пакет playwright (pip install playwright). "
            "На Windows достаточно установленного Chrome."
        ) from exc

    cfg = {
        "clientId": client_id,
        "oauthToken": oauth_token,
        "deviceId": device_id,
        "clientVersion": client_version,
        "clientSessionId": session_id,
        "timeoutMs": int(timeout_sec * 1000),
        "kasadaScript": KASADA_SCRIPT,
    }

    with sync_playwright() as playwright:
        browser, label = _launch_browser(playwright)
        try:
            context = browser.new_context(user_agent=TWITCH_UA, locale="ru-RU")
            context.add_cookies(
                [
                    {
                        "name": "auth-token",
                        "value": oauth_token,
                        "domain": ".twitch.tv",
                        "path": "/",
                        "httpOnly": False,
                        "secure": True,
                        "sameSite": "None",
                    }
                ]
            )
            page = context.new_page()
            page.goto(
                "https://www.twitch.tv/drops/inventory",
                wait_until="domcontentloaded",
                timeout=int(timeout_sec * 1000),
            )
            page.wait_for_timeout(2500)

            for script in (_FETCH_INTEGRITY_JS, _KASADA_INTEGRITY_JS):
                try:
                    payload = page.evaluate(script, cfg)
                    token = str(payload.get("token") or "")
                    if not token:
                        continue
                    expiration = payload.get("expiration")
                    expires = (
                        float(expiration)
                        if isinstance(expiration, (int, float))
                        else time.time() + 600
                    )
                    return token, expires
                except Exception:
                    continue
            raise RuntimeError(
                f"Браузер ({label}) не смог получить Client-Integrity. "
                "Войдите в Twitch в Chrome и повторите."
            )
        finally:
            browser.close()