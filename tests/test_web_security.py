from __future__ import annotations

import re
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src.core.config import AppConfig
from src.web.app import create_app


@pytest.fixture
def client(temp_config: AppConfig) -> TestClient:
    app = create_app(temp_config)
    return TestClient(app)


def _get_csrf(client: TestClient, url: str = "/login") -> str:
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


def test_health_no_auth(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_api_key_not_exposed(client: TestClient) -> None:
    response = client.get("/login")
    assert "APIYI_API_KEY" not in response.text
    assert "apiyi" not in response.text.lower()


def test_secret_masking() -> None:
    from src.utils.secrets import mask_message

    msg = "Error: APIYI_API_KEY=sk-abc123def456ghi789"
    masked = mask_message(msg)
    assert "sk-abc123def456ghi789" not in masked


def test_cookie_config() -> None:
    from src.web.auth import get_session_config

    cfg = get_session_config()
    assert cfg["same_site"] == "lax"
    assert cfg["https_only"] is False
    assert cfg["max_age"] == 24 * 3600


def test_session_expiry() -> None:
    from src.web.auth import get_session_config

    cfg = get_session_config()
    assert cfg["max_age"] > 0


def test_path_traversal_sku_rejected(client: TestClient) -> None:
    _login(client)
    token = _get_csrf(client, "/products/new")
    response = client.post(
        "/api/upload/new-product",
        data={
            "sku": "../evil",
            "category": "jacket",
            "color": "black",
            "fabric_name": "ripstop",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code in (400, 403)


def test_upload_invalid_extension(client: TestClient) -> None:
    _login(client)
    token = _get_csrf(client, "/products/new")
    fake = BytesIO(b"not an image")
    response = client.post(
        "/api/upload/new-product",
        data={
            "sku": "TEST-UPLOAD-1",
            "category": "jacket",
            "color": "black",
            "fabric_name": "ripstop",
            "csrf_token": token,
        },
        files={"front": ("evil.txt", fake, "text/plain")},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_upload_fake_image(client: TestClient) -> None:
    _login(client)
    token = _get_csrf(client, "/products/new")
    fake = BytesIO(b"fake image content")
    response = client.post(
        "/api/upload/new-product",
        data={
            "sku": "TEST-UPLOAD-2",
            "category": "jacket",
            "color": "black",
            "fabric_name": "ripstop",
            "csrf_token": token,
        },
        files={"front": ("fake.jpg", fake, "image/jpeg")},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_upload_too_large(client: TestClient) -> None:
    _login(client)
    token = _get_csrf(client, "/products/new")
    import random

    img = Image.new("RGB", (2000, 2000))
    pixels = img.load()
    for i in range(2000):
        for j in range(2000):
            pixels[i, j] = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    assert buf.getbuffer().nbytes > 5 * 1024 * 1024
    response = client.post(
        "/api/upload/new-product",
        data={
            "sku": "TEST-UPLOAD-3",
            "category": "jacket",
            "color": "black",
            "fabric_name": "ripstop",
            "csrf_token": token,
        },
        files={"front": ("large.png", buf, "image/png")},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_upload_success(client: TestClient) -> None:
    _login(client)
    token = _get_csrf(client, "/products/new")
    img = Image.new("RGB", (800, 1000), color="blue")
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    response = client.post(
        "/api/upload/new-product",
        data={
            "sku": "TEST-UPLOAD-4",
            "category": "jacket",
            "color": "black",
            "fabric_name": "ripstop",
            "csrf_token": token,
        },
        files={"front": ("front.png", buf, "image/png")},
        follow_redirects=False,
    )
    assert response.status_code in (200, 409)
