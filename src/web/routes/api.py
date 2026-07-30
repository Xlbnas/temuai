"""API routes for Web UI (calls Core)."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from src.core.config import AppConfig
from src.core.manifest import ManifestManager
from src.core.models import TaskManifest, TaskStatus
from src.core.pipeline import Pipeline
from src.utils.paths import resolve_within, safe_filename
from src.web.auth import get_current_username, validate_csrf_token

router = APIRouter()


def _config(request: Request) -> AppConfig:
    return request.app.state.config


def _candidate_dir(config: AppConfig, sku: str, platform: str, task_id: str) -> Path:
    return (
        config.output_dir
        / safe_filename(sku)
        / safe_filename(platform)
        / "candidates"
        / safe_filename(task_id)
    )


class ValidateRequest(BaseModel):
    sku: str
    platform: str = "temu"


class GenerateRequestBody(BaseModel):
    sku: str
    task: str
    platform: str = "temu"
    model: str | None = None
    count: int = 1


@router.post("/validate")
async def api_validate(
    request: Request,
    body: ValidateRequest,
    username: str = Depends(get_current_username),
) -> dict:
    config = _config(request)
    pipeline = Pipeline(config, live=False)
    return pipeline.validate(body.sku, body.platform)


@router.post("/build-dry-run")
async def api_build_dry_run(
    request: Request,
    sku: str = Form(...),
    platform: str = Form("temu"),
    csrf_token: str = Form(...),
    username: str = Depends(get_current_username),
) -> dict:
    validate_csrf_token(request, csrf_token)
    config = _config(request)
    pipeline = Pipeline(config, live=False)
    try:
        manifest = pipeline.build(sku, platform)
        return {"success": True, "manifest": manifest.model_dump(mode="json")}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/generate")
async def api_generate(
    request: Request,
    body: GenerateRequestBody,
    username: str = Depends(get_current_username),
) -> dict:
    config = _config(request)
    pipeline = Pipeline(config, live=False)
    task_manifest = pipeline.run_task(
        body.sku, body.platform, body.task, model_override=body.model, count=body.count
    )
    return {"success": task_manifest.status != "failed", "task": task_manifest.model_dump(mode="json")}


@router.post("/accept", response_model=None)
async def api_accept(
    request: Request,
    sku: str = Form(...),
    task: str = Form(...),
    candidate: int = Form(...),
    platform: str = Form("temu"),
    csrf_token: str = Form(...),
    username: str = Depends(get_current_username),
):
    """Accept a candidate via the Core pipeline (no Web-layer file handling)."""
    validate_csrf_token(request, csrf_token)
    config = _config(request)
    pipeline = Pipeline(config, live=False)
    try:
        pipeline.accept_candidate(sku, platform, task, candidate)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return RedirectResponse(
        url=f"/products/{safe_filename(sku)}/task/{safe_filename(task)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/reject", response_model=None)
async def api_reject(
    request: Request,
    sku: str = Form(...),
    task: str = Form(...),
    candidate: int = Form(...),
    platform: str = Form("temu"),
    csrf_token: str = Form(...),
    username: str = Depends(get_current_username),
):
    """Reject a candidate: manifest state only, the file is always kept."""
    validate_csrf_token(request, csrf_token)
    config = _config(request)
    pipeline = Pipeline(config, live=False)
    try:
        pipeline.reject_candidate(sku, platform, task, candidate)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return RedirectResponse(
        url=f"/products/{safe_filename(sku)}/task/{safe_filename(task)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/candidates/{sku}/{platform}/{task_id}")
async def list_candidates(
    request: Request,
    sku: str,
    platform: str,
    task_id: str,
    username: str = Depends(get_current_username),
) -> dict:
    config = _config(request)
    manifest_manager = ManifestManager(config.output_dir)
    manifest = manifest_manager.load(sku, platform)
    if not manifest:
        raise HTTPException(status_code=404, detail="Manifest not found")
    task = next((t for t in manifest.tasks if t.task_id == task_id), None)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    candidates = []
    for c in task.candidates:
        candidates.append(
            {
                "index": c.index,
                "filename": c.filename,
                "status": c.status.value,
                "url": f"/api/candidates/{sku}/{platform}/{task_id}/image/{c.filename}",
            }
        )
    return {"task": task_id, "candidates": candidates}


@router.get("/candidates/{sku}/{platform}/{task_id}/image/{filename}", response_model=None)
async def candidate_image(
    request: Request,
    sku: str,
    platform: str,
    task_id: str,
    filename: str,
    username: str = Depends(get_current_username),
):
    """Serve a candidate image with strict path-traversal protection.

    Paths are resolved inside the SKU's candidate directory only; anything
    escaping that directory (e.g. "../") is rejected.
    """
    from fastapi.responses import FileResponse

    config = _config(request)
    try:
        cand_dir = resolve_within(config.output_dir, safe_filename(sku), safe_filename(platform), "candidates", safe_filename(task_id))
        file_path = resolve_within(cand_dir, Path(filename).name)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(file_path)
