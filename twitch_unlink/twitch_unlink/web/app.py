from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from twitch_unlink.proxy_rotator import parse_proxy_list
from twitch_unlink.web.session import SessionStore, serialize_drop_game

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_PROXY_INTERVAL = 60.0

store = SessionStore()
app = FastAPI(title="Twitch Drops Unlink", version="1.2.0")

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class LoginRequest(BaseModel):
    token: str = Field(..., min_length=1)
    proxies: str = ""
    proxy_interval_sec: float = Field(default=DEFAULT_PROXY_INTERVAL, ge=10, le=600)


class DisconnectRequest(BaseModel):
    session_id: str
    client_id: str


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


@app.get("/api/drop-games/{session_id}")
def list_drop_games(session_id: str, q: str = ""):
    session = _require_session(session_id)
    client = session.client
    try:
        games = [serialize_drop_game(g) for g in client.list_drop_game_links()]
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    query = q.strip().lower()
    if query:
        games = [
            g
            for g in games
            if query in g["name"].lower()
            or query in g["client_id"].lower()
            or query == g["game_id"].lower()
        ]

    return {"games": games, "proxy": session.proxy_status()}


@app.post("/api/disconnect")
def disconnect(body: DisconnectRequest):
    session = _require_session(body.session_id)
    try:
        session.client.unlink_drop_game(body.client_id)
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