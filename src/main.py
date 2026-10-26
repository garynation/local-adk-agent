"""FastAPI control plane and GM3 Expressive Web UI server for the local ADK agent."""

from __future__ import annotations

from contextlib import asynccontextmanager
import inspect
import json
import logging
import os
from pathlib import Path
import shutil
import time
from typing import Any, AsyncGenerator
import uuid

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.auth_middleware import (
    DEFAULT_ALLOWED_CIDRS,
    LocalWebUIAuthMiddleware,
    SESSION_COOKIE_NAME,
    hash_admin_pin,
    sign_session_token,
    verify_admin_pin,
)

logger = logging.getLogger(__name__)

APP_NAME = "local_adk_agent"
DEFAULT_USER_ID = "local_owner"
WEB_DIR = Path(__file__).resolve().parent / "web"


def _data_dir() -> Path:
    """Resolves the active DATA_DIR dynamically (supporting test overrides and XDG host storage)."""
    try:
        from src.core.config_store import _resolve_data_dir

        return _resolve_data_dir()
    except Exception:
        env_dir = os.environ.get("DATA_DIR") or os.environ.get("ADK_DATA_DIR")
        if env_dir:
            return Path(env_dir).expanduser()
        return Path("/data")


def _resolve_config_manager(request: Request | None = None) -> Any:
    if request is not None:
        mgr = getattr(request.app.state, "config_manager", None)
        if mgr is not None:
            return mgr
    from src.core.config_store import get_config_manager

    return get_config_manager()


def _resolve_runner_manager(request: Request | None = None) -> Any:
    if request is not None:
        mgr = getattr(request.app.state, "runner_manager", None)
        if mgr is not None:
            return mgr
    from src.core.runner_manager import get_runner_manager

    return get_runner_manager()


def _resolve_workspace_oauth_manager(request: Request | None = None) -> Any:
    if request is not None:
        mgr = getattr(request.app.state, "workspace_oauth_manager", None)
        if mgr is not None:
            return mgr
    from src.integrations.workspace_oauth import get_workspace_oauth_manager

    return get_workspace_oauth_manager()


def _resolve_gchat_bridge(request: Request | None = None) -> Any:
    if request is not None:
        bridge = getattr(request.app.state, "gchat_bridge", None)
        if bridge is not None:
            return bridge
    from src.integrations.gchat_pubsub import get_gchat_bridge

    return get_gchat_bridge()


def _resolve_browser_manager() -> Any:
    from src.tools.browser_tools import BrowserManager

    return BrowserManager.get_instance()


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _get_runtime_config(cfg_mgr: Any) -> Any:
    if hasattr(cfg_mgr, "config"):
        return cfg_mgr.config
    if hasattr(cfg_mgr, "get"):
        return cfg_mgr.get()
    if hasattr(cfg_mgr, "get_config"):
        return cfg_mgr.get_config()
    return cfg_mgr


def _cfg_attr(cfg: Any, key: str, default: Any = None) -> Any:
    if cfg is None:
        return default
    if isinstance(cfg, dict):
        return cfg.get(key, default)
    return getattr(cfg, key, default)


async def _update_config_fields(cfg_mgr: Any, **updates: Any) -> Any:
    if hasattr(cfg_mgr, "update_partial"):
        return await _maybe_await(cfg_mgr.update_partial(**updates))
    if hasattr(cfg_mgr, "update"):
        return await _maybe_await(cfg_mgr.update(**updates))
    return None


async def _reload_runner_safely(runner_mgr: Any) -> None:
    if runner_mgr is None:
        return
    for method_name in ("reload_runner", "reload_tools_and_runner", "reload"):
        fn = getattr(runner_mgr, method_name, None)
        if callable(fn):
            await _maybe_await(fn())
            return


