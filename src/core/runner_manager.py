"""Hot-reloadable ADK Runner singleton and SQLite DatabaseSessionService manager."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
import inspect
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, AsyncGenerator, Callable, Optional
import uuid

from ..agents.root_agent import (
    _discover_browser_tools,
    _discover_workspace_tools,
    build_personal_assistant_root_agent,
    types,
)
from .config_store import (
    ConfigManager,
    RuntimeConfig,
    get_config_manager,
)

logger = logging.getLogger(__name__)

APP_NAME = "local_adk_agent"


def normalize_async_sqlite_url(db_url: str) -> str:
    """Ensures SQLite URLs use the async aiosqlite driver required by DatabaseSessionService.

    Accepts either:
      - 'sqlite:///data/sessions.db' -> 'sqlite+aiosqlite:////data/sessions.db'
      - 'sqlite:////data/sessions.db' -> 'sqlite+aiosqlite:////data/sessions.db'
      - 'sqlite+aiosqlite:////data/sessions.db' -> unchanged
    """
    cleaned = db_url.strip()
    if cleaned.startswith("sqlite:///"):
        path_part = cleaned[len("sqlite:///") :]
        if not path_part.startswith("/"):
            path_part = "/" + path_part
        return f"sqlite+aiosqlite:///{path_part}"
    if cleaned.startswith("sqlite+aiosqlite:///") and not cleaned.startswith(
        "sqlite+aiosqlite:////"
    ):
        path_part = cleaned[len("sqlite+aiosqlite:///") :]
        return f"sqlite+aiosqlite:////{path_part}"
    return cleaned


def _configure_sqlite_wal(dbapi_conn: Any, connection_record: Any = None) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


class _FallbackSessionObj:
    """Lightweight session record compatible with ADK Session interface."""

    def __init__(
        self,
        session_id: str,
        app_name: str,
        user_id: str,
        state: Optional[dict[str, Any]] = None,
        events: Optional[list[Any]] = None,
        last_update_time: Optional[float] = None,
    ) -> None:
        self.id = session_id
        self.app_name = app_name
        self.user_id = user_id
        self.state: dict[str, Any] = dict(state or {})
        self.events: list[Any] = list(events or [])
        self.last_update_time: float = last_update_time or time.time()


class _FallbackSessionsListResponse:
    def __init__(self, sessions: list[_FallbackSessionObj]) -> None:
        self.sessions = sessions


class _SqliteFallbackSessionService:
    """SQLite WAL-backed fallback session service when google-adk[db]/aiosqlite is unavailable."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=5.0)
        _configure_sqlite_wal(conn)
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS adk_sessions_fallback (
                        app_name TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        state_json TEXT NOT NULL,
                        events_json TEXT NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (app_name, user_id, session_id)
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()
            try:
                os.chmod(self._db_path, 0o600)
            except OSError:
                pass

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: Optional[str] = None,
        state: Optional[dict[str, Any]] = None,
    ) -> _FallbackSessionObj:
        sid = session_id or f"sess_{uuid.uuid4().hex[:12]}"
        now = time.time()
        st = dict(state or {})
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO adk_sessions_fallback
                    (app_name, user_id, session_id, state_json, events_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (app_name, user_id, sid, json.dumps(st), json.dumps([]), now),
                )
                conn.commit()
            finally:
                conn.close()
        return _FallbackSessionObj(
            session_id=sid,
            app_name=app_name,
            user_id=user_id,
            state=st,
            events=[],
            last_update_time=now,
        )

    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
    ) -> Optional[_FallbackSessionObj]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT user_id, state_json, events_json, updated_at
                    FROM adk_sessions_fallback
                    WHERE app_name = ? AND session_id = ? AND (user_id = ? OR ? = '*')
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (app_name, session_id, user_id, user_id),
                ).fetchone()
                if not row:
                    return None
                actual_user_id, state_json, events_json, updated_at = row
                return _FallbackSessionObj(
                    session_id=session_id,
                    app_name=app_name,
                    user_id=actual_user_id,
                    state=json.loads(state_json),
                    events=json.loads(events_json),
                    last_update_time=float(updated_at),
                )
            finally:
                conn.close()

    async def list_sessions(
        self,
        *,
        app_name: str,
        user_id: Optional[str] = None,
    ) -> _FallbackSessionsListResponse:
        with self._lock:
            conn = self._connect()
            try:
                if user_id and user_id != "*":
                    rows = conn.execute(
                        """
                        SELECT session_id, user_id, state_json, events_json, updated_at
                        FROM adk_sessions_fallback
                        WHERE app_name = ? AND user_id = ?
                        ORDER BY updated_at DESC
                        """,
                        (app_name, user_id),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT session_id, user_id, state_json, events_json, updated_at
                        FROM adk_sessions_fallback
                        WHERE app_name = ?
                        ORDER BY updated_at DESC
                        """,
                        (app_name,),
                    ).fetchall()
                sessions = [
                    _FallbackSessionObj(
                        session_id=r[0],
                        app_name=app_name,
                        user_id=r[1],
                        state=json.loads(r[2]),
                        events=json.loads(r[3]),
                        last_update_time=float(r[4]),
                    )
                    for r in rows
                ]
                return _FallbackSessionsListResponse(sessions)
            finally:
                conn.close()

    async def append_serialized_event(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        event_dict: dict[str, Any],
        state_updates: Optional[dict[str, Any]] = None,
    ) -> None:
        existing = await self.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id
        )
        if existing is None:
            existing = await self.create_session(
                app_name=app_name,
                user_id=user_id,
                session_id=session_id,
                state=state_updates or {},
            )
        events = list(existing.events)
        events.append(event_dict)
        state = dict(existing.state)
        if state_updates:
            state.update(state_updates)
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    UPDATE adk_sessions_fallback
                    SET state_json = ?, events_json = ?, updated_at = ?
                    WHERE app_name = ? AND user_id = ? AND session_id = ?
                    """,
                    (
                        json.dumps(state),
                        json.dumps(events),
                        now,
                        app_name,
                        existing.user_id,
                        session_id,
                    ),
                )
                conn.commit()
            finally:
                conn.close()


