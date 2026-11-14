"""GQL-мутации через Chrome/Edge (integrity + запрос в одной сессии браузера)."""

from __future__ import annotations

import sys
import time
from typing import Any

from twitch_unlink.gql_mutations import DELETE_OAUTH_APP
from twitch_unlink.integrity import TWITCH_UA, TwitchIntegrity, fetch_twilight_build_id

KASADA_SCRIPT = (
    "https://k.twitchcdn.net/149e9513-01fa-4fb0-aad4-566afd725d1b/"
    "2d206a39-8ed7-437e-a3be-862e0f06eea3/p.js"
)

_RUN_MUTATION_JS = """
async (cfg) => {
  const baseHeaders = {
    "Client-Id": cfg.clientId,
    "Authorization": "OAuth " + cfg.oauthToken,
    "X-Device-Id": cfg.deviceId,
    "Device-Id": cfg.deviceId,
    "Client-Version": cfg.clientVersion,
    "Client-Session-Id": cfg.clientSessionId,
    "Content-Type": "text/plain;charset=UTF-8",
  };

  async function integrityOnce() {
    const resp = await fetch("https://gql.twitch.tv/integrity", {
      method: "POST",
      headers: baseHeaders,
      credentials: "include",
      body: null,
    });
    if (!resp.ok) throw new Error("integrity HTTP " + resp.status);
    return await resp.json();
  }

  async function integrityWithKasada() {
    return await new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error("Kasada timeout")), cfg.timeoutMs);
      const configure = () => {
        try {
          window.KPSDK.configure([{
            protocol: "https:",
            method: "POST",
            domain: "gql.twitch.tv",
            path: "/integrity",
          }]);
        } catch (e) {}
      };
      const run = async () => {
        try {
          clearTimeout(t);
          resolve(await integrityOnce());
        } catch (e) {
          clearTimeout(t);
          reject(e);
        }
      };
      document.addEventListener("kpsdk-load", configure, { once: true });
      document.addEventListener("kpsdk-ready", run, { once: true });
      const s = document.createElement("script");
      s.src = cfg.kasadaScript;
      s.onerror = () => { clearTimeout(t); reject(new Error("kasada script")); };
      document.body.appendChild(s);
    });
  }

  let integ;
  try {
    integ = await integrityOnce();
  } catch (e) {
    integ = await integrityWithKasada();
  }
  const token = integ.token;
  if (!token) throw new Error("no integrity token");

  const gqlHeaders = { ...baseHeaders, "Client-Integrity": token };
  const payload = [{
    operationName: cfg.operationName,
    variables: cfg.variables,
    query: cfg.query,
  }];
  const gql = await fetch("https://gql.twitch.tv/gql", {
    method: "POST",
    headers: gqlHeaders,
    credentials: "include",
    body: JSON.stringify(payload),
  });
  const text = await gql.text();
  let body;
  try {
    body = JSON.parse(text);
  } catch (e) {
    throw new Error("gql parse: " + text.slice(0, 200));
  }
  const block = Array.isArray(body) ? body[0] : body;
  if (block.errors && block.errors.length) {
    throw new Error(block.errors.map((e) => e.message || String(e)).join("; "));
  }
  return block.data || {};
}
"""


def _parse_playwright_proxy(proxy_url: str | None) -> dict[str, str] | None:
    if not proxy_url:
        return None
    return {"server": proxy_url}


def _launch_browser(playwright):  # noqa: ANN001
    launch_kwargs: dict = {
        "headless": False,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    }
    channels: list[str | None]
    if sys.platform == "win32":
        channels = ["chrome", "msedge", None]
    else:
        channels = ["chrome", "msedge", None]
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
        "Не найден Google Chrome или Microsoft Edge. Установите браузер и повторите."
    ) from last_err


def _browser_context(playwright, proxy_url: str | None):  # noqa: ANN001
    browser, label = _launch_browser(playwright)
    context = browser.new_context(
        user_agent=TWITCH_UA,
        locale="ru-RU",
        proxy=_parse_playwright_proxy(proxy_url),
    )
    return browser, context, label


