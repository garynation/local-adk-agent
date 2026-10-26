"""Local Web UI authentication middleware and LAN/Tailscale CIDR guard."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import secrets
from typing import Any

try:
    from fastapi import Request, Response
    from fastapi.responses import JSONResponse
    from starlette.middleware.base import BaseHTTPMiddleware
except ImportError:  # pragma: no cover

    class Request:  # type: ignore[no-redef]
        pass

    class Response:  # type: ignore[no-redef]
        pass

    class JSONResponse:  # type: ignore[no-redef]
        def __init__(self, content: Any = None, status_code: int = 200) -> None:
            self.content = content
            self.status_code = status_code

    class BaseHTTPMiddleware:  # type: ignore[no-redef]
        def __init__(self, app: Any = None) -> None:
            self.app = app


SESSION_COOKIE_NAME = "adk_webui_session"
ADMIN_SESSION_COOKIE = SESSION_COOKIE_NAME
PBKDF2_ITERATIONS = 200_000

DEFAULT_ALLOWED_CIDRS: list[str] = [
    "127.0.0.0/8",
    "::1/128",
    "100.64.0.0/10",   # Tailscale CGNAT subnet
    "192.168.0.0/16",  # Private LAN
    "10.0.0.0/8",      # Private LAN / Docker bridge
    "172.16.0.0/12",   # Docker / Private networks
]


class PinHashResult(tuple):
    """Dual tuple/dict result supporting both `salt, formatted = hash_admin_pin(pin)` and `record['salt']` / `record['pin_hash']`."""

    def __new__(cls, salt_hex: str, formatted_hash: str) -> PinHashResult:
        instance = super().__new__(cls, (salt_hex, formatted_hash))
        instance._mapping = {
            "salt": salt_hex,
            "pin_hash": formatted_hash,
            "formatted": formatted_hash,
        }
        return instance

    def __getitem__(self, key: int | slice | str) -> Any:
        if isinstance(key, str):
            return self._mapping[key]
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        return self._mapping.get(key, default)

    def keys(self) -> Any:
        return self._mapping.keys()


def hash_admin_pin(pin: str, salt: str | None = None) -> PinHashResult:
    """Hashes an admin PIN using PBKDF2-HMAC-SHA256.

    Returns:
        PinHashResult supporting both tuple unpacking `(salt_hex, formatted_hash)`
        and dictionary key access `record["salt"]`, `record["pin_hash"]`.
    """
    clean_pin = pin.strip()
    if not clean_pin:
        return PinHashResult("", "")
    salt_hex = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        clean_pin.encode("utf-8"),
        salt_hex.encode("utf-8"),
        PBKDF2_ITERATIONS,
    ).hex()
    formatted = f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt_hex}${digest}"
    return PinHashResult(salt_hex, formatted)


def verify_admin_pin(
    pin: str,
    arg2: str | None,
    arg3: str | None = None,
) -> bool:
    """Verifies a candidate admin PIN against a stored PBKDF2-HMAC-SHA256 hash.

    Supports both `(pin, stored_hash, stored_salt=None)` and `(pin, salt, pin_hash)`.
    """
    if not arg2 and not arg3:
        return True
    clean_pin = pin.strip()
    if not clean_pin:
        return False

    # Detect calling convention: (pin, salt, pin_hash) vs (pin, stored_hash, stored_salt)
    if arg3 and (
        arg3.startswith("pbkdf2_sha256$")
        or (arg2 and not arg2.startswith("pbkdf2_sha256$") and len(arg3) >= len(arg2))
    ):
        stored_hash = arg3
        stored_salt = arg2
    else:
        stored_hash = arg2
        stored_salt = arg3

    if not stored_hash:
        return True

    if stored_hash.startswith("pbkdf2_sha256$"):
        parts = stored_hash.split("$")
        if len(parts) == 4:
            try:
                iterations = int(parts[1])
            except ValueError:
                iterations = PBKDF2_ITERATIONS
            salt_hex = parts[2]
            expected_digest = parts[3]
            candidate = hashlib.pbkdf2_hmac(
                "sha256",
                clean_pin.encode("utf-8"),
                salt_hex.encode("utf-8"),
                iterations,
            ).hex()
            return secrets.compare_digest(candidate, expected_digest)

    salt_hex = stored_salt or ""
    candidate = hashlib.pbkdf2_hmac(
        "sha256",
        clean_pin.encode("utf-8"),
        salt_hex.encode("utf-8"),
        PBKDF2_ITERATIONS,
    ).hex()
    return secrets.compare_digest(candidate, stored_hash)


def sign_session_token(secret: str) -> str:
    """Generates an HMAC-SHA256 session cookie token."""
    key = (secret or "local-adk-default-session-secret").encode("utf-8")
    return hmac.new(key, b"authenticated-owner", hashlib.sha256).hexdigest()


compute_session_cookie_token = sign_session_token


def is_ip_allowed(client_host: str, cidrs: list[str] | None = None) -> bool:
    """Checks whether client_host belongs to any allowed CIDR block."""
    if not client_host or client_host in ("testclient", "localhost"):
        return True
    try:
        ip = ipaddress.ip_address(client_host)
    except ValueError:
        return True

    allowed_list = cidrs or DEFAULT_ALLOWED_CIDRS
    for cidr in allowed_list:
        try:
            if ip in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


class LocalWebUIAuthMiddleware(BaseHTTPMiddleware):
    """Protects the Web UI and /api/* routes with CIDR allowlisting and optional PIN cookie."""

    def __init__(
        self,
        app: Any,
        config_manager: Any = None,
        config_store: Any = None,
    ) -> None:
        super().__init__(app)
        self._config_manager = config_manager or config_store
        self._fallback_secret = os.environ.get(
            "WEBUI_SESSION_SECRET", secrets.token_hex(32)
        )

    def _resolve_config(self, request: Request) -> Any:
        mgr = (
            self._config_manager
            or getattr(request.app.state, "config_manager", None)
            or getattr(request.app.state, "config_store", None)
        )
        if mgr is None:
            try:
                from src.core.config_store import get_config_manager

                mgr = get_config_manager()
            except Exception:
                return None

        if hasattr(mgr, "config"):
            return mgr.config
        if hasattr(mgr, "get"):
            return mgr.get()
        if hasattr(mgr, "get_config"):
            return mgr.get_config()
        return mgr

    def _extract_attr(self, cfg: Any, name: str, default: Any = None) -> Any:
        if cfg is None:
            return default
        if isinstance(cfg, dict):
            return cfg.get(name, default)
        return getattr(cfg, name, default)

    def sign_token(self, secret: str | None = None) -> str:
        return sign_session_token(secret or self._fallback_secret)

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        path = request.url.path

        # 1. Public health check & Google Chat JWT-verified webhook bypass UI cookie auth
        if path in ("/api/health", "/api/gchat/webhook"):
            return await call_next(request)

        cfg = self._resolve_config(request)
        client_host = request.client.host if request.client else "127.0.0.1"
        allowed_cidrs = (
            self._extract_attr(cfg, "webui_allowed_cidrs", None)
            or DEFAULT_ALLOWED_CIDRS
        )

        # 2. Enforce LAN / Tailscale / localhost subnet boundary
        if not is_ip_allowed(client_host, allowed_cidrs):
            return JSONResponse(
                status_code=403,
                content={
                    "error": f"Client IP {client_host} is outside allowed subnets."
                },
            )

        # 3. Check if Admin PIN authentication is active
        pin_hash = self._extract_attr(
            cfg, "webui_admin_pin_hash", None
        ) or self._extract_attr(cfg, "webui_pin_hash", None)
        auth_enabled = bool(
            self._extract_attr(cfg, "webui_auth_enabled", False) or pin_hash
        )

        if auth_enabled:
            if (
                path
                in (
                    "/",
                    "/api/auth/login",
                    "/api/auth/logout",
                    "/api/auth/status",
                    "/api/config/security",
                )
                or path.startswith("/static/")
            ):
                return await call_next(request)

            session_secret = (
                self._extract_attr(cfg, "webui_session_secret", None)
                or self._extract_attr(cfg, "webui_admin_pin_hash", None)
                or self._fallback_secret
            )
            cookie = request.cookies.get(SESSION_COOKIE_NAME, "")
            expected = sign_session_token(str(session_secret))
            if not cookie or not hmac.compare_digest(cookie, expected):
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": "Web UI PIN authentication required.",
                        "auth_required": True,
                    },
                )

        return await call_next(request)