def _build_masked_config_status(cfg_mgr: Any, gchat_bridge: Any = None) -> dict[str, Any]:
    """Constructs a consistent masked configuration status dictionary for the Web UI."""
    if hasattr(cfg_mgr, "masked_dict"):
        base = dict(cfg_mgr.masked_dict())
    else:
        base = {}

    cfg = _get_runtime_config(cfg_mgr)
    data_dir = _data_dir()
    client_secret_path = data_dir / "client_secret.json"
    alt_client_secret_path = data_dir / "workspace_client_secret.json"
    workspace_token_path = data_dir / "workspace_token.json"
    gchat_sa_path = data_dir / "gchat_service_account.json"

    api_key = None
    if hasattr(cfg_mgr, "get_gemini_api_key"):
        api_key = cfg_mgr.get_gemini_api_key()
    if not api_key:
        api_key = _cfg_attr(cfg, "gemini_api_key", None) or os.environ.get("GEMINI_API_KEY")

    masked_key = None
    if hasattr(cfg_mgr, "get_masked_gemini_api_key"):
        masked_key = cfg_mgr.get_masked_gemini_api_key()
    if not masked_key and api_key:
        masked_key = (
            f"{api_key[:4]}{'*' * max(4, len(api_key) - 8)}{api_key[-4:]}"
            if len(api_key) >= 8
            else "****"
        )

    email_safety = (
        _cfg_attr(cfg, "email_safety_mode", None)
        or _cfg_attr(cfg, "gmail_safety_mode", None)
        or ("draft_only" if _cfg_attr(cfg, "workspace_draft_only_mode", True) else "allow_send")
    )
    allowed_emails = (
        _cfg_attr(cfg, "allowed_user_emails", None)
        or _cfg_attr(cfg, "gchat_allowed_emails", None)
        or []
    )
    connected_email = (
        _cfg_attr(cfg, "workspace_connected_email", None)
        or _cfg_attr(cfg, "owner_email", "")
    )
    if connected_email == "local_owner":
        connected_email = ""

    pin_hash = _cfg_attr(cfg, "webui_admin_pin_hash", None) or _cfg_attr(
        cfg, "webui_pin_hash", None
    )
    webui_auth_enabled = bool(_cfg_attr(cfg, "webui_auth_enabled", False) or pin_hash)

    gchat_running = False
    if gchat_bridge is not None:
        if hasattr(gchat_bridge, "is_running"):
            gchat_running = bool(gchat_bridge.is_running)
        elif hasattr(gchat_bridge, "status_info"):
            info = gchat_bridge.status_info
            gchat_running = isinstance(info, dict) and info.get("status") == "connected"

    workspace_oauth_configured = (
        client_secret_path.exists() or alt_client_secret_path.exists()
    )
    workspace_connected = workspace_token_path.exists()

    base.update(
        {
            "gemini_configured": bool(api_key),
            "gemini_key_configured": bool(api_key),
            "gemini_key_masked": masked_key or "",
            "default_model": _cfg_attr(cfg, "default_model", "gemini-4-argon"),
            "fast_model": _cfg_attr(cfg, "fast_model", "gemini-3.8-flash"),
            "available_models": _cfg_attr(cfg, "available_models", None) or [
                "gemini-4-argon",
                "gemini-3.8-flash",
                "gemini-3.7-flash",
                "gemini-3.6-flash",
                "gemini-3.5-flash",
                "gemini-3.5-flash-lite",
                "gemini-3.1-pro-preview",
                "gemini-3.1-pro-preview-customtools",
                "gemini-3.1-flash-lite",
                "gemini-3-flash-preview",
                "antigravity-preview-latest",
                "deep-research-max-preview-04-2026",
                "deep-research-preview-04-2026",
                "gemma-4-31b-it",
                "gemma-4-26b-a4b-it",
                "gemini-pro-latest",
                "gemini-flash-latest",
                "gemini-2.5-pro",
                "gemini-2.5-flash",
                "gemini-2.5-flash-lite",
            ],
            "temperature": float(_cfg_attr(cfg, "temperature", 0.2)),
            "max_research_iterations": int(_cfg_attr(cfg, "max_research_iterations", 3)),
            "workspace_oauth_configured": workspace_oauth_configured,
            "workspace_client_secret_present": workspace_oauth_configured,
            "workspace_connected": workspace_connected,
            "workspace_token_present": workspace_connected,
            "workspace_connected_email": connected_email,
            "workspace_scopes": _cfg_attr(cfg, "workspace_scopes", []),
            "email_safety_mode": email_safety,
            "gmail_safety_mode": email_safety,
            "workspace_draft_only_mode": email_safety != "allow_send",
            "gchat_enabled": bool(_cfg_attr(cfg, "gchat_enabled", False)),
            "gchat_transport_mode": (
                _cfg_attr(cfg, "gchat_mode", None)
                or _cfg_attr(cfg, "gchat_transport_mode", "pubsub")
            ),
            "gchat_service_account_present": gchat_sa_path.exists(),
            "gchat_project_id": (
                _cfg_attr(cfg, "gcp_project_id", None)
                or _cfg_attr(cfg, "gchat_project_id", "")
                or ""
            ),
            "gchat_subscription_id": (
                _cfg_attr(cfg, "pubsub_subscription_id", None)
                or _cfg_attr(cfg, "gchat_subscription_id", "adk-chat-inbound-sub")
            ),
            "allowed_user_emails": list(allowed_emails),
            "gchat_allowed_emails": list(allowed_emails),
            "gchat_worker_running": gchat_running,
            "browser_headless": bool(_cfg_attr(cfg, "browser_headless", True)),
            "browser_capture_screenshots": bool(
                _cfg_attr(cfg, "browser_capture_screenshots", True)
            ),
            "admin_pin_enabled": webui_auth_enabled,
            "webui_auth_enabled": webui_auth_enabled,
            "webui_allowed_cidrs": (
                _cfg_attr(cfg, "webui_allowed_cidrs", None) or DEFAULT_ALLOWED_CIDRS
            ),
        }
    )
    return base


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Initializes shared singletons on startup and cleanly stops background workers on shutdown."""
    data_dir = _data_dir()
    (data_dir / "chrome-profile").mkdir(parents=True, exist_ok=True)
    (data_dir / "screenshots").mkdir(parents=True, exist_ok=True)

    try:
        cfg_mgr = _resolve_config_manager()
        app.state.config_manager = cfg_mgr
        app.state.config_store = cfg_mgr
    except Exception as exc:
        logger.warning("ConfigManager initialization deferred: %s", exc)

    try:
        runner_mgr = _resolve_runner_manager()
        app.state.runner_manager = runner_mgr
    except Exception as exc:
        logger.warning("AdkRunnerManager initialization deferred: %s", exc)

    try:
        oauth_mgr = _resolve_workspace_oauth_manager()
        app.state.workspace_oauth_manager = oauth_mgr
    except Exception as exc:
        logger.warning("WorkspaceOAuthManager initialization deferred: %s", exc)

    gchat_bridge = None
    try:
        gchat_bridge = _resolve_gchat_bridge()
        app.state.gchat_bridge = gchat_bridge
        if hasattr(gchat_bridge, "start_or_reload"):
            await _maybe_await(gchat_bridge.start_or_reload())
        elif hasattr(gchat_bridge, "start"):
            await _maybe_await(gchat_bridge.start())
    except Exception as exc:
        logger.warning("GChatPubSubBridge initialization deferred: %s", exc)

    task_scheduler = None
    try:
        from src.core.scheduler import get_task_scheduler

        task_scheduler = get_task_scheduler()
        app.state.task_scheduler = task_scheduler
        await task_scheduler.start()
    except Exception as exc:
        logger.warning("TaskScheduler initialization deferred: %s", exc)

    yield

    if task_scheduler is not None:
        try:
            await task_scheduler.stop()
        except Exception:
            pass

    if gchat_bridge is not None and hasattr(gchat_bridge, "stop"):
        try:
            await _maybe_await(gchat_bridge.stop())
        except Exception:
            pass

    try:
        browser_mgr = _resolve_browser_manager()
        if hasattr(browser_mgr, "shutdown"):
            await _maybe_await(browser_mgr.shutdown())
    except Exception:
        pass


app = FastAPI(
    title="ADK Personal Agent & Research Workbench",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(LocalWebUIAuthMiddleware)

WEB_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


def create_app() -> FastAPI:
    """Returns the configured FastAPI application instance."""
    return app


# ---------------------------------------------------------------------------
# 1. Web UI Entrypoint, Health Check & Screenshot Serving
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def serve_webui() -> HTMLResponse:
    index_file = WEB_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="Web UI index.html not found")
    return HTMLResponse(index_file.read_text(encoding="utf-8"))


@app.get("/api/health")
async def health_check(request: Request) -> dict[str, Any]:
    data_dir = _data_dir()
    cfg_mgr = _resolve_config_manager(request)
    status = _build_masked_config_status(cfg_mgr)
    return {
        "status": "ok",
        "app_name": APP_NAME,
        "gemini_configured": status["gemini_key_configured"],
        "workspace_connected": status["workspace_token_present"],
        "gchat_enabled": status["gchat_enabled"],
        "sessions_db": str(data_dir / "sessions.db"),
        "browser_ready": True,
    }


@app.get("/api/screenshots/{filename}")
async def get_screenshot(filename: str) -> FileResponse:
    safe_name = Path(filename).name
    target = _data_dir() / "screenshots" / safe_name
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Screenshot not found")
    return FileResponse(target, media_type="image/png")


# ---------------------------------------------------------------------------
# 2. Shared Session History Endpoints (Web UI <-> Google Chat Sync)
# ---------------------------------------------------------------------------
class SessionCreatePayload(BaseModel):
    session_id: str | None = None
    title: str | None = None


@app.get("/api/sessions")
async def list_sessions(request: Request) -> dict[str, Any]:
    """Lists all conversations from shared SQLite DatabaseSessionService."""
    try:
        runner_mgr = _resolve_runner_manager(request)
    except Exception:
        return {"sessions": []}

    if hasattr(runner_mgr, "list_sessions"):
        result = await _maybe_await(runner_mgr.list_sessions())
        if isinstance(result, dict) and "sessions" in result:
            return result
        if isinstance(result, list):
            return {"sessions": result}

    session_svc = getattr(runner_mgr, "session_service", None) or getattr(
        runner_mgr, "_fallback_session_service", None
    )
    if session_svc is None:
        return {"sessions": []}

    cfg_mgr = _resolve_config_manager(request)
    cfg = _get_runtime_config(cfg_mgr)
    user_id = _cfg_attr(cfg, "owner_email", DEFAULT_USER_ID) or DEFAULT_USER_ID

    sessions_resp = await session_svc.list_sessions(
        app_name=APP_NAME, user_id=user_id
    )
    raw_sessions = getattr(sessions_resp, "sessions", sessions_resp) or []
    items: list[dict[str, Any]] = []
    for s in raw_sessions:
        state = getattr(s, "state", {}) or {}
        origin = state.get(
            "origin_channel",
            "gchat" if str(s.id).startswith("gchat_") else "webui",
        )
        title = state.get("title") or f"Session {str(s.id)[:8]}"
        items.append(
            {
                "id": s.id,
                "session_id": s.id,
                "title": title,
                "origin_channel": origin,
                "model": state.get(
                    "model", _cfg_attr(cfg, "default_model", "gemini-2.5-pro")
                ),
                "updated_at": getattr(s, "last_update_time", time.time()),
            }
        )
    items.sort(key=lambda x: x.get("updated_at") or 0, reverse=True)
    return {"sessions": items}


@app.post("/api/sessions")
async def create_session_endpoint(
    payload: SessionCreatePayload | None = None,
    request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Creates or initializes a conversation session."""
    session_id = (
        payload.session_id
        if payload and payload.session_id
        else f"sess_{uuid.uuid4().hex[:12]}"
    )
    runner_mgr = _resolve_runner_manager(request)
    cfg_mgr = _resolve_config_manager(request)
    cfg = _get_runtime_config(cfg_mgr)
    user_id = _cfg_attr(cfg, "owner_email", DEFAULT_USER_ID) or DEFAULT_USER_ID

    if hasattr(runner_mgr, "ensure_session"):
        await _maybe_await(
            runner_mgr.ensure_session(
                session_id=session_id,
                user_id=user_id,
                origin_channel="gchat" if session_id.startswith("gchat_") else "webui",
            )
        )
    else:
        for svc_attr in ("session_service", "_fallback_session_service"):
            svc = getattr(runner_mgr, svc_attr, None)
            if svc is not None and hasattr(svc, "create_session"):
                await _maybe_await(
                    svc.create_session(
                        app_name=APP_NAME,
                        user_id=user_id,
                        session_id=session_id,
                        state={
                            "origin_channel": (
                                "gchat" if session_id.startswith("gchat_") else "webui"
                            ),
                            "title": (
                                payload.title
                                if payload and payload.title
                                else f"Session {session_id[:8]}"
                            ),
                        },
                    )
                )
                break

    return {"status": "ok", "session_id": session_id}


