"""Tests for Web candidate Accept/Reject and image path safety."""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.core.config import AppConfig
from src.core.manifest import ManifestManager
from src.core.models import TaskStatus
from src.core.pipeline import Pipeline
from src.utils.paths import resolve_within
from src.web.app import create_app

TASK_ID = "02_model_front"


@pytest.fixture
def client(temp_config: AppConfig) -> TestClient:
    return TestClient(create_app(temp_config))


@pytest.fixture
def sku_with_candidates(temp_config: AppConfig, sample_sku: str) -> str:
    """Generate dry-run candidates for one task and persist the manifest."""
    pipeline = Pipeline(temp_config, live=False)
    task_manifest = pipeline.run_task(sample_sku, "temu", TASK_ID, count=3)
    assert task_manifest.status == TaskStatus.GENERATED
    pipeline.manifest_manager.update_task(sample_sku, "temu", task_manifest)
    return sample_sku


def _get_csrf(client: TestClient, url: str) -> str:
    response = client.get(url)
    m = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert m
    return m.group(1)


def _login(client: TestClient) -> None:
    token = _get_csrf(client, "/login")
    response = client.post(
        "/login",
        data={"username": "admin", "password": "test-password", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 302


def _load_manifest(config: AppConfig, sku: str):
    return ManifestManager(config.output_dir).load(sku, "temu")


# ---------------- Accept / Reject ----------------

def test_task_candidates_page(
    client: TestClient, temp_config: AppConfig, sku_with_candidates: str
) -> None:
    _login(client)
    sku = sku_with_candidates
    response = client.get(f"/products/{sku}/task/{TASK_ID}")
    assert response.status_code == 200
    assert "Candidate 1" in response.text
    assert "Candidate 3" in response.text
    assert 'action="/api/accept"' in response.text
    assert 'action="/api/reject"' in response.text


def test_candidate_accept(
    client: TestClient, temp_config: AppConfig, sku_with_candidates: str
) -> None:
    _login(client)
    sku = sku_with_candidates
    token = _get_csrf(client, f"/products/{sku}/task/{TASK_ID}")
    response = client.post(
        "/api/accept",
        data={
            "sku": sku,
            "task": TASK_ID,
            "candidate": "2",
            "platform": "temu",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    manifest = _load_manifest(temp_config, sku)
    task = next(t for t in manifest.tasks if t.task_id == TASK_ID)
    assert task.status == TaskStatus.ACCEPTED
    assert task.accepted_candidate == 2
    by_index = {c.index: c for c in task.candidates}
    assert by_index[2].status == TaskStatus.ACCEPTED
    # Other candidates are kept and stay pending (not auto-rejected)
    assert by_index[1].status == TaskStatus.PENDING
    assert by_index[3].status == TaskStatus.PENDING
    # Final image copied, all candidate files kept on disk
    assert (temp_config.output_dir / sku / "temu" / f"{TASK_ID}.png").exists()
    for c in task.candidates:
        assert Path(c.path).exists()

    # Page now marks the accepted candidate as the current final image
    page = client.get(f"/products/{sku}/task/{TASK_ID}")
    assert "Accepted (current final)" in page.text


def test_candidate_reject_keeps_file(
    client: TestClient, temp_config: AppConfig, sku_with_candidates: str
) -> None:
    _login(client)
    sku = sku_with_candidates
    token = _get_csrf(client, f"/products/{sku}/task/{TASK_ID}")
    response = client.post(
        "/api/reject",
        data={
            "sku": sku,
            "task": TASK_ID,
            "candidate": "1",
            "platform": "temu",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    manifest = _load_manifest(temp_config, sku)
    task = next(t for t in manifest.tasks if t.task_id == TASK_ID)
    cand = next(c for c in task.candidates if c.index == 1)
    assert cand.status == TaskStatus.REJECTED
    # File is NOT deleted
    assert Path(cand.path).exists()


def test_candidate_reject_accepted_fails(
    client: TestClient, temp_config: AppConfig, sku_with_candidates: str
) -> None:
    _login(client)
    sku = sku_with_candidates
    token = _get_csrf(client, f"/products/{sku}/task/{TASK_ID}")
    client.post(
        "/api/accept",
        data={"sku": sku, "task": TASK_ID, "candidate": "1", "platform": "temu", "csrf_token": token},
        follow_redirects=False,
    )
    token = _get_csrf(client, f"/products/{sku}/task/{TASK_ID}")
    response = client.post(
        "/api/reject",
        data={"sku": sku, "task": TASK_ID, "candidate": "1", "platform": "temu", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_accept_requires_csrf(
    client: TestClient, temp_config: AppConfig, sku_with_candidates: str
) -> None:
    _login(client)
    response = client.post(
        "/api/accept",
        data={
            "sku": sku_with_candidates,
            "task": TASK_ID,
            "candidate": "1",
            "platform": "temu",
            "csrf_token": "wrong",
        },
    )
    assert response.status_code == 403


# ---------------- Image serving / path traversal ----------------

def test_candidate_image_served(
    client: TestClient, temp_config: AppConfig, sku_with_candidates: str
) -> None:
    _login(client)
    sku = sku_with_candidates
    manifest = _load_manifest(temp_config, sku)
    task = next(t for t in manifest.tasks if t.task_id == TASK_ID)
    filename = task.candidates[0].filename
    response = client.get(f"/api/candidates/{sku}/temu/{TASK_ID}/image/{filename}")
    assert response.status_code == 200
    assert len(response.content) > 0


def test_candidate_image_traversal_rejected(
    client: TestClient, temp_config: AppConfig, sku_with_candidates: str
) -> None:
    _login(client)
    sku = sku_with_candidates
    # Literal ".." is normalized away by the HTTP client itself; the encoded
    # form reaches the server and must be rejected by safe path resolution.
    response = client.get(f"/api/candidates/{sku}/temu/{TASK_ID}/image/%2E%2E")
    assert response.status_code in (400, 404)
    response = client.get(f"/api/candidates/{sku}/temu/{TASK_ID}/image/..%2Fmanifest.json")
    assert response.status_code in (400, 404)


def test_resolve_within_blocks_traversal(tmp_path: Path) -> None:
    base = tmp_path / "candidates"
    base.mkdir()
    assert resolve_within(base, "a.png") == (base / "a.png").resolve()
    with pytest.raises(ValueError):
        resolve_within(base, "..", "secret.txt")
    with pytest.raises(ValueError):
        resolve_within(base, "sub", "..", "..", "secret.txt")


# ---------------- Session contents ----------------

def test_session_cookie_contains_no_sensitive_fields(client: TestClient) -> None:
    """Session is a signed client-side cookie; it must only carry identity state."""
    _login(client)
    cookie = client.cookies.get("tif_session")
    assert cookie
    payload_b64 = cookie.split(".")[0]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    assert payload.get("user") == "admin"
    blob = json.dumps(payload).lower()
    for forbidden in ("password", "session_secret", "apiyi", "api_key", "secret"):
        assert forbidden not in blob
