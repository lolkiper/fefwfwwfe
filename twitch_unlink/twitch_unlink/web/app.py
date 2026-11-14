from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from twitch_unlink.proxy_rotator import parse_proxy_list
from twitch_unlink.web.session import (
    SessionStore,
    serialize_app,
    serialize_org,
    serialize_social,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_PROXY_INTERVAL = 60.0

store = SessionStore()
app = FastAPI(title="Twitch Unlink", version="1.1.0")

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class LoginRequest(BaseModel):
    token: str = Field(..., min_length=1)
    proxies: str = ""
    proxy_interval_sec: float = Field(default=DEFAULT_PROXY_INTERVAL, ge=10, le=600)


class DisconnectRequest(BaseModel):
    session_id: str
    kind: Literal["app", "social", "org"]
    item_id: str


def _require_session(session_id: str):
    session = store.get(session_id)
    if not session:
        raise HTTPException(status_code=401, detail="Сессия истекла. Войдите снова.")
    return session


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/login")
def login(body: LoginRequest):
    proxies = parse_proxy_list(body.proxies)
    try:
        session = store.create(
            body.token,
            proxies,
            proxy_interval_sec=body.proxy_interval_sec,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Ошибка сети или прокси: {exc}") from exc

    return {
        "session_id": session.session_id,
        "login": session.login,
        "user_id": session.user_id,
        "proxy": session.proxy_status(),
    }


@app.get("/api/session/{session_id}")
def session_status(session_id: str):
    session = _require_session(session_id)
    return {
        "login": session.login,
        "user_id": session.user_id,
        "proxy": session.proxy_status(),
    }


@app.get("/api/connections/{session_id}")
def list_connections(session_id: str, q: str = ""):
    session = _require_session(session_id)
    client = session.client
    try:
        apps = [serialize_app(a) for a in client.list_oauth_apps()]
        orgs = [serialize_org(o) for o in client.list_organizations()]
        social_all = [serialize_social(s) for s in client.list_social_links()]
        social = [s for s in social_all if s["linked"]]
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    query = q.strip().lower()
    if query:

        def match_item(item: dict) -> bool:
            name = str(item.get("name", "")).lower()
            item_id = str(item.get("id", "")).lower()
            return query in name or query == item_id or query in item_id

        apps = [a for a in apps if match_item(a)]
        orgs = [o for o in orgs if match_item(o)]
        social = [s for s in social if match_item(s)]

    return {
        "apps": apps,
        "organizations": orgs,
        "social": social,
        "proxy": session.proxy_status(),
    }


@app.post("/api/disconnect")
def disconnect(body: DisconnectRequest):
    session = _require_session(body.session_id)
    client = session.client
    user_id = session.user_id
    try:
        if body.kind == "app":
            client.unlink_oauth_app(body.item_id)
        elif body.kind == "social":
            client.unlink_social(body.item_id, user_id)
        elif body.kind == "org":
            orgs = client.list_organizations()
            org = next((o for o in orgs if o.org_id == body.item_id), None)
            if not org:
                raise HTTPException(status_code=404, detail="Организация не найдена.")
            if not org.can_leave:
                raise HTTPException(
                    status_code=403,
                    detail=f"Нельзя выйти из «{org.name}» (роль: {org.role}).",
                )
            client.leave_organization(body.item_id, user_id)
        else:
            raise HTTPException(status_code=400, detail="Неизвестный тип подключения.")
    except HTTPException:
        raise
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True, "proxy": session.proxy_status()}


@app.delete("/api/session/{session_id}")
def logout(session_id: str):
    store.destroy(session_id)
    return {"ok": True}


def main() -> None:
    import uvicorn

    uvicorn.run(
        "twitch_unlink.web.app:app",
        host="127.0.0.1",
        port=8765,
        reload=False,
    )


if __name__ == "__main__":
    main()