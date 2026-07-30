"""Authenticated Studio Web UI; business changes are delegated to StudioService."""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from src.studio.analyzers import MockAssetAnalyzer
from src.studio.models import ContentKind, Importance, NormalizedBBox, SourceKind, StudioPlatform
from src.studio.service import StudioService
from src.web.auth import generate_csrf_token, get_current_username, validate_csrf_token

router = APIRouter()


def _service(request: Request) -> StudioService:
    return StudioService(request.app.state.config)


def _view(request: Request, template: str, **context: Any) -> HTMLResponse:
    context.setdefault("csrf_token", generate_csrf_token(request))
    context.setdefault("request", request)
    return cast(
        HTMLResponse, request.app.state.templates.TemplateResponse(request, template, context)
    )


@router.get("/studio", response_class=HTMLResponse)
async def projects(request: Request, username: str = Depends(get_current_username)) -> HTMLResponse:
    return _view(
        request,
        "studio_projects.html",
        projects=_service(request).list_projects(),
        user=username,
        error=None,
    )


@router.post("/studio/projects", response_model=None)
async def create_project(
    request: Request,
    name: str = Form(...),
    target_platform: StudioPlatform = Form(...),  # noqa: B008
    csrf_token: str = Form(...),
    username: str = Depends(get_current_username),
) -> RedirectResponse | HTMLResponse:
    validate_csrf_token(request, csrf_token)
    try:
        project = _service(request).create_project(name, target_platform)
    except ValueError as exc:
        return _view(
            request,
            "studio_projects.html",
            projects=_service(request).list_projects(),
            user=username,
            error=str(exc),
        )
    return RedirectResponse(f"/studio/{project.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/studio/{project_id}", response_class=HTMLResponse)