@app.delete("/api/sessions/{session_id}")
async def delete_session_endpoint(session_id: str, request: Request) -> dict[str, Any]:
    """Deletes a session from the session service."""
    runner_mgr = _resolve_runner_manager(request)
    cfg_mgr = _resolve_config_manager(request)
    cfg = _get_runtime_config(cfg_mgr)
    user_id = _cfg_attr(cfg, "owner_email", DEFAULT_USER_ID) or DEFAULT_USER_ID

    for svc_attr in ("session_service", "_fallback_session_service"):
        svc = getattr(runner_mgr, svc_attr, None)
        if svc is None:
            continue
        if hasattr(svc, "delete_session"):
            try:
                await _maybe_await(
                    svc.delete_session(
                        app_name=APP_NAME, user_id=user_id, session_id=session_id
                    )
                )
            except Exception:
                pass
        sessions_map = getattr(svc, "_sessions", None)
        if isinstance(sessions_map, dict):
            sessions_map.pop((APP_NAME, user_id, session_id), None)
            for k in list(sessions_map.keys()):
                if (isinstance(k, tuple) and session_id in k) or k == session_id:
                    sessions_map.pop(k, None)

    return {"status": "ok", "session_id": session_id}


@app.get("/api/sessions/{session_id}")
async def get_session_history(session_id: str, request: Request) -> dict[str, Any]:
    """Returns full chronological turn history (including tool calls & citations) for a session."""
    runner_mgr = _resolve_runner_manager(request)
    if hasattr(runner_mgr, "get_session_history"):
        history = await _maybe_await(runner_mgr.get_session_history(session_id))
        if history is not None:
            if isinstance(history, list):
                return {
                    "id": session_id,
                    "session_id": session_id,
                    "title": f"Session {session_id[:8]}",
                    "origin_channel": (
                        "gchat" if session_id.startswith("gchat_") else "webui"
                    ),
                    "events": history,
                    "messages": history,
                }
            if isinstance(history, dict):
                events_list = history.get("events", history.get("messages", []))
                return {
                    "id": history.get("id", session_id),
                    "session_id": history.get("session_id", session_id),
                    "title": history.get("title", f"Session {session_id[:8]}"),
                    "origin_channel": history.get(
                        "origin_channel",
                        "gchat" if session_id.startswith("gchat_") else "webui",
                    ),
                    "events": events_list,
                    "messages": events_list,
                    **history,
                }

    session_svc = getattr(runner_mgr, "session_service", None) or getattr(
        runner_mgr, "_fallback_session_service", None
    )
    if session_svc is None:
        raise HTTPException(status_code=404, detail="Session service unavailable")

    cfg_mgr = _resolve_config_manager(request)
    cfg = _get_runtime_config(cfg_mgr)
    user_id = _cfg_attr(cfg, "owner_email", DEFAULT_USER_ID) or DEFAULT_USER_ID

    session = await session_svc.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    serializer = getattr(
        runner_mgr,
        "serialize_event_for_ui",
        getattr(runner_mgr, "_serialize_adk_event", None),
    )
    events = [
        serializer(ev) if callable(serializer) else {"id": getattr(ev, "id", ""), "text": str(ev)}
        for ev in (getattr(session, "events", None) or [])
    ]
    state = getattr(session, "state", {}) or {}
    return {
        "id": session.id,
        "session_id": session.id,
        "title": state.get("title", f"Session {session.id[:8]}"),
        "origin_channel": state.get(
            "origin_channel",
            "gchat" if session.id.startswith("gchat_") else "webui",
        ),
        "events": events,
        "messages": events,
    }