def run_mutation_in_browser(
    oauth_token: str,
    *,
    operation_name: str,
    query: str,
    variables: dict[str, Any],
    client_id: str | None = None,
    proxy_url: str | None = None,
    integrity: TwitchIntegrity | None = None,
    timeout_sec: float = 120.0,
) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Установите playwright: pip install playwright") from exc

    ig = integrity or TwitchIntegrity(client_id or "kimne78kx3ncx6brgo4mv6wki5h1ko")
    ig.client_version = fetch_twilight_build_id()

    cfg = {
        "clientId": ig.client_id,
        "oauthToken": oauth_token,
        "deviceId": ig.device_id,
        "clientVersion": ig.client_version,
        "clientSessionId": ig.session_id,
        "timeoutMs": int(timeout_sec * 1000),
        "kasadaScript": KASADA_SCRIPT,
        "operationName": operation_name,
        "query": query,
        "variables": variables,
    }

    last_error: Exception | None = None
    urls = (
        "https://www.twitch.tv/drops/inventory",
        "https://www.twitch.tv/settings/connections",
    )

    with sync_playwright() as playwright:
        browser, context, label = _browser_context(playwright, proxy_url)
        try:
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
            for url in urls:
                page = context.new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=int(timeout_sec * 1000))
                    page.wait_for_timeout(4000)
                    data = page.evaluate(_RUN_MUTATION_JS, cfg)
                    return data
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                finally:
                    page.close()
            raise RuntimeError(
                f"Не удалось выполнить запрос в {label}: {last_error}"
            ) from last_error
        finally:
            browser.close()


def fetch_integrity_via_browser(
    oauth_token: str,
    *,
    client_id: str,
    client_version: str,
    device_id: str,
    session_id: str,
    proxy_url: str | None = None,
    timeout_sec: float = 90.0,
) -> tuple[str, float]:
    """Только токен integrity (для совместимости; отвязка идёт через run_mutation_in_browser)."""
    cfg = {
        "clientId": client_id,
        "oauthToken": oauth_token,
        "deviceId": device_id,
        "clientVersion": client_version,
        "clientSessionId": session_id,
        "timeoutMs": int(timeout_sec * 1000),
        "kasadaScript": KASADA_SCRIPT,
        "operationName": "ViewCount",
        "query": "query ViewCount { __typename }",
        "variables": {},
    }
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Установите playwright") from exc

    with sync_playwright() as playwright:
        browser, context, _label = _browser_context(playwright, proxy_url)
        try:
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
            page.wait_for_timeout(3500)
            page.evaluate(_RUN_MUTATION_JS, cfg)
        finally:
            browser.close()
    return "", time.time() + 600


def unlink_drop_game_in_browser(
    oauth_token: str,
    game_client_id: str,
    *,
    twitch_client_id: str = "kimne78kx3ncx6brgo4mv6wki5h1ko",
    proxy_url: str | None = None,
    integrity: TwitchIntegrity | None = None,
) -> None:
    run_mutation_in_browser(
        oauth_token,
        operation_name="DeleteOauthApp",
        query=DELETE_OAUTH_APP,
        variables={"id": game_client_id},
        client_id=twitch_client_id,
        proxy_url=proxy_url,
        integrity=integrity,
    )


class SharedBrowserUnlinker:
    """Один браузер — много аккаунтов (массовая отвязка без лимита)."""

    def __init__(self, proxy_url: str | None = None) -> None:
        self._proxy_url = proxy_url
        self._playwright = None
        self._browser = None
        self._integrity = TwitchIntegrity("kimne78kx3ncx6brgo4mv6wki5h1ko")

    def __enter__(self) -> SharedBrowserUnlinker:
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser, _, _ = _launch_browser(self._playwright)
        return self

    def __exit__(self, *args: object) -> None:
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

    def unlink_one(self, oauth_token: str, game_client_id: str) -> None:
        assert self._browser is not None
        context = self._browser.new_context(
            user_agent=TWITCH_UA,
            locale="ru-RU",
            proxy=_parse_playwright_proxy(self._proxy_url),
        )
        try:
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
            ig = TwitchIntegrity(self._integrity.client_id)
            cfg = {
                "clientId": ig.client_id,
                "oauthToken": oauth_token,
                "deviceId": ig.device_id,
                "clientVersion": fetch_twilight_build_id(),
                "clientSessionId": ig.session_id,
                "timeoutMs": 120000,
                "kasadaScript": KASADA_SCRIPT,
                "operationName": "DeleteOauthApp",
                "query": DELETE_OAUTH_APP,
                "variables": {"id": game_client_id},
            }
            page.goto(
                "https://www.twitch.tv/drops/inventory",
                wait_until="domcontentloaded",
                timeout=120000,
            )
            page.wait_for_timeout(3500)
            page.evaluate(_RUN_MUTATION_JS, cfg)
        finally:
            context.close()