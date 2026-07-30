"""Web page routes."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from src.core.config import AppConfig
from src.core.manifest import ManifestManager
from src.core.pipeline import Pipeline
from src.web.auth import (
    generate_csrf_token,
    get_client_ip,
    get_current_username,
    login_rate_limiter,
    login_user,
    logout_user,
    validate_csrf_token,
    verify_password,
)

router = APIRouter()


def _templates(request: Request):
    return request.app.state.templates


def _config(request: Request) -> AppConfig:
    return request.app.state.config


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    if request.session.get("user"):
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    templates = _templates(request)
    token = generate_csrf_token(request)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"request": request, "csrf_token": token, "error": None},
    )


@router.post("/login", response_model=None)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
):
    validate_csrf_token(request, csrf_token)
    templates = _templates(request)

    expected_username = os.getenv("APP_USERNAME", "")
    expected_hash = os.getenv("APP_PASSWORD_HASH", "")
    client_ip = get_client_ip(request)

    if not expected_username or not expected_hash:
        token = generate_csrf_token(request)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "request": request,
                "csrf_token": token,
                "error": "Authentication is not configured on server.",
            },
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    if login_rate_limiter.is_locked(client_ip):
        token = generate_csrf_token(request)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "request": request,
                "csrf_token": token,
                "error": "Too many failed login attempts. Please try again later.",
            },
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    if username != expected_username or not verify_password(password, expected_hash):
        login_rate_limiter.record_failure(client_ip)
        token = generate_csrf_token(request)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request, "csrf_token": token, "error": "Invalid username or password."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    login_rate_limiter.record_success(client_ip)
    login_user(request, username)
    return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)


@router.post("/logout")
async def logout(request: Request, csrf_token: str = Form(...)) -> RedirectResponse:
    validate_csrf_token(request, csrf_token)
    logout_user(request)
    return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    username: str = Depends(get_current_username),
) -> HTMLResponse:
    templates = _templates(request)
    config = _config(request)
    sku_list = []
    input_dir = config.input_dir
    if input_dir.exists():
        for d in sorted(input_dir.iterdir()):
            if d.is_dir() and (d / "product.yaml").exists():
                sku_list.append(_build_sku_summary(config, d.name))
    token = generate_csrf_token(request)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"request": request, "sku_list": sku_list, "csrf_token": token, "user": username},
    )


def _build_sku_summary(config: AppConfig, sku: str) -> dict:
    manifest_manager = ManifestManager(config.output_dir)
    manifest = manifest_manager.load(sku, "temu")
    output_dir = config.output_dir / sku / "temu"
    has_output = output_dir.exists() and any(output_dir.iterdir())
    return {
        "sku": sku,
        "platform": "temu",
        "valid": manifest is not None or (config.input_dir / sku / "product.yaml").exists(),
        "last_build": manifest.created_at if manifest else None,
        "estimated_cost": manifest.total_estimated_cost_usd if manifest else 0.0,
        "has_output": has_output,
    }


@router.get("/products/new", response_class=HTMLResponse)
async def new_product_page(
    request: Request,
    username: str = Depends(get_current_username),
) -> HTMLResponse:
    templates = _templates(request)
    token = generate_csrf_token(request)
    return templates.TemplateResponse(
        request,
        "new_product.html",
        {"request": request, "csrf_token": token, "user": username},
    )


@router.get("/products/{sku}/task/{task_id}", response_class=HTMLResponse)
async def task_candidates_page(
    request: Request,
    sku: str,
    task_id: str,
    username: str = Depends(get_current_username),
) -> HTMLResponse:
    templates = _templates(request)
    config = _config(request)
    manifest_manager = ManifestManager(config.output_dir)
    manifest = manifest_manager.load(sku, "temu")
    task = None
    if manifest:
        task = next((t for t in manifest.tasks if t.task_id == task_id), None)
    if task is None:
        return templates.TemplateResponse(
            request,
            "task_candidates.html",
            {
                "request": request,
                "sku": sku,
                "task_id": task_id,
                "task": None,
                "candidates": [],
                "csrf_token": generate_csrf_token(request),
                "user": username,
            },
            status_code=status.HTTP_404_NOT_FOUND,
        )
    candidates = [
        {
            "index": c.index,
            "filename": c.filename,
            "status": c.status.value,
            "url": f"/api/candidates/{sku}/temu/{task_id}/image/{c.filename}",
        }
        for c in task.candidates
    ]
    token = generate_csrf_token(request)
    return templates.TemplateResponse(
        request,
        "task_candidates.html",
        {
            "request": request,
            "sku": sku,
            "task_id": task_id,
            "task": task,
            "candidates": candidates,
            "csrf_token": token,
            "user": username,
        },
    )


@router.get("/products/{sku}", response_class=HTMLResponse)
async def sku_detail(
    request: Request,
    sku: str,
    username: str = Depends(get_current_username),
) -> HTMLResponse:
    templates = _templates(request)
    config = _config(request)
    pipeline = Pipeline(config, live=False)
    validation = pipeline.validate(sku, "temu")
    manifest_manager = ManifestManager(config.output_dir)
    manifest = manifest_manager.load(sku, "temu")
    token = generate_csrf_token(request)
    return templates.TemplateResponse(
        request,
        "sku_detail.html",
        {
            "request": request,
            "sku": sku,
            "validation": validation,
            "manifest": manifest,
            "csrf_token": token,
            "user": username,
        },
    )