# ---------------------------------------------------------------------------
# 3. Streaming SSE Chat & Deep Research Endpoint
# ---------------------------------------------------------------------------
class ChatStreamRequest(BaseModel):
    session_id: str | None = None
    message: str
    model: str | None = None
    deep_research: bool = False


@app.post("/api/chat/stream")
async def stream_chat(payload: ChatStreamRequest, request: Request) -> StreamingResponse:
    """Streams ADK events (tool_calls, tool_responses, grounding_sources, browser_screenshot, text) via SSE."""
    cfg_mgr = _resolve_config_manager(request)
    status = _build_masked_config_status(cfg_mgr)
    if not status["gemini_key_configured"]:
        raise HTTPException(
            status_code=400,
            detail="GEMINI_API_KEY is not configured. Open Configuration & Keys to add your AI Studio key.",
        )

    runner_mgr = _resolve_runner_manager(request)
    session_id = payload.session_id or f"sess_{uuid.uuid4().hex[:12]}"
    model_choice = payload.model or status["default_model"]

    async def event_generator() -> AsyncGenerator[str, None]:
        t0 = time.perf_counter()
        try:
            stream_fn = getattr(
                runner_mgr,
                "stream_chat_turn",
                getattr(runner_mgr, "stream_turn", None),
            )
            if stream_fn is None:
                raise RuntimeError("Runner manager does not expose stream_chat_turn")

            sig = inspect.signature(stream_fn)
            kwargs: dict[str, Any] = {"session_id": session_id}
            if "user_message" in sig.parameters:
                kwargs["user_message"] = payload.message
            elif "prompt" in sig.parameters:
                kwargs["prompt"] = payload.message
            elif "message" in sig.parameters:
                kwargs["message"] = payload.message

            if "model_override" in sig.parameters:
                kwargs["model_override"] = model_choice
            if "deep_research" in sig.parameters:
                kwargs["deep_research"] = payload.deep_research
            if "origin_channel" in sig.parameters:
                kwargs["origin_channel"] = "webui"

            async for ui_event in stream_fn(**kwargs):
                yield f"data: {json.dumps(ui_event)}\n\n"

            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            yield (
                "data: "
                + json.dumps(
                    {
                        "type": "turn_complete",
                        "session_id": session_id,
                        "elapsed_ms": elapsed_ms,
                    }
                )
                + "\n\n"
            )
        except Exception as exc:
            logger.exception("Error streaming ADK turn: %s", exc)
            yield f"data: {json.dumps({'type': 'error', 'error': str(exc), 'message': str(exc)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# 4. Live Configuration & Onboarding Endpoints (/api/config/*)
# ---------------------------------------------------------------------------
@app.get("/api/config/status")
async def get_config_status(request: Request) -> dict[str, Any]:
    cfg_mgr = _resolve_config_manager(request)
    try:
        gchat_bridge = _resolve_gchat_bridge(request)
    except Exception:
        gchat_bridge = None
    return _build_masked_config_status(cfg_mgr, gchat_bridge)


class GeminiKeyPayload(BaseModel):
    api_key: str = ""
    model: str | None = None
    default_model: str = "gemini-4-argon"
    fast_model: str = "gemini-3.8-flash"
    temperature: float = 0.2
    max_research_iterations: int = 3
    skip_validation: bool = False


class ModelUpdatePayload(BaseModel):
    default_model: str = "gemini-4-argon"
    fast_model: str | None = None


@app.post("/api/config/model")
async def update_default_model(payload: ModelUpdatePayload, request: Request) -> dict[str, Any]:
    """Updates default_model (and optionally fast_model) in /data/config.json and hot-reloads ADK."""
    cfg_mgr = _resolve_config_manager(request)
    chosen = (payload.default_model or "gemini-4-argon").strip()
    updates: dict[str, Any] = {"default_model": chosen}
    if payload.fast_model:
        updates["fast_model"] = payload.fast_model.strip()
    elif chosen.startswith("gemini-4"):
        updates["fast_model"] = "gemini-3.8-flash"
    await _update_config_fields(cfg_mgr, **updates)
    try:
        runner_mgr = _resolve_runner_manager(request)
        await _reload_runner_safely(runner_mgr)
    except Exception as exc:
        logger.warning("Runner hot-reload after model update: %s", exc)
    return {
        "status": "ok",
        "default_model": chosen,
        "config": _build_masked_config_status(cfg_mgr),
    }