class AdkRunnerManager:
    """Manages the singleton DatabaseSessionService and hot-reloadable ADK Runner."""

    _singleton: Optional[AdkRunnerManager] = None
    _singleton_lock = threading.RLock()

    def __init__(
        self,
        config_manager: Optional[ConfigManager] = None,
        db_url: Optional[str] = None,
        workspace_tools_provider: Optional[Callable[[RuntimeConfig], list[Any]]] = None,
        browser_tools_provider: Optional[Callable[[RuntimeConfig], list[Any]]] = None,
    ) -> None:
        self._config_manager = config_manager or get_config_manager()
        data_root = self._config_manager._config_path.parent
        self._db_path = data_root / "sessions.db"
        raw_db_url = db_url or f"sqlite:///{self._db_path}"
        self._db_url = normalize_async_sqlite_url(raw_db_url)
        self._workspace_tools_provider = workspace_tools_provider
        self._browser_tools_provider = browser_tools_provider
        self._rebuild_lock = asyncio.Lock()
        self._reload_count = 0

        self._fallback_session_service = _SqliteFallbackSessionService(self._db_path)
        self.session_service: Any = self._init_session_service()
        self.memory_service: Any = self._init_memory_service()
        self.artifact_service: Any = self._init_artifact_service()

        self._runner: Optional[Any] = None
        self._config_manager.register_listener(self._on_config_updated)

    def _init_session_service(self) -> Any:
        try:
            from google.adk.sessions import DatabaseSessionService
            from sqlalchemy import event as sa_event

            svc = DatabaseSessionService(db_url=self._db_url)
            if hasattr(svc, "db_engine") and hasattr(svc.db_engine, "sync_engine"):
                sa_event.listen(
                    svc.db_engine.sync_engine,
                    "connect",
                    _configure_sqlite_wal,
                )
            return svc
        except Exception as exc:
            logger.info(
                "Using embedded SQLite WAL session service at %s (%s)",
                self._db_path,
                exc,
            )
            return self._fallback_session_service

    def _init_memory_service(self) -> Any:
        try:
            from google.adk.memory.in_memory_memory_service import InMemoryMemoryService

            return InMemoryMemoryService()
        except Exception:
            return None

    def _init_artifact_service(self) -> Any:
        try:
            from google.adk.artifacts.in_memory_artifact_service import (
                InMemoryArtifactService,
            )

            return InMemoryArtifactService()
        except Exception:
            return None

    @property
    def reload_count(self) -> int:
        return self._reload_count

    def _assemble_runner(
        self,
        cfg: RuntimeConfig,
        model_override: Optional[str] = None,
    ) -> Any:
        api_key = self._config_manager.get_gemini_api_key()
        workspace_tools = (
            self._workspace_tools_provider(cfg)
            if self._workspace_tools_provider
            else _discover_workspace_tools(cfg)
        )
        browser_tools = (
            self._browser_tools_provider(cfg)
            if self._browser_tools_provider
            else _discover_browser_tools(cfg)
        )

        root_agent = build_personal_assistant_root_agent(
            config=cfg,
            api_key=api_key,
            model_override=model_override,
            workspace_tools=workspace_tools,
            browser_tools=browser_tools,
        )

        try:
            from google.adk import Runner

            kwargs: dict[str, Any] = {
                "app_name": APP_NAME,
                "agent": root_agent,
                "session_service": self.session_service,
            }
            if self.memory_service is not None:
                kwargs["memory_service"] = self.memory_service
            if self.artifact_service is not None:
                kwargs["artifact_service"] = self.artifact_service
            sig = inspect.signature(Runner.__init__)
            if "auto_create_session" in sig.parameters:
                kwargs["auto_create_session"] = True
            return Runner(**kwargs)
        except Exception as exc:
            logger.debug("ADK Runner initialized in lightweight mode: %s", exc)
            return {
                "app_name": APP_NAME,
                "agent": root_agent,
                "session_service": self.session_service,
            }

    async def get_runner(self, model_override: Optional[str] = None) -> Any:
        """Returns the current hot-reloaded Runner (or builds a model-overridden Runner)."""
        async with self._rebuild_lock:
            if (
                model_override
                and model_override != self._config_manager.config.default_model
            ):
                return self._assemble_runner(
                    self._config_manager.config, model_override=model_override
                )
            if self._runner is None:
                self._runner = self._assemble_runner(self._config_manager.config)
            return self._runner

    async def _on_config_updated(self, new_config: RuntimeConfig) -> None:
        """Callback triggered whenever /data/config.json is updated via Web UI."""
        async with self._rebuild_lock:
            logger.info(
                "Hot-reloading ADK Runner (model=%s, workspace=%s, gchat=%s)",
                new_config.default_model,
                new_config.workspace_enabled,
                new_config.gchat_enabled,
            )
            self._runner = self._assemble_runner(new_config)
            self._reload_count += 1

    async def reload_runner(self) -> None:
        """Hot-reloads the ADK Runner and toolsets in-place without dropping SQLite sessions."""
        await self._on_config_updated(self._config_manager.config)

    async def reload(self) -> None:
        """Alias for reload_runner()."""
        await self.reload_runner()

    async def reload_tools_and_runner(self) -> None:
        """Alias for reload_runner()."""
        await self.reload_runner()

    async def _ensure_session_metadata(
        self,
        *,
        user_id: str,
        session_id: str,
        origin_channel: str,
        title_hint: str,
        model_name: str,
    ) -> None:
        title = title_hint.strip()[:60] or f"Session {session_id[:8]}"
        state_init = {
            "origin_channel": origin_channel,
            "title": title,
            "model": model_name,
            "user_email": user_id,
        }
        try:
            existing = await self.session_service.get_session(
                app_name=APP_NAME, user_id=user_id, session_id=session_id
            )
            if existing is None:
                await self.session_service.create_session(
                    app_name=APP_NAME,
                    user_id=user_id,
                    session_id=session_id,
                    state=state_init,
                )
        except Exception as exc:
            logger.debug("Fallback session metadata recording: %s", exc)
            await self._fallback_session_service.create_session(
                app_name=APP_NAME,
                user_id=user_id,
                session_id=session_id,
                state=state_init,
            )

    async def stream_chat_turn(
        self,
        *,
        session_id: Optional[str] = None,
        user_message: str = "",
        prompt: Optional[str] = None,
        user_id: Optional[str] = None,
        deep_research: bool = False,
        model_override: Optional[str] = None,
        origin_channel: str = "webui",
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Executes a user turn against the ADK Runner and yields structured SSE/GChat events."""
        cfg = self._config_manager.config
        resolved_session_id = session_id or f"sess_{uuid.uuid4().hex[:12]}"
        resolved_message = (user_message or prompt or "").strip()
        resolved_user_id = (
            user_id
            or cfg.workspace_connected_email
            or cfg.owner_email
            or "local_owner"
        )
        effective_model = model_override or cfg.default_model

        await self._ensure_session_metadata(
            user_id=resolved_user_id,
            session_id=resolved_session_id,
            origin_channel=origin_channel,
            title_hint=resolved_message,
            model_name=effective_model,
        )

        await self._fallback_session_service.append_serialized_event(
            app_name=APP_NAME,
            user_id=resolved_user_id,
            session_id=resolved_session_id,
            event_dict={
                "id": f"ev_user_{uuid.uuid4().hex[:8]}",
                "author": "user",
                "role": "user",
                "text": resolved_message,
                "timestamp": time.time(),
                "tool_calls": [],
                "tool_responses": [],
                "grounding_queries": [],
                "grounding_sources": [],
            },
            state_updates={
                "origin_channel": origin_channel,
                "model": effective_model,
            },
        )

        api_key = self._config_manager.get_gemini_api_key()
        if not api_key:
            err_msg = (
                "GEMINI_API_KEY is not configured. Open Settings in the Web UI "
                "to add your Google AI Studio API key."
            )
            yield {
                "type": "error",
                "session_id": resolved_session_id,
                "author": "system",
                "error": err_msg,
                "message": err_msg,
                "text": f"⚠️ {err_msg}",
                "tool_calls": [],
                "tool_responses": [],
                "grounding_queries": [],
                "grounding_sources": [],
            }
            return

        runner = await self.get_runner(model_override=model_override)
        if not hasattr(runner, "run_async"):
            err_msg = "google-adk Runner runtime is not active in this environment."
            yield {
                "type": "error",
                "session_id": resolved_session_id,
                "author": "system",
                "error": err_msg,
                "message": err_msg,
                "text": err_msg,
                "tool_calls": [],
                "tool_responses": [],
                "grounding_queries": [],
                "grounding_sources": [],
            }
            return

        effective_text = resolved_message
        if deep_research:
            effective_text = (
                "[DEEP RESEARCH MODE ENABLED: Use `deep_research_workflow` (or "
                "`run_gemini_deep_research_interaction` if configured) to conduct a "
                "thorough multi-query investigation and return a fully cited report.]\n\n"
                f"{resolved_message}"
            )

        content = types.Content(
            role="user",
            parts=[types.Part.from_text(text=effective_text)],
        )
        state_delta = {
            "app:gemini_api_key": api_key,
            "origin_channel": origin_channel,
            "model": effective_model,
        }

        run_kwargs: dict[str, Any] = {
            "user_id": resolved_user_id,
            "session_id": resolved_session_id,
            "new_message": content,
        }
        sig = inspect.signature(runner.run_async)
        if "state_delta" in sig.parameters:
            run_kwargs["state_delta"] = state_delta
        if "run_config" in sig.parameters:
            try:
                from google.adk.agents import RunConfig

                run_kwargs["run_config"] = RunConfig(streaming_mode="sse")
            except Exception:
                pass

        accumulated_text: list[str] = []
        accumulated_calls: list[dict[str, Any]] = []
        accumulated_sources: list[dict[str, str]] = []
        accumulated_queries: list[str] = []

        async with aclosing(runner.run_async(**run_kwargs)) as agen:
            async for event in agen:
                serialized = self._serialize_adk_event(
                    event, session_id=resolved_session_id
                )
                if serialized.get("text") and not serialized.get("partial"):
                    accumulated_text.append(serialized["text"])
                if serialized.get("tool_calls"):
                    accumulated_calls.extend(serialized["tool_calls"])
                if serialized.get("grounding_sources"):
                    accumulated_sources.extend(serialized["grounding_sources"])
                if serialized.get("grounding_queries"):
                    accumulated_queries.extend(serialized["grounding_queries"])
                yield serialized

        if accumulated_text or accumulated_calls:
            await self._fallback_session_service.append_serialized_event(
                app_name=APP_NAME,
                user_id=resolved_user_id,
                session_id=resolved_session_id,
                event_dict={
                    "id": f"ev_asst_{uuid.uuid4().hex[:8]}",
                    "author": "personal_adk_assistant",
                    "role": "model",
                    "text": "\n".join(accumulated_text),
                    "timestamp": time.time(),
                    "tool_calls": accumulated_calls,
                    "tool_responses": [],
                    "grounding_queries": accumulated_queries,
                    "grounding_sources": accumulated_sources,
                },
            )

    async def stream_turn(
        self,
        *,
        session_id: Optional[str] = None,
        prompt: str = "",
        user_message: str = "",
        user_id: Optional[str] = None,
        deep_research: bool = False,
        model_override: Optional[str] = None,
        origin_channel: str = "webui",
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Alias for stream_chat_turn supporting both `prompt` and `user_message` parameters."""
        async for ev in self.stream_chat_turn(
            session_id=session_id,
            user_message=user_message or prompt,
            user_id=user_id,
            deep_research=deep_research,
            model_override=model_override,
            origin_channel=origin_channel,
        ):
            yield ev

    async def run_turn_text(
        self,
        *,
        session_id: str,
        user_message: str,
        user_id: Optional[str] = None,
        deep_research: bool = False,
        model_override: Optional[str] = None,
        origin_channel: str = "gchat",
        on_tool_call: Optional[Callable[[str], Any]] = None,
    ) -> dict[str, Any]:
        """Executes a full turn and returns the aggregated final text, tool trace, and citations."""
        text_parts: list[str] = []
        partial_buffer: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        seen_tools: list[str] = []
        grounding_sources: list[dict[str, str]] = []
        grounding_queries: list[str] = []
        last_screenshot: Optional[dict[str, Any]] = None

        async for event in self.stream_chat_turn(
            session_id=session_id,
            user_message=user_message,
            user_id=user_id,
            deep_research=deep_research,
            model_override=model_override,
            origin_channel=origin_channel,
        ):
            if event.get("type") == "error":
                return {
                    "session_id": session_id,
                    "status": "error",
                    "text": event.get("text") or event.get("error", "Error"),
                    "error": event.get("error"),
                    "tool_calls": tool_calls,
                    "grounding_sources": grounding_sources,
                    "grounding_queries": grounding_queries,
                }

            for tc in event.get("tool_calls", []):
                tool_calls.append(tc)
                t_name = tc.get("name", "")
                if t_name and t_name not in seen_tools:
                    seen_tools.append(t_name)
                    if on_tool_call is not None:
                        cb_res = on_tool_call(t_name)
                        if inspect.isawaitable(cb_res):
                            await cb_res

            if event.get("grounding_sources"):
                for src in event["grounding_sources"]:
                    if src not in grounding_sources:
                        grounding_sources.append(src)
            if event.get("grounding_queries"):
                for q in event["grounding_queries"]:
                    if q not in grounding_queries:
                        grounding_queries.append(q)
            if event.get("browser_screenshot"):
                last_screenshot = event["browser_screenshot"]

            chunk_text = event.get("text", "")
            if chunk_text:
                if event.get("partial"):
                    partial_buffer.append(chunk_text)
                else:
                    text_parts.append(chunk_text)

        final_text = "\n\n".join(text_parts).strip()
        if not final_text and partial_buffer:
            final_text = "".join(partial_buffer).strip()

        return {
            "session_id": session_id,
            "status": "ok",
            "text": final_text,
            "tool_calls": tool_calls,
            "tool_names": seen_tools,
            "grounding_sources": grounding_sources,
            "grounding_queries": grounding_queries,
            "browser_screenshot": last_screenshot,
        }

    def _serialize_adk_event(
        self,
        event: Any,
        session_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Normalizes an ADK Event (or dict event) into a UI- and GChat-friendly dictionary."""
        if isinstance(event, dict):
            out = dict(event)
            if session_id and "session_id" not in out:
                out["session_id"] = session_id
            out.setdefault("type", "event")
            return out

        payload: dict[str, Any] = {
            "type": "event",
            "session_id": session_id,
            "id": getattr(event, "id", f"ev_{uuid.uuid4().hex[:8]}"),
            "invocation_id": getattr(event, "invocation_id", None),
            "author": getattr(event, "author", "assistant"),
            "partial": bool(getattr(event, "partial", False)),
            "timestamp": getattr(event, "timestamp", time.time()),
            "text": "",
            "tool_calls": [],
            "tool_responses": [],
            "grounding_queries": [],
            "grounding_sources": [],
        }

        content = getattr(event, "content", None)
        parts = getattr(content, "parts", None) if content else None
        if parts:
            text_chunks: list[str] = []
            for part in parts:
                if getattr(part, "text", None) and not getattr(part, "thought", False):
                    text_chunks.append(part.text)
                fn_call = getattr(part, "function_call", None)
                if fn_call:
                    payload["tool_calls"].append(
                        {
                            "id": getattr(fn_call, "id", None),
                            "name": getattr(fn_call, "name", ""),
                            "args": dict(getattr(fn_call, "args", None) or {}),
                        }
                    )
                fn_resp = getattr(part, "function_response", None)
                if fn_resp:
                    payload["tool_responses"].append(
                        {
                            "id": getattr(fn_resp, "id", None),
                            "name": getattr(fn_resp, "name", ""),
                            "response": getattr(fn_resp, "response", None),
                        }
                    )
            payload["text"] = "".join(text_chunks)

        gm = getattr(event, "grounding_metadata", None)
        if gm:
            payload["grounding_queries"] = list(
                getattr(gm, "web_search_queries", None) or []
            )
            chunks = getattr(gm, "grounding_chunks", None)
            if chunks:
                for chunk in chunks:
                    web = getattr(chunk, "web", None)
                    uri = getattr(web, "uri", None) if web else None
                    if uri:
                        payload["grounding_sources"].append(
                            {
                                "title": getattr(web, "title", None) or uri,
                                "uri": uri,
                            }
                        )

        actions = getattr(event, "actions", None)
        sd = getattr(actions, "state_delta", None) if actions else None
        if isinstance(sd, dict):
            if "last_grounding_sources" in sd and not payload["grounding_sources"]:
                payload["grounding_sources"] = sd["last_grounding_sources"]
            if "last_grounding_queries" in sd and not payload["grounding_queries"]:
                payload["grounding_queries"] = sd["last_grounding_queries"]
            if "last_browser_screenshot" in sd:
                payload["browser_screenshot"] = sd["last_browser_screenshot"]

        return payload

    def serialize_event_for_ui(self, event: Any) -> dict[str, Any]:
        return self._serialize_adk_event(event)

    async def list_sessions(
        self,
        user_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Lists all conversations across both Web UI and Google Chat from SQLite."""
        cfg = self._config_manager.config
        candidate_users: list[str] = []
        for u in (
            user_id,
            cfg.workspace_connected_email,
            cfg.owner_email,
            "local_owner",
            "owner",
            *cfg.allowed_user_emails,
        ):
            if u and u not in candidate_users:
                candidate_users.append(u)

        seen_ids: set[str] = set()
        items: list[dict[str, Any]] = []

        for svc in (self.session_service, self._fallback_session_service):
            for u in candidate_users:
                try:
                    resp = await svc.list_sessions(app_name=APP_NAME, user_id=u)
                    sessions_list = getattr(resp, "sessions", resp) or []
                    for s in sessions_list:
                        sid = getattr(s, "id", None)
                        if not sid or sid in seen_ids:
                            continue
                        seen_ids.add(sid)
                        st = getattr(s, "state", {}) or {}
                        origin = (
                            st.get("origin_channel")
                            or st.get("source")
                            or ("gchat" if str(sid).startswith("gchat_") else "webui")
                        )
                        if origin == "google_chat":
                            origin = "gchat"
                        title = st.get("title") or f"Session {str(sid)[:8]}"
                        updated = getattr(s, "last_update_time", None) or time.time()
                        items.append(
                            {
                                "id": sid,
                                "title": title,
                                "origin_channel": origin,
                                "model": st.get("model", cfg.default_model),
                                "user_id": getattr(s, "user_id", u),
                                "updated_at": float(updated),
                            }
                        )
                except Exception as exc:
                    logger.debug("list_sessions query skipped for %s: %s", u, exc)

        items.sort(key=lambda x: x["updated_at"], reverse=True)
        return items

    async def get_session_history(
        self,
        session_id: str,
        user_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Returns chronological turn history (including tool calls & citations) for a session."""
        cfg = self._config_manager.config
        candidate_users: list[str] = []
        for u in (
            user_id,
            cfg.workspace_connected_email,
            cfg.owner_email,
            "local_owner",
            "owner",
            *cfg.allowed_user_emails,
        ):
            if u and u not in candidate_users:
                candidate_users.append(u)

        session_obj: Any = None
        for svc in (self.session_service, self._fallback_session_service):
            for u in candidate_users:
                try:
                    s = await svc.get_session(
                        app_name=APP_NAME, user_id=u, session_id=session_id
                    )
                    if s is not None:
                        if session_obj is None or (
                            getattr(s, "events", None)
                            and not getattr(session_obj, "events", None)
                        ):
                            session_obj = s
                except Exception:
                    continue

        if session_obj is None:
            try:
                session_obj = await self._fallback_session_service.get_session(
                    app_name=APP_NAME, user_id="*", session_id=session_id
                )
            except Exception:
                pass

        if session_obj is None:
            return None

        st = getattr(session_obj, "state", {}) or {}
        origin = (
            st.get("origin_channel")
            or st.get("source")
            or ("gchat" if str(session_id).startswith("gchat_") else "webui")
        )
        if origin == "google_chat":
            origin = "gchat"
        raw_events = getattr(session_obj, "events", []) or []
        turns = [
            self._serialize_adk_event(ev, session_id=session_id) for ev in raw_events
        ]

        return {
            "id": session_obj.id,
            "title": st.get("title", f"Session {session_id[:8]}"),
            "origin_channel": origin,
            "model": st.get("model", cfg.default_model),
            "events": turns,
        }


def get_runner_manager() -> AdkRunnerManager:
    """Returns the process-wide AdkRunnerManager singleton."""
    with AdkRunnerManager._singleton_lock:
        cfg_mgr = get_config_manager()
        if (
            AdkRunnerManager._singleton is None
            or AdkRunnerManager._singleton._config_manager is not cfg_mgr
        ):
            AdkRunnerManager._singleton = AdkRunnerManager(config_manager=cfg_mgr)
        return AdkRunnerManager._singleton


def reset_runner_manager() -> None:
    """Resets the singleton instance (primarily for unit tests)."""
    with AdkRunnerManager._singleton_lock:
        AdkRunnerManager._singleton = None
