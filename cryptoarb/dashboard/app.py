"""FastAPI-приложение дашборда: отдаёт страницу и пушит снапшот движка
по WebSocket раз в секунду."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def create_app(engine) -> FastAPI:
    app = FastAPI(title="CryptoArbitrage Dashboard")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        # Новый API starlette (>=0.29): request первым, keyword-аргументы
        return templates.TemplateResponse(request=request, name="index.html")

    @app.get("/favicon.ico")
    async def favicon():
        f = STATIC_DIR / "favicon.ico"
        if f.exists():
            return Response(content=f.read_bytes(), media_type="image/x-icon")
        return Response(status_code=204)

    @app.get("/health")
    async def health():
        return {"ok": True, "running": engine is not None}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        try:
            while True:
                try:
                    snap = engine.snapshot() if engine else {"error": "engine not ready"}
                except Exception as e:
                    snap = {"error": str(e)}
                await websocket.send_json(snap)
                await asyncio.sleep(1.0)
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    return app