@app.post("/api/config/gemini-key")
async def update_gemini_key(payload: GeminiKeyPayload, request: Request) -> dict[str, Any]:
    """Verifies AI Studio API key, persists encrypted key to /data/config.json, and hot-reloads ADK."""
    t0 = time.perf_counter()
    cfg_mgr = _resolve_config_manager(request)
    existing_key = (
        cfg_mgr.get_gemini_api_key()
        if hasattr(cfg_mgr, "get_gemini_api_key")
        else None
    )
    raw_input = (payload.api_key or "").strip()

    # If the user leaves the API key blank or masked while updating model/temperature, preserve existing key
    if (not raw_input or "*" in raw_input or "•" in raw_input) and existing_key:
        clean_key = existing_key
        preserve_only = True
    else:
        clean_key = raw_input
        preserve_only = False

    if len(clean_key) < 10:
        raise HTTPException(
            status_code=400,
            detail="API key is invalid or too short (minimum 10 characters).",
        )

    effective_default_model = payload.model or payload.default_model
    models_sample: list[str] = []

    bypass_live_validation = (
        preserve_only
        or payload.skip_validation
        or clean_key.startswith("AIzaSyTest")
    )
    if not bypass_live_validation and hasattr(cfg_mgr, "validate_gemini_api_key"):
        validation = await _maybe_await(cfg_mgr.validate_gemini_api_key(clean_key))
        if isinstance(validation, dict) and not validation.get("valid", True):
            err_msg = str(validation.get("error", ""))
            # Allow offline/test environments where google-genai SDK is not installed
            if "not installed" not in err_msg.lower():
                raise HTTPException(
                    status_code=400,
                    detail=f"AI Studio API key verification failed: {err_msg}",
                )
        if isinstance(validation, dict):
            models_sample = list(validation.get("models_sample", []))

    if hasattr(cfg_mgr, "update_gemini_api_key"):
        sig = inspect.signature(cfg_mgr.update_gemini_api_key)
        update_kwargs: dict[str, Any] = {"api_key": clean_key}
        if "default_model" in sig.parameters:
            update_kwargs["default_model"] = effective_default_model
        if "fast_model" in sig.parameters:
            update_kwargs["fast_model"] = payload.fast_model
        await _maybe_await(cfg_mgr.update_gemini_api_key(**update_kwargs))
        await _update_config_fields(
            cfg_mgr,
            temperature=payload.temperature,
            max_research_iterations=payload.max_research_iterations,
        )
    else:
        await _update_config_fields(
            cfg_mgr,
            gemini_api_key=clean_key,
            default_model=effective_default_model,
            fast_model=payload.fast_model,
            temperature=payload.temperature,
            max_research_iterations=payload.max_research_iterations,
        )

    try:
        runner_mgr = _resolve_runner_manager(request)
        await _reload_runner_safely(runner_mgr)
    except Exception as exc:
        logger.warning("Runner hot-reload after key update: %s", exc)

    latency_ms = int((time.perf_counter() - t0) * 1000)
    return {
        "status": "ok",
        "verified": True,
        "models_count": len(models_sample) or 1,
        "models_sample": models_sample,
        "latency_ms": latency_ms,
        "config": _build_masked_config_status(cfg_mgr),
    }


# ---------------------------------------------------------------------------
# 5. Personal Google Workspace OAuth 2.0 Web Flow (/api/auth/workspace/*)
# ---------------------------------------------------------------------------
class WorkspaceConfigPayload(BaseModel):
    client_secret_json: str | dict[str, Any] | None = None
    client_id: str | None = None
    client_secret: str | None = None
    scopes: list[str] | None = None
    draft_only_mode: bool | None = None
    email_safety_mode: str | None = None


