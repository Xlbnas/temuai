from __future__ import annotations

import re
from typing import Generator

import pytest
from fastapi.testclient import TestClient

from src.core.config import AppConfig
from src.web.app import create_app


@pytest.fixture
def client(temp_config: AppConfig) -> Generator[TestClient, None, None]:
    app = create_app(temp_config)
    with TestClient(app) as c:
        yield c


def _get_csrf(client: TestClient, url: str = "/login") -> str:
    response = client.get(url)
    m = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert m
    return m.group(1)


def test_unauthenticated_redirect(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/login"


def test_login_page(client: TestClient) -> None:
    response = client.get("/login")
    assert response.status_code == 200
    assert "Login" in response.text
    assert "csrf_token" in response.text


def test_login_success(client: TestClient) -> None:
    token = _get_csrf(client, "/login")
    response = client.post(
        "/login",
        data={"username": "admin", "password": "test-password", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/"


def test_login_failure(client: TestClient) -> None:
    token = _get_csrf(client, "/login")
    response = client.post(
        "/login",
        data={"username": "admin", "password": "wrong-password", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert "Invalid username or password" in response.text


def test_login_csrf_protection(client: TestClient) -> None:
    response = client.post(
        "/login",
        data={"username": "admin", "password": "test-password", "csrf_token": "invalid"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_logout(client: TestClient) -> None:
    token = _get_csrf(client, "/login")
    response = client.post(
        "/login",
        data={"username": "admin", "password": "test-password", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 302
    token = _get_csrf(client, "/")
    response = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
