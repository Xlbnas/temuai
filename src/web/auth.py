"""Web authentication, sessions, CSRF and rate limiting."""
from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from typing import Any

import argon2
from fastapi import HTTPException, Request, status
from starlette.middleware.sessions import SessionMiddleware


def _get_password_hasher() -> argon2.PasswordHasher:
    return argon2.PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1)


def verify_password(password: str, hash_value: str) -> bool:
    """Verify password against Argon2id hash."""
    if not hash_value:
        return False
    hasher = _get_password_hasher()
    try:
        hasher.verify(hash_value, password)
        return True
    except argon2.exceptions.VerifyMismatchError:
        return False
    except Exception:
        return False


def get_session_config() -> dict[str, Any]:
    secret = os.getenv("SESSION_SECRET", "")
    if not secret:
        raise RuntimeError("SESSION_SECRET environment variable is required for web sessions")
    max_age_hours = int(os.getenv("SESSION_MAX_AGE_HOURS", "24"))
    secure = os.getenv("COOKIE_SECURE", "false").lower() in ("true", "1", "yes")
    return {
        "secret_key": secret,
        "max_age": max_age_hours * 3600,
        "same_site": "lax",
        "https_only": secure,
        "session_cookie": "tif_session",
    }


def add_session_middleware(app: Any) -> None:
    cfg = get_session_config()
    app.add_middleware(
        SessionMiddleware,
        secret_key=cfg["secret_key"],
        max_age=cfg["max_age"],
        same_site=cfg["same_site"],
        https_only=cfg["https_only"],
        session_cookie=cfg["session_cookie"],
    )


def get_current_username(request: Request) -> str:
    """Dependency: require authenticated user."""
    user = request.session.get("user")
    if not user:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/login"},
        )
    return user


def login_user(request: Request, username: str) -> None:
    request.session["user"] = username
    request.session["login_at"] = int(time.time())


def logout_user(request: Request) -> None:
    request.session.clear()


def generate_csrf_token(request: Request) -> str:
    token = secrets.token_urlsafe(32)
    request.session["csrf_token"] = token
    return token


def validate_csrf_token(request: Request, token: str | None) -> None:
    expected = request.session.get("csrf_token")
    if not expected or not token or not secrets.compare_digest(expected, token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")


@dataclass
class RateLimitEntry:
    failures: int
    locked_until: float


class LoginRateLimiter:
    """Simple in-memory rate limiter for login attempts."""

    MAX_FAILURES = 5
    LOCKOUT_SECONDS = 300

    def __init__(self) -> None:
        self._store: dict[str, RateLimitEntry] = {}

    def is_locked(self, identifier: str) -> bool:
        entry = self._store.get(identifier)
        if not entry:
            return False
        if time.time() < entry.locked_until:
            return True
        # Lock expired, reset
        self._store.pop(identifier, None)
        return False

    def record_failure(self, identifier: str) -> None:
        entry = self._store.get(identifier, RateLimitEntry(0, 0.0))
        entry.failures += 1
        if entry.failures >= self.MAX_FAILURES:
            entry.locked_until = time.time() + self.LOCKOUT_SECONDS
        self._store[identifier] = entry

    def record_success(self, identifier: str) -> None:
        self._store.pop(identifier, None)


login_rate_limiter = LoginRateLimiter()


def trust_proxy_headers() -> bool:
    """Whether to trust X-Forwarded-* headers (only behind a trusted proxy)."""
    return os.getenv("TRUST_PROXY_HEADERS", "false").lower() in ("true", "1", "yes")


def get_client_ip(request: Request) -> str:
    """Client IP for rate limiting.

    By default the direct peer address is used. X-Forwarded-For is only
    honored when TRUST_PROXY_HEADERS=true, i.e. when the app sits behind a
    trusted reverse proxy — otherwise clients could forge it to bypass
    login rate limiting.
    """
    if trust_proxy_headers():
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