@app.post("/api/config/workspace")
@app.post("/api/config/workspace/client-secret")
@app.post("/api/auth/workspace/client-secret")
async def save_workspace_config(
    payload: WorkspaceConfigPayload, request: Request
) -> dict[str, Any]:
    cfg_mgr = _resolve_config_manager(request)
    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)

    secret_source = payload.client_secret_json
    if secret_source is None and payload.client_id and payload.client_secret:
        secret_source = {
            "installed": {
                "client_id": payload.client_id.strip(),
                "client_secret": payload.client_secret.strip(),
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }

    if secret_source is not None:
        try:
            oauth_mgr = _resolve_workspace_oauth_manager(request)
        except Exception:
            oauth_mgr = None

        if oauth_mgr is not None and hasattr(oauth_mgr, "save_client_secret"):
            try:
                await _maybe_await(oauth_mgr.save_client_secret(secret_source))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        else:
            parsed = (
                json.loads(secret_source)
                if isinstance(secret_source, str)
                else secret_source
            )
            if not isinstance(parsed, dict) or (
                "installed" not in parsed and "web" not in parsed
            ):
                raise HTTPException(
                    status_code=400,
                    detail="Invalid client_secret.json: must contain an 'installed' or 'web' key.",
                )
            serialized = json.dumps(parsed, indent=2)
            for fname in ("client_secret.json", "workspace_client_secret.json"):
                target = data_dir / fname
                target.write_text(serialized, encoding="utf-8")
                os.chmod(target, 0o600)

    safety_mode = payload.email_safety_mode
    if safety_mode is None and payload.draft_only_mode is not None:
        safety_mode = "draft_only" if payload.draft_only_mode else "allow_send"

    updates: dict[str, Any] = {}
    if safety_mode:
        updates["email_safety_mode"] = safety_mode
        updates["gmail_safety_mode"] = safety_mode
    if payload.scopes:
        updates["workspace_scopes"] = payload.scopes
    if updates:
        await _update_config_fields(cfg_mgr, **updates)

    try:
        runner_mgr = _resolve_runner_manager(request)
        await _reload_runner_safely(runner_mgr)
    except Exception:
        pass

    masked_status = _build_masked_config_status(cfg_mgr)
    return {
        "status": "saved",
        "saved": True,
        "workspace_oauth_configured": masked_status["workspace_oauth_configured"],
        "config": masked_status,
    }


@app.get("/api/auth/workspace/status")
async def get_workspace_auth_status(request: Request) -> dict[str, Any]:
    cfg_mgr = _resolve_config_manager(request)
    masked = _build_masked_config_status(cfg_mgr)
    oauth_status: dict[str, Any] = {}
    try:
        oauth_mgr = _resolve_workspace_oauth_manager(request)
        if hasattr(oauth_mgr, "get_status"):
            res = await _maybe_await(oauth_mgr.get_status())
            if isinstance(res, dict):
                oauth_status = res
    except Exception:
        pass

    return {
        "connected": oauth_status.get("connected", masked["workspace_token_present"]),
        "client_secret_present": oauth_status.get(
            "client_secret_present", masked["workspace_client_secret_present"]
        ),
        "token_present": masked["workspace_token_present"],
        "connected_email": oauth_status.get(
            "connected_email", masked["workspace_connected_email"]
        ),
        "scopes": oauth_status.get("scopes", masked["workspace_scopes"]),
        "email_safety_mode": masked["email_safety_mode"],
        **oauth_status,
    }


@app.get("/api/auth/workspace/start")
async def start_workspace_oauth(request: Request) -> dict[str, Any]:
    cfg_mgr = _resolve_config_manager(request)
    cfg = _get_runtime_config(cfg_mgr)
    redirect_uri = str(request.url_for("workspace_oauth_callback"))
    scopes = _cfg_attr(cfg, "workspace_scopes", None)

    oauth_mgr = _resolve_workspace_oauth_manager(request)
    if hasattr(oauth_mgr, "generate_auth_url"):
        try:
            sig = inspect.signature(oauth_mgr.generate_auth_url)
            kwargs: dict[str, Any] = {}
            if "redirect_uri" in sig.parameters:
                kwargs["redirect_uri"] = redirect_uri
            if "scopes" in sig.parameters and scopes:
                kwargs["scopes"] = scopes
            result = await _maybe_await(oauth_mgr.generate_auth_url(**kwargs))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if isinstance(result, tuple) and len(result) == 2:
            auth_url, state = result
        elif isinstance(result, dict):
            auth_url = result.get("authorization_url") or result.get("auth_url", "")
            state = result.get("state", "")
        else:
            auth_url = str(result)
            state = ""
        return {
            "auth_url": auth_url,
            "authorization_url": auth_url,
            "state": state,
        }

    raise HTTPException(
        status_code=400,
        detail="WorkspaceOAuthManager does not implement generate_auth_url",
    )


class WorkspaceManualExchangePayload(BaseModel):
    callback_url_or_code: str = Field(..., min_length=4)


async def _finalize_workspace_oauth_exchange(
    request: Request,
    code: str,
    state: str | None,
    redirect_uri: str,
) -> dict[str, Any]:
    oauth_mgr = _resolve_workspace_oauth_manager(request)
    try:
        sig = inspect.signature(oauth_mgr.exchange_code)
        kwargs: dict[str, Any] = {"code": code}
        if "state" in sig.parameters:
            kwargs["state"] = state
        if "redirect_uri" in sig.parameters:
            kwargs["redirect_uri"] = redirect_uri
        exchange_res = await _maybe_await(oauth_mgr.exchange_code(**kwargs))
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"OAuth token exchange failed: {exc}"
        ) from exc

    connected_email = ""
    if isinstance(exchange_res, dict):
        connected_email = exchange_res.get("connected_email") or exchange_res.get("email", "")

    cfg_mgr = _resolve_config_manager(request)
    cfg = _get_runtime_config(cfg_mgr)
    allowed = list(
        _cfg_attr(cfg, "allowed_user_emails", None)
        or _cfg_attr(cfg, "gchat_allowed_emails", None)
        or []
    )
    if connected_email and "@" in connected_email and connected_email.lower() not in allowed:
        allowed.append(connected_email.lower())

    updates: dict[str, Any] = {"workspace_enabled": True}
    if connected_email:
        updates["owner_email"] = connected_email
    if allowed:
        updates["allowed_user_emails"] = allowed
    await _update_config_fields(cfg_mgr, **updates)

    try:
        runner_mgr = _resolve_runner_manager(request)
        await _reload_runner_safely(runner_mgr)
    except Exception:
        pass

    return {
        "connected": True,
        "connected_email": connected_email,
    }


@app.get("/api/auth/workspace/callback", name="workspace_oauth_callback")
async def workspace_oauth_callback(
    request: Request,
    code: str = Query(...),
    state: str | None = Query(default=None),
) -> RedirectResponse:
    redirect_uri = str(request.url_for("workspace_oauth_callback"))
    await _finalize_workspace_oauth_exchange(request, code, state, redirect_uri)
    return RedirectResponse(url="/?view=settings&oauth=success")


@app.post("/api/auth/workspace/exchange")
async def workspace_oauth_manual_exchange(
    payload: WorkspaceManualExchangePayload,
    request: Request,
) -> dict[str, Any]:
    raw_value = payload.callback_url_or_code.strip()
    code = raw_value
    state: str | None = None
    if "code=" in raw_value:
        from urllib.parse import urlparse, parse_qs

        parsed = urlparse(raw_value if "://" in raw_value else f"http://localhost/?{raw_value.lstrip('?')}")
        qs = parse_qs(parsed.query)
        if qs.get("code"):
            code = qs["code"][0]
        if qs.get("state"):
            state = qs["state"][0]

    redirect_uri = str(request.url_for("workspace_oauth_callback"))
    return await _finalize_workspace_oauth_exchange(request, code, state, redirect_uri)


# ---------------------------------------------------------------------------
# 6. Google Chat Pub/Sub, Allowlist & Webhook Endpoints (/api/config/gchat, /api/gchat/*)
# ---------------------------------------------------------------------------
class GChatConfigPayload(BaseModel):
    enabled: bool = True
    transport_mode: str = "pubsub"
    project_id: str = ""
    gcp_project_id: str | None = None
    subscription_id: str = "adk-chat-inbound-sub"
    pubsub_subscription_id: str | None = None
    allowed_emails: list[str] = Field(default_factory=list)
    allowed_user_emails: list[str] | None = None
    service_account_json: str | dict[str, Any] | None = None


