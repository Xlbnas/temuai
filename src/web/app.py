"""FastAPI application for TEMU Image Factory Web UI."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.core.config import AppConfig, get_config
from src.utils.secrets import mask_message
from src.web.auth import add_session_middleware, get_current_username
from src.web.routes import api, pages, studio, upload


def create_app(config: AppConfig | None = None) -> FastAPI:
    app = FastAPI(title="TEMU Image Factory", version="0.1.0")
    add_session_middleware(app)

    templates_dir = Path(__file__).parent / "templates"
    static_dir = Path(__file__).parent / "static"
    templates_dir.mkdir(parents=True, exist_ok=True)
    static_dir.mkdir(parents=True, exist_ok=True)

    templates = Jinja2Templates(directory=str(templates_dir))
    app.state.templates = templates
    app.state.config = config or get_config()

    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(pages.router)
    app.include_router(api.router, prefix="/api")
    app.include_router(upload.router, prefix="/api/upload")
    app.include_router(studio.router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={"detail": mask_message(str(exc))},
        )

    @app.exception_handler(KeyError)
    async def key_error_handler(request: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": "Resource not found"})

    return app


app = create_app()