async def project_detail(
    request: Request, project_id: str, username: str = Depends(get_current_username)
) -> HTMLResponse:
    try:
        record = _service(request).get_record(project_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Studio project not found")
    bundle = _service(request).compile_reference_bundle(project_id) if record.assets else None
    return _view(
        request,
        "studio_project.html",
        record=record,
        bundle=bundle,
        packs=_service(request).style_packs(record.project.target_platform),
        user=username,
        error=None,
    )


@router.post("/studio/{project_id}/assets", response_model=None)
async def import_assets(
    request: Request,
    project_id: str,
    files: list[UploadFile] = File(...),  # noqa: B008
    csrf_token: str = Form(...),
    username: str = Depends(get_current_username),
) -> RedirectResponse | HTMLResponse:
    validate_csrf_token(request, csrf_token)
    if len(files) > 20:
        raise HTTPException(status_code=400, detail="Upload at most 20 images per request")
    max_bytes = int(request.app.state.config.safe_env("MAX_UPLOAD_MB", "30") or "30") * 1024 * 1024
    service = _service(request)
    try:
        for upload in files:
            service.import_asset(project_id, upload.filename or "", await upload.read(), max_bytes)
    except ValueError as exc:
        record = service.get_record(project_id)
        return _view(
            request,
            "studio_project.html",
            record=record,
            bundle=None,
            packs=service.style_packs(record.project.target_platform),
            user=username,
            error=str(exc),
        )
    return RedirectResponse(f"/studio/{project_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/studio/{project_id}/assets/{asset_id}/analyze", response_model=None)
async def analyze(
    request: Request,
    project_id: str,
    asset_id: str,
    use_mock: Annotated[bool, Form()] = False,
    csrf_token: str = Form(...),
    username: str = Depends(get_current_username),
) -> RedirectResponse:
    validate_csrf_token(request, csrf_token)
    if not use_mock:
        raise HTTPException(status_code=400, detail="M1 analysis requires explicit offline mock mode")
    _service(request).analyze_asset(project_id, asset_id, analyzer=MockAssetAnalyzer())
    return RedirectResponse(f"/studio/{project_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/studio/{project_id}/assets/{asset_id}/classification", response_model=None)
async def update_classification(
    request: Request,
    project_id: str,
    asset_id: str,
    source_kind: SourceKind = Form(...),  # noqa: B008
    content_kind: ContentKind = Form(...),  # noqa: B008
    detail_types: str = Form(...),
    csrf_token: str = Form(...),
    username: str = Depends(get_current_username),
) -> RedirectResponse:
    validate_csrf_token(request, csrf_token)
    _service(request).update_analysis(
        project_id, asset_id, source_kind, content_kind, detail_types.split(",")
    )
    return RedirectResponse(f"/studio/{project_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/studio/{project_id}/assets/{asset_id}/regions/{region_id}", response_model=None)
async def update_region(
    request: Request,
    project_id: str,
    asset_id: str,
    region_id: str,
    detail_type: str = Form(...),
    importance: Importance = Form(...),  # noqa: B008
    label: str = Form(...),
    bbox_x: float | None = Form(None),
    bbox_y: float | None = Form(None),
    bbox_width: float | None = Form(None),
    bbox_height: float | None = Form(None),
    confirmed: Annotated[bool, Form()] = False,
    delete: Annotated[bool, Form()] = False,
    csrf_token: str = Form(...),
    username: str = Depends(get_current_username),
) -> RedirectResponse:
    validate_csrf_token(request, csrf_token)
    bbox_values = (bbox_x, bbox_y, bbox_width, bbox_height)
    if any(value is not None for value in bbox_values) and any(value is None for value in bbox_values):
        raise HTTPException(status_code=400, detail="Provide all four normalized bbox values")
    bbox = None
    if all(value is not None for value in bbox_values):
        bbox = NormalizedBBox(
            x=cast(float, bbox_x),
            y=cast(float, bbox_y),
            width=cast(float, bbox_width),
            height=cast(float, bbox_height),
        )
    _service(request).update_region(
        project_id,
        asset_id,
        region_id,
        detail_type=detail_type,
        importance=importance,
        label=label,
        confirmed=confirmed,
        bbox=bbox,
        delete=delete,
    )
    return RedirectResponse(f"/studio/{project_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/studio/{project_id}/assets/{asset_id}/render", response_model=None)
async def render_annotation(
    request: Request,
    project_id: str,
    asset_id: str,
    csrf_token: str = Form(...),
    username: str = Depends(get_current_username),
) -> RedirectResponse:
    validate_csrf_token(request, csrf_token)
    _service(request).render_annotations(project_id, asset_id)
    return RedirectResponse(f"/studio/{project_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/studio/{project_id}/spec", response_model=None)
async def product_spec(
    request: Request,
    project_id: str,
    csrf_token: str = Form(...),
    username: str = Depends(get_current_username),
) -> RedirectResponse:
    validate_csrf_token(request, csrf_token)
    _service(request).compile_product_spec(project_id)
    return RedirectResponse(f"/studio/{project_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/studio/{project_id}/style", response_model=None)
async def style(
    request: Request,
    project_id: str,
    style_pack_id: str = Form(...),
    csrf_token: str = Form(...),
    username: str = Depends(get_current_username),
) -> RedirectResponse:
    validate_csrf_token(request, csrf_token)
    _service(request).select_style_pack(project_id, style_pack_id)
    return RedirectResponse(f"/studio/{project_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/studio/{project_id}/assets/{asset_id}/{variant}")
async def asset_image(
    request: Request,
    project_id: str,
    asset_id: str,
    variant: str,
    username: str = Depends(get_current_username),
) -> FileResponse:
    try:
        return FileResponse(_service(request).resolve_asset_path(project_id, asset_id, variant))
    except (KeyError, FileNotFoundError):
        raise HTTPException(status_code=404, detail="Studio asset not found")


@router.get("/studio/{project_id}/reference-board")
async def reference_board(
    request: Request, project_id: str, username: str = Depends(get_current_username)
) -> FileResponse:
    try:
        return FileResponse(_service(request).reference_board_path(project_id))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Reference board not found")