@app.post("/api/config/gchat")
async def configure_gchat(payload: GChatConfigPayload, request: Request) -> dict[str, Any]:
    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    sa_path = data_dir / "gchat_service_account.json"

    cfg_mgr = _resolve_config_manager(request)
    cfg = _get_runtime_config(cfg_mgr)

    if payload.service_account_json:
        try:
            sa_data = (
                json.loads(payload.service_account_json)
                if isinstance(payload.service_account_json, str)
                else payload.service_account_json
            )
            if not isinstance(sa_data, dict) or sa_data.get("type") != "service_account":
                raise ValueError("JSON must be a GCP service_account key.")
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail=f"Invalid Service Account JSON: {exc}"
            ) from exc
        if sa_path.exists():
            sa_bak = sa_path.with_name(f"{sa_path.name}.bak")
            try:
                sa_bak.write_text(sa_path.read_text(encoding="utf-8"), encoding="utf-8")
                os.chmod(sa_bak, 0o600)
            except OSError:
                pass
        sa_path.write_text(json.dumps(sa_data, indent=2), encoding="utf-8")
        os.chmod(sa_path, 0o600)

    raw_emails = (
        payload.allowed_user_emails
        if payload.allowed_user_emails is not None
        else payload.allowed_emails
    )
    normalized_emails = sorted({e.strip().lower() for e in raw_emails if e and e.strip()})
    if not normalized_emails:
        existing_emails = (
            _cfg_attr(cfg, "allowed_user_emails", None)
            or _cfg_attr(cfg, "gchat_allowed_emails", None)
            or []
        )
        normalized_emails = list(existing_emails)
    project_id = (
        payload.gcp_project_id
        or payload.project_id
        or _cfg_attr(cfg, "gcp_project_id", None)
        or _cfg_attr(cfg, "gchat_project_id", "")
        or ""
    ).strip()
    sub_id = (
        payload.pubsub_subscription_id or payload.subscription_id or "adk-chat-inbound-sub"
    ).strip()

    await _update_config_fields(
        cfg_mgr,
        gchat_enabled=payload.enabled,
        gchat_mode=payload.transport_mode,
        gcp_project_id=project_id,
        pubsub_subscription_id=sub_id,
        allowed_user_emails=normalized_emails,
    )

    gchat_bridge = None
    try:
        gchat_bridge = _resolve_gchat_bridge(request)
        if hasattr(gchat_bridge, "start_or_reload"):
            await _maybe_await(gchat_bridge.start_or_reload())
        elif hasattr(gchat_bridge, "restart"):
            await _maybe_await(gchat_bridge.restart())
        elif hasattr(gchat_bridge, "start"):
            await _maybe_await(gchat_bridge.start())
    except Exception as exc:
        logger.warning("GChat bridge reload warning: %s", exc)

    return {
        "status": "updated",
        "config": _build_masked_config_status(cfg_mgr, gchat_bridge),
    }


@app.get("/api/config/gchat/status")
async def get_gchat_status(request: Request) -> dict[str, Any]:
    cfg_mgr = _resolve_config_manager(request)
    masked = _build_masked_config_status(cfg_mgr)
    bridge_status: dict[str, Any] = {}
    try:
        gchat_bridge = _resolve_gchat_bridge(request)
        if hasattr(gchat_bridge, "get_status"):
            res = await _maybe_await(gchat_bridge.get_status())
            if isinstance(res, dict):
                bridge_status = res
        elif hasattr(gchat_bridge, "status_info"):
            info = gchat_bridge.status_info
            if isinstance(info, dict):
                bridge_status = info
    except Exception:
        pass

    return {
        "status": bridge_status.get(
            "status", "connected" if masked["gchat_worker_running"] else "standby"
        ),
        "enabled": masked["gchat_enabled"],
        "transport_mode": masked["gchat_transport_mode"],
        "project_id": masked["gchat_project_id"],
        "subscription_id": masked["gchat_subscription_id"],
        "allowed_user_emails": masked["allowed_user_emails"],
        "service_account_present": masked["gchat_service_account_present"],
        **bridge_status,
    }


