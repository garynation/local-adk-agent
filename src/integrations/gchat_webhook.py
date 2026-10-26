"""Optional Cloudflare Tunnel (`cloudflared`) Google Chat Webhook Handler.

Verifies the Google Chat OIDC JWT bearer token (`iss == "chat@system.gserviceaccount.com"`)
and dispatches the event asynchronously to `GChatPubSubBridge.handle_chat_event()`
via FastAPI `BackgroundTasks` so long Deep Research or Headless Chrome tasks never
trigger Google Chat's 30-second synchronous HTTP timeout.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

try:
    from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
except ImportError:  # pragma: no cover - host fallback when fastapi is not installed

    class HTTPException(Exception):  # type: ignore[no-redef]
        def __init__(self, status_code: int, detail: str = "") -> None:
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class BackgroundTasks:  # type: ignore[no-redef]
        def __init__(self) -> None:
            self.tasks: list[tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]]] = []

        def add_task(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
            self.tasks.append((func, args, kwargs))

    class Request:  # type: ignore[no-redef]
        pass

    def Header(default: Any = None, **kwargs: Any) -> Any:  # type: ignore[misc]
        return default

    class APIRouter:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            self.routes: list[Any] = []

        def post(self, path: str, **kwargs: Any) -> Callable[[Any], Any]:
            def _dec(fn: Any) -> Any:
                self.routes.append(("POST", path, fn))
                return fn

            return _dec


try:
    from google.auth.transport import requests as google_auth_requests
except ImportError:  # pragma: no cover

    class _FallbackAuthRequests:
        class Request:
            pass

    google_auth_requests = _FallbackAuthRequests()  # type: ignore[assignment]

try:
    from google.oauth2 import id_token
except ImportError:  # pragma: no cover

    class _FallbackIdToken:
        @staticmethod
        def verify_oauth2_token(
            token: str, request: Any, audience: Optional[str] = None
        ) -> dict[str, Any]:
            raise RuntimeError("google-auth is required for live OIDC token verification.")

    id_token = _FallbackIdToken()  # type: ignore[assignment]

from src.integrations.gchat_pubsub import get_gchat_bridge

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/gchat", tags=["google-chat"])

CHAT_ISSUER = "chat@system.gserviceaccount.com"
ALLOWED_ISSUERS = (CHAT_ISSUER, "https://accounts.google.com")


def verify_gchat_bearer_token(
    authorization: Optional[str],
    expected_audience: Optional[str] = None,
) -> dict[str, Any]:
    """Verifies the Google Chat OIDC Bearer token signature, expiry, audience, and issuer."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token in Authorization header.")

    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty Bearer token.")

    try:
        request_adapter = google_auth_requests.Request()
        claims = id_token.verify_oauth2_token(
            token,
            request_adapter,
            audience=expected_audience or None,
        )
        if claims.get("iss") not in ALLOWED_ISSUERS:
            raise ValueError(f"Unexpected token issuer: {claims.get('iss')}")
        return dict(claims)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Google Chat webhook JWT verification failed: %s", exc)
        raise HTTPException(status_code=403, detail=f"Invalid Google Chat token: {exc}") from exc


verify_google_chat_jwt = verify_gchat_bearer_token


def _resolve_expected_audience(request: Request) -> str:
    try:
        from src.core.config_store import get_config_manager

        mgr = get_config_manager()
        cfg = mgr.config if hasattr(mgr, "config") else mgr.get()
        if isinstance(cfg, dict):
            gchat = cfg.get("gchat", cfg)
            return str(
                gchat.get("webhook_audience")
                or gchat.get("gcp_project_number")
                or gchat.get("gcp_project_id")
                or ""
            ).strip()
        return str(
            getattr(cfg, "webhook_audience", None)
            or getattr(cfg, "gcp_project_id", None)
            or getattr(cfg, "gchat_project_id", None)
            or ""
        ).strip()
    except Exception:
        return ""


@router.post("/webhook")
async def gchat_webhook_endpoint(
    request: Request,
    background_tasks: BackgroundTasks,
    authorization: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    """Handles inbound Google Chat HTTP webhook requests (Cloudflare Tunnel mode)."""
    expected_audience = _resolve_expected_audience(request)
    verify_gchat_bearer_token(authorization, expected_audience or None)

    event = await request.json()  # type: ignore[attr-defined]
    app_state = getattr(getattr(request, "app", None), "state", None)
    bridge = getattr(app_state, "gchat_bridge", None) or get_gchat_bridge()
    background_tasks.add_task(bridge.handle_chat_event, event)
    return {"status": "accepted"}
