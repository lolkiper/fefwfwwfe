"""GQL-мутации через Chrome/Edge (integrity + запрос в одной сессии браузера)."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

from twitch_unlink.gql_mutations import DELETE_OAUTH_APP
from twitch_unlink.integrity import TWITCH_UA, TwitchIntegrity, fetch_twilight_build_id

KASADA_SCRIPT = (
    "https://k.twitchcdn.net/149e9513-01fa-4fb0-aad4-566afd725d1b/"
    "2d206a39-8ed7-437e-a3be-862e0f06eea3/p.js"
)

STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
if (!window.chrome) { window.chrome = { runtime: {} }; }
"""

_RUN_MUTATION_JS = """
async (cfg) => {
  try { localStorage.setItem('auth-token', cfg.oauthToken); } catch (e) {}

  const baseHeaders = {
    "Client-Id": cfg.clientId,
    "Authorization": "OAuth " + cfg.oauthToken,
    "X-Device-Id": cfg.deviceId,
    "Device-Id": cfg.deviceId,
    "Client-Version": cfg.clientVersion,
    "Client-Session-Id": cfg.clientSessionId,
    "Content-Type": "text/plain;charset=UTF-8",
  };

  async function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

  async function waitKpsdk(maxMs) {
    const t0 = Date.now();
    while (Date.now() - t0 < maxMs) {
      if (window.KPSDK) return true;
      await sleep(200);
    }
    return false;
  }

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

  await waitKpsdk(20000);
  await sleep(cfg.pageWarmupMs || 2000);

  let integ;
  let lastErr;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      if (window.KPSDK) {
        integ = await new Promise((resolve, reject) => {
          const t = setTimeout(() => reject(new Error("kpsdk-ready timeout")), 25000);
          document.addEventListener("kpsdk-ready", async () => {
            try {
              clearTimeout(t);
              resolve(await integrityOnce());
            } catch (e) {
              clearTimeout(t);
              reject(e);
            }
          }, { once: true });
          try {
            window.KPSDK.configure([{
              protocol: "https:",
              method: "POST",
              domain: "gql.twitch.tv",
              path: "/integrity",
            }]);
          } catch (e) {}
        });
      } else {
        try {
          integ = await integrityOnce();
        } catch (e) {
          integ = await integrityWithKasada();
        }
      }
      break;
    } catch (e) {
      lastErr = e;
      await sleep(1500);
    }
  }
  if (!integ) throw lastErr || new Error("integrity failed");

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

_BROWSER_PROFILE = Path(tempfile.gettempdir()) / "TwitchDropsUnlinkChrome"


def _open_persistent_context(playwright):  # noqa: ANN001
    """Chrome/Edge без прокси (прокси ломают integrity)."""
    _BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
    channels: list[str | None] = ["chrome", "msedge", None]
    last_err: Exception | None = None
    for channel in channels:
        kwargs: dict[str, Any] = {
            "user_data_dir": str(_BROWSER_PROFILE),
            "headless": False,
            "ignore_default_args": ["--enable-automation"],
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            "user_agent": TWITCH_UA,
            "locale": "en-US",
            "viewport": {"width": 1400, "height": 900},
        }
        if channel:
            kwargs["channel"] = channel
        try:
            context = playwright.chromium.launch_persistent_context(**kwargs)
            context.add_init_script(STEALTH_INIT)
            return context, channel or "chromium"
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    raise RuntimeError(
        "Не найден Google Chrome или Edge. Установите браузер и повторите."
    ) from last_err


def _auth_cookies(oauth_token: str) -> list[dict[str, Any]]:
    return [
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


def _prepare_cfg(
    oauth_token: str,
    operation_name: str,
    query: str,
    variables: dict[str, Any],
    client_id: str | None,
    integrity: TwitchIntegrity | None,
    timeout_sec: float,
) -> tuple[dict[str, Any], TwitchIntegrity]:
    ig = integrity or TwitchIntegrity(client_id or "kimne78kx3ncx6brgo4mv6wki5h1ko")
    ig.client_version = fetch_twilight_build_id()
    cfg = {
        "clientId": ig.client_id,
        "oauthToken": oauth_token,
        "deviceId": ig.device_id,
        "clientVersion": ig.client_version,
        "clientSessionId": ig.session_id,
        "timeoutMs": int(timeout_sec * 1000),
        "pageWarmupMs": 3000,
        "kasadaScript": KASADA_SCRIPT,
        "operationName": operation_name,
        "query": query,
        "variables": variables,
    }
    return cfg, ig


def run_mutation_in_browser(
    oauth_token: str,
    *,
    operation_name: str,
    query: str,
    variables: dict[str, Any],
    client_id: str | None = None,
    proxy_url: str | None = None,  # noqa: ARG001 — намеренно игнорируем
    integrity: TwitchIntegrity | None = None,
    timeout_sec: float = 150.0,
) -> dict[str, Any]:
    if proxy_url:
        pass  # Chrome всегда без прокси — иначе integrity падает

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Установите playwright: pip install playwright") from exc

    cfg, _ig = _prepare_cfg(
        oauth_token, operation_name, query, variables, client_id, integrity, timeout_sec
    )

    last_error: Exception | None = None
    urls = (
        "https://www.twitch.tv/drops/inventory",
        "https://www.twitch.tv/settings/connections",
    )

    with sync_playwright() as playwright:
        context, label = _open_persistent_context(playwright)
        try:
            for url in urls:
                context.clear_cookies()
                context.add_cookies(_auth_cookies(oauth_token))
                page = context.new_page()
                try:
                    page.goto(url, wait_until="load", timeout=int(timeout_sec * 1000))
                    page.wait_for_timeout(6000)
                    data = page.evaluate(_RUN_MUTATION_JS, cfg)
                    return data
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                finally:
                    page.close()
            raise RuntimeError(
                f"Отвязка в {label} не удалась: {last_error}. "
                "Нажмите «Сброс прокси» (Chrome идёт без прокси). "
                "Не закрывайте окно Chrome до конца операции."
            ) from last_error
        finally:
            context.close()


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
        proxy_url=None,
        integrity=integrity,
    )


class SharedBrowserUnlinker:
    """Один Chrome — тысячи аккаунтов, без лимита."""

    def __init__(self, proxy_url: str | None = None) -> None:
        if proxy_url:
            pass
        self._playwright = None
        self._context = None
        self._game_client_id: str | None = None

    def __enter__(self) -> SharedBrowserUnlinker:
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._context, _ = _open_persistent_context(self._playwright)
        return self

    def __exit__(self, *args: object) -> None:
        if self._context:
            self._context.close()
        if self._playwright:
            self._playwright.stop()

    def unlink_one(self, oauth_token: str, game_client_id: str) -> None:
        assert self._context is not None
        self._game_client_id = game_client_id
        cfg, _ig = _prepare_cfg(
            oauth_token,
            "DeleteOauthApp",
            DELETE_OAUTH_APP,
            {"id": game_client_id},
            "kimne78kx3ncx6brgo4mv6wki5h1ko",
            None,
            150.0,
        )
        self._context.clear_cookies()
        self._context.add_cookies(_auth_cookies(oauth_token))
        page = self._context.new_page()
        try:
            page.goto(
                "https://www.twitch.tv/drops/inventory",
                wait_until="load",
                timeout=150000,
            )
            page.wait_for_timeout(6000)
            page.evaluate(_RUN_MUTATION_JS, cfg)
        finally:
            page.close()