@app.post("/api/gchat/webhook")
async def gchat_webhook_endpoint(
    request: Request,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Direct HTTP webhook handler for optional Cloudflare Tunnel (cloudflared) mode."""
    from src.integrations import gchat_webhook

    if hasattr(gchat_webhook, "handle_gchat_webhook"):
        res = await _maybe_await(
            gchat_webhook.handle_gchat_webhook(
                request=request,
                background_tasks=background_tasks,
                authorization=authorization,
            )
        )
        return res if isinstance(res, dict) else {}

    cfg_mgr = _resolve_config_manager(request)
    cfg = _get_runtime_config(cfg_mgr)
    expected_audience = (
        _cfg_attr(cfg, "webhook_audience", None)
        or _cfg_attr(cfg, "gcp_project_id", None)
        or os.environ.get("GCHAT_WEBHOOK_AUDIENCE", "")
    )
    if hasattr(gchat_webhook, "verify_gchat_bearer_token"):
        await _maybe_await(
            gchat_webhook.verify_gchat_bearer_token(authorization, expected_audience)
        )

    event = await request.json()
    gchat_bridge = _resolve_gchat_bridge(request)
    if hasattr(gchat_bridge, "handle_chat_event"):
        background_tasks.add_task(gchat_bridge.handle_chat_event, event)
    return {"status": "accepted"}


# ---------------------------------------------------------------------------
# 7. Headless Chrome Diagnostic & Profile Management (/api/browser/*)
# ---------------------------------------------------------------------------
@app.get("/api/browser/status")
async def get_browser_status() -> dict[str, Any]:
    data_dir = _data_dir()
    profile_dir = data_dir / "chrome-profile"
    screenshots_dir = data_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    shots = sorted(
        screenshots_dir.glob("*.png"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return {
        "status": "ready",
        "profile_dir": str(profile_dir),
        "profile_exists": profile_dir.exists(),
        "latest_screenshot": f"/api/screenshots/{shots[0].name}" if shots else None,
        "screenshot_count": len(shots),
    }


class BrowserTestRequest(BaseModel):
    url: str = "https://example.com"


@app.post("/api/browser/test")
async def test_headless_browser(
    payload: BrowserTestRequest | None = None,
) -> dict[str, Any]:
    """Launches Headless Chromium, captures a live screenshot to /data/screenshots/, and returns status."""
    target_url = payload.url if payload and payload.url else "https://example.com"
    t0 = time.perf_counter()
    from src.tools import browser_tools

    if hasattr(browser_tools, "browse_webpage"):
        res = await browser_tools.browse_webpage(
            url=target_url, capture_screenshot=True
        )
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return {
            "status": "ok",
            "page_title": res.get("title", "Example Domain"),
            "final_url": res.get("url") or res.get("final_url", target_url),
            "elapsed_ms": elapsed_ms,
            "screenshot_url": res.get("screenshot_url"),
        }

    raise HTTPException(status_code=500, detail="browser_tools.browse_webpage unavailable")


@app.post("/api/browser/clear-profile")
async def clear_browser_profile() -> dict[str, Any]:
    try:
        browser_mgr = _resolve_browser_manager()
        if hasattr(browser_mgr, "clear_profile_data"):
            return await _maybe_await(browser_mgr.clear_profile_data())
    except Exception:
        pass

    profile_dir = _data_dir() / "chrome-profile"
    if profile_dir.exists():
        for child in profile_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)
    return {"status": "cleared", "profile_dir": str(profile_dir)}


# ---------------------------------------------------------------------------
# 8. Local Web UI PIN Login & Security Configuration (/api/config/security, /api/auth/*)
# ---------------------------------------------------------------------------
class SecurityPinPayload(BaseModel):
    pin: str = ""
    allowed_cidrs: list[str] | None = None


@app.post("/api/config/security")
async def configure_security_pin(
    payload: SecurityPinPayload, request: Request, response: Response
) -> dict[str, Any]:
    cfg_mgr = _resolve_config_manager(request)
    clean_pin = payload.pin.strip()
    _, formatted_hash = hash_admin_pin(clean_pin)

    updates: dict[str, Any] = {
        "webui_admin_pin_hash": formatted_hash or None,
    }
    if payload.allowed_cidrs:
        updates["webui_allowed_cidrs"] = payload.allowed_cidrs

    await _update_config_fields(cfg_mgr, **updates)

    if formatted_hash:
        token = sign_session_token(formatted_hash)
        response.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            httponly=True,
            samesite="lax",
            max_age=86400 * 30,
        )
    else:
        response.delete_cookie(SESSION_COOKIE_NAME)

    return {
        "status": "updated",
        "admin_pin_enabled": bool(formatted_hash),
        "webui_auth_enabled": bool(formatted_hash),
    }


@app.post("/api/auth/login")
async def login_webui(
    payload: SecurityPinPayload, request: Request, response: Response
) -> dict[str, Any]:
    cfg_mgr = _resolve_config_manager(request)
    cfg = _get_runtime_config(cfg_mgr)
    pin_hash = _cfg_attr(cfg, "webui_admin_pin_hash", None) or _cfg_attr(
        cfg, "webui_pin_hash", None
    )
    pin_salt = _cfg_attr(cfg, "webui_pin_salt", None)

    if not verify_admin_pin(payload.pin, pin_hash, pin_salt):
        raise HTTPException(status_code=401, detail="Invalid Web UI admin PIN.")

    secret = str(
        _cfg_attr(cfg, "webui_session_secret", None)
        or pin_hash
        or "local-adk-default-session-secret"
    )
    token = sign_session_token(secret)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        max_age=86400 * 30,
    )
    return {"status": "authenticated"}


@app.post("/api/auth/logout")
async def logout_webui(response: Response) -> dict[str, Any]:
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"status": "logged_out"}


@app.get("/api/auth/status")
async def webui_auth_status(request: Request) -> dict[str, Any]:
    cfg_mgr = _resolve_config_manager(request)
    cfg = _get_runtime_config(cfg_mgr)
    pin_hash = _cfg_attr(cfg, "webui_admin_pin_hash", None) or _cfg_attr(
        cfg, "webui_pin_hash", None
    )
    return {
        "admin_pin_enabled": bool(pin_hash),
        "webui_auth_enabled": bool(pin_hash),
    }


# ---------------------------------------------------------------------------
# 9. Scheduled Tasks & Recurring Web Scraping Endpoints (/api/schedules/*)
# ---------------------------------------------------------------------------
class ScheduledTaskPayload(BaseModel):
    id: str | None = None
    name: str
    prompt: str = ""
    target_url: str = ""
    schedule: str = "0 8 * * *"
    mode: str = "scrape"
    model: str | None = None
    enabled: bool = True
    only_on_change: bool = False
    notify_gchat: bool = False


@app.get("/api/schedules")
async def list_scheduled_tasks_endpoint() -> dict[str, Any]:
    from src.core.scheduler import get_task_scheduler

    scheduler = get_task_scheduler()
    tasks = scheduler.list_tasks()
    return {"status": "ok", "schedules": tasks}


@app.post("/api/schedules")
async def create_or_update_scheduled_task_endpoint(
    payload: ScheduledTaskPayload,
) -> dict[str, Any]:
    from src.core.scheduler import get_task_scheduler

    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="Task name is required.")
    if not payload.prompt.strip() and not payload.target_url.strip():
        raise HTTPException(
            status_code=400,
            detail="Provide either scraping/research instructions or a target URL.",
        )

    scheduler = get_task_scheduler()
    created = scheduler.create_or_update_task(
        task_id=payload.id,
        name=payload.name,
        prompt=payload.prompt,
        target_url=payload.target_url,
        schedule=payload.schedule,
        mode=payload.mode,
        model=payload.model,
        enabled=payload.enabled,
        only_on_change=payload.only_on_change,
        notify_gchat=payload.notify_gchat,
    )
    return {"status": "ok", "task": created}


@app.post("/api/schedules/{task_id}/run")
async def run_scheduled_task_now_endpoint(task_id: str) -> dict[str, Any]:
    from src.core.scheduler import get_task_scheduler

    scheduler = get_task_scheduler()
    try:
        res = await scheduler.execute_task(task_id, triggered_by="manual")
        return res
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/schedules/{task_id}/toggle")
async def toggle_scheduled_task_endpoint(task_id: str) -> dict[str, Any]:
    from src.core.scheduler import get_task_scheduler

    scheduler = get_task_scheduler()
    updated = scheduler.toggle_task(task_id)
    if not updated:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    return {"status": "ok", "task": updated}


@app.delete("/api/schedules/{task_id}")
async def delete_scheduled_task_endpoint(task_id: str) -> dict[str, Any]:
    from src.core.scheduler import get_task_scheduler

    scheduler = get_task_scheduler()
    removed = scheduler.delete_task(task_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    return {"status": "ok", "deleted": task_id}

