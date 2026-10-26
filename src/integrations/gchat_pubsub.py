"""Zero-Port Google Chat Integration via Cloud Pub/Sub Streaming Pull & REST API.

Connects to `projects/<project>/subscriptions/<sub_id>` over outbound HTTPS/gRPC,
enforces the owner `allowed_user_emails` security allowlist, immediately `ack()`s
inbound messages so long Deep Research or Headless Chrome runs never time out,
streams live tool-progress updates via `spaces.messages.patch`, and shares SQLite
session state (`/data/sessions.db`) with the local Web UI via `get_runner_manager()`.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Callable, Iterator, Optional

try:
    from google.oauth2 import service_account
except ImportError:  # pragma: no cover

    class _FallbackServiceAccountModule:
        class Credentials:
            @classmethod
            def from_service_account_file(
                cls, filename: str, scopes: Optional[list[str]] = None
            ) -> Any:
                return {"filename": filename, "scopes": scopes}

    service_account = _FallbackServiceAccountModule()  # type: ignore[assignment]

try:
    from googleapiclient.discovery import build
except ImportError:  # pragma: no cover

    def build(*args: Any, **kwargs: Any) -> Any:  # type: ignore[misc]
        raise RuntimeError("google-api-python-client is required for live Google Chat API calls.")


try:
    from google.cloud import pubsub_v1
except ImportError:  # pragma: no cover
    pubsub_v1 = None  # type: ignore[assignment]

try:
    from google.genai import types as genai_types
except ImportError:  # pragma: no cover
    genai_types = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

CHAT_BOT_SCOPES = ["https://www.googleapis.com/auth/chat.bot"]


def _get_data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", os.environ.get("ADK_DATA_DIR", "/data")))


class _DualSyncAsyncResult(dict):
    """Dictionary result that can be used synchronously or awaited in async routes."""

    def __await__(self) -> Iterator[Any]:
        async def _coro() -> _DualSyncAsyncResult:
            return self

        return _coro().__await__()


class GChatPubSubBridge:
    """Zero-port Google Chat integration using Cloud Pub/Sub streaming pull + REST API."""

    def __init__(
        self,
        config_loader: Optional[Callable[[], Any]] = None,
        adk_runner_getter: Optional[Callable[[], Any]] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        *,
        config_manager: Optional[Any] = None,
        runner_manager: Optional[Any] = None,
        data_dir: Optional[Path | str] = None,
    ) -> None:
        self._config_loader = config_loader
        self._config_manager = config_manager
        self._runner_getter = adk_runner_getter
        self._runner_manager = runner_manager
        self._loop = loop
        self._explicit_data_dir = Path(data_dir) if data_dir else None

        self._subscriber: Optional[Any] = None
        self._streaming_pull_future: Optional[Any] = None
        self._chat_service: Any = None
        self._status: str = "disconnected"
        self._last_error: Optional[str] = None
        self._lock = threading.RLock()

    @property
    def data_dir(self) -> Path:
        if self._explicit_data_dir is not None:
            return self._explicit_data_dir
        return _get_data_dir()

    @property
    def sa_key_path(self) -> Path:
        return self.data_dir / "gchat_service_account.json"

    @property
    def is_running(self) -> bool:
        return self._status == "connected" and self._streaming_pull_future is not None

    def _read_gchat_config(self) -> dict[str, Any]:
        """Normalizes Google Chat settings across RuntimeConfig, AppConfig, or dict."""
        raw: Any = None
        if self._config_loader is not None:
            raw = self._config_loader()
        elif self._config_manager is not None:
            raw = (
                self._config_manager.config
                if hasattr(self._config_manager, "config")
                else self._config_manager.get()
            )
        else:
            try:
                from src.core.config_store import get_config_manager

                mgr = get_config_manager()
                raw = mgr.config if hasattr(mgr, "config") else mgr.get()
            except Exception:
                raw = {}

        if isinstance(raw, dict):
            gchat_sub = raw.get("gchat", raw)
            enabled = bool(
                gchat_sub.get("enabled", raw.get("gchat_enabled", False))
            )
            mode = (
                gchat_sub.get("transport_mode")
                or gchat_sub.get("mode")
                or raw.get("gchat_mode")
                or raw.get("gchat_transport_mode")
                or "pubsub"
            )
            project_id = (
                gchat_sub.get("gcp_project_id")
                or gchat_sub.get("project_id")
                or raw.get("gcp_project_id")
                or raw.get("gchat_project_id")
                or ""
            )
            sub_id = (
                gchat_sub.get("pubsub_subscription_id")
                or gchat_sub.get("subscription_id")
                or raw.get("pubsub_subscription_id")
                or raw.get("gchat_subscription_id")
                or "adk-chat-inbound-sub"
            )
            allowed = (
                gchat_sub.get("allowed_user_emails")
                or gchat_sub.get("allowed_emails")
                or raw.get("allowed_user_emails")
                or raw.get("gchat_allowed_emails")
                or []
            )
            webhook_audience = (
                gchat_sub.get("webhook_audience")
                or raw.get("webhook_audience")
                or gchat_sub.get("gcp_project_number")
                or ""
            )
            return {
                "enabled": enabled,
                "transport_mode": str(mode).strip().lower(),
                "gcp_project_id": str(project_id or "").strip(),
                "pubsub_subscription_id": str(sub_id or "").strip(),
                "allowed_user_emails": [
                    str(e).strip().lower() for e in (allowed or []) if str(e).strip()
                ],
                "webhook_audience": str(webhook_audience or "").strip(),
            }

        enabled = bool(getattr(raw, "gchat_enabled", False))
        mode = (
            getattr(raw, "gchat_mode", None)
            or getattr(raw, "gchat_transport_mode", None)
            or "pubsub"
        )
        project_id = (
            getattr(raw, "gcp_project_id", None)
            or getattr(raw, "gchat_project_id", None)
            or ""
        )
        sub_id = (
            getattr(raw, "pubsub_subscription_id", None)
            or getattr(raw, "gchat_subscription_id", None)
            or "adk-chat-inbound-sub"
        )
        allowed = (
            getattr(raw, "allowed_user_emails", None)
            or getattr(raw, "gchat_allowed_emails", None)
            or []
        )
        webhook_audience = getattr(raw, "webhook_audience", None) or ""
        return {
            "enabled": enabled,
            "transport_mode": str(mode).strip().lower(),
            "gcp_project_id": str(project_id or "").strip(),
            "pubsub_subscription_id": str(sub_id or "").strip(),
            "allowed_user_emails": [
                str(e).strip().lower() for e in (allowed or []) if str(e).strip()
            ],
            "webhook_audience": str(webhook_audience or "").strip(),
        }

    def _resolve_runner_or_manager(self) -> Any:
        if self._runner_manager is not None:
            return self._runner_manager
        if self._runner_getter is not None:
            return self._runner_getter()
        from src.core.runner_manager import get_runner_manager

        return get_runner_manager()

    def is_user_allowed(self, sender_email: str) -> bool:
        """Strictly checks whether sender_email is in allowed_user_emails."""
        cleaned = (sender_email or "").strip().lower()
        if not cleaned:
            return False
        cfg = self._read_gchat_config()
        allowed = cfg.get("allowed_user_emails", [])
        return bool(allowed) and cleaned in allowed

    def save_service_account_json(self, raw_json_or_dict: str | dict[str, Any]) -> dict[str, Any]:
        """Validates and writes /data/gchat_service_account.json with 0600 permissions."""
        with self._lock:
            parsed = (
                json.loads(raw_json_or_dict)
                if isinstance(raw_json_or_dict, str)
                else raw_json_or_dict
            )
            if not isinstance(parsed, dict) or parsed.get("type") != "service_account":
                raise ValueError(
                    "Invalid Service Account JSON: must contain '\"type\": \"service_account\"'."
                )
            self.data_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self.data_dir), prefix=".gchat_sa.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps(parsed, indent=2))
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, self.sa_key_path)
                os.chmod(self.sa_key_path, 0o600)
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            return {"saved": True, "path": str(self.sa_key_path)}

    @property
    def status_info(self) -> dict[str, Any]:
        cfg = self._read_gchat_config()
        return {
            "status": self._status,
            "running": self.is_running,
            "is_running": self.is_running,
            "enabled": cfg["enabled"],
            "transport_mode": cfg["transport_mode"],
            "project_id": cfg["gcp_project_id"],
            "subscription_id": cfg["pubsub_subscription_id"],
            "allowed_user_emails": cfg["allowed_user_emails"],
            "service_account_present": self.sa_key_path.exists(),
            "last_error": self._last_error,
        }

    def get_status(self) -> dict[str, Any]:
        return self.status_info

    def start_or_reload(self) -> _DualSyncAsyncResult:
        """Starts or hot-reloads the Cloud Pub/Sub streaming pull subscriber."""
        with self._lock:
            self.stop()
            cfg = self._read_gchat_config()
            if not cfg["enabled"] or cfg["transport_mode"] != "pubsub":
                self._status = "disabled"
                return _DualSyncAsyncResult(self.status_info)

            project_id = cfg["gcp_project_id"]
            sub_id = cfg["pubsub_subscription_id"]
            if not project_id or not sub_id or not self.sa_key_path.exists():
                self._status = "unconfigured"
                return _DualSyncAsyncResult(self.status_info)

            if pubsub_v1 is None:
                self._status = "error"
                self._last_error = "google-cloud-pubsub is not installed"
                return _DualSyncAsyncResult(self.status_info)

            try:
                if self._loop is None:
                    try:
                        self._loop = asyncio.get_running_loop()
                    except RuntimeError:
                        pass

                chat_creds = service_account.Credentials.from_service_account_file(
                    str(self.sa_key_path), scopes=CHAT_BOT_SCOPES
                )
                self._chat_service = build(
                    "chat", "v1", credentials=chat_creds, cache_discovery=False
                )

                pubsub_creds = service_account.Credentials.from_service_account_file(
                    str(self.sa_key_path)
                )
                self._subscriber = pubsub_v1.SubscriberClient(credentials=pubsub_creds)
                subscription_path = self._subscriber.subscription_path(project_id, sub_id)

                self._streaming_pull_future = self._subscriber.subscribe(
                    subscription_path, callback=self._on_pubsub_message
                )
                self._status = "connected"
                self._last_error = None
                logger.info("Connected Google Chat Pub/Sub subscriber to %s", subscription_path)
            except Exception as exc:
                self._status = "error"
                self._last_error = str(exc)
                logger.exception("Failed to start Google Chat Pub/Sub bridge: %s", exc)

            return _DualSyncAsyncResult(self.status_info)

    start = start_or_reload
    restart = start_or_reload

    def stop(self) -> _DualSyncAsyncResult:
        with self._lock:
            if self._streaming_pull_future is not None:
                try:
                    self._streaming_pull_future.cancel()
                except Exception:
                    pass
                self._streaming_pull_future = None
            if self._subscriber is not None:
                try:
                    self._subscriber.close()
                except Exception:
                    pass
                self._subscriber = None
            self._status = "disconnected"
            return _DualSyncAsyncResult(self.status_info)

    def _on_pubsub_message(self, message: Any) -> None:
        """Synchronous callback invoked by google-cloud-pubsub worker thread."""
        try:
            raw_bytes = (
                message.data
                if isinstance(message.data, bytes)
                else str(message.data).encode("utf-8")
            )
            event = json.loads(raw_bytes.decode("utf-8"))
        except Exception as exc:
            logger.error("Invalid JSON in Pub/Sub message: %s", exc)
            message.ack()
            return

        # Immediately ACK so Pub/Sub never redelivers during multi-minute Deep Research runs
        message.ack()

        if self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self.handle_chat_event(event), self._loop)
        else:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.handle_chat_event(event))
            except RuntimeError:
                asyncio.run(self.handle_chat_event(event))

    async def handle_chat_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Processes an inbound Google Chat event (from Pub/Sub or Webhook)."""
        event_type = event.get("type")
        if not event_type and "chat" in event:
            chat_payload = event["chat"]
            sender = chat_payload.get("user", {})
            msg_payload = chat_payload.get("messagePayload", {}).get("message", {})
            space = msg_payload.get("space", {}) or chat_payload.get("space", {})
            event_type = "MESSAGE" if msg_payload else "ADDED_TO_SPACE"
        else:
            msg_payload = event.get("message", {}) or {}
            sender = event.get("user") or msg_payload.get("sender") or {}
            space = event.get("space") or msg_payload.get("space") or {}

        sender_email = (sender.get("email") or "").lower().strip()
        space_name = space.get("name", "")
        thread_name = (msg_payload.get("thread") or {}).get("name")

        # Strict security gate: only allow the Workspace owner's allowlisted email(s)
        if not self.is_user_allowed(sender_email):
            logger.warning("Blocked unauthorized Google Chat user: %s", sender_email)
            if space_name and self._chat_service is not None:
                await asyncio.to_thread(
                    self._send_text_message,
                    space_name,
                    f"⛔ Unauthorized user (`{sender_email}`). Only allowlisted domain owners may use this agent.",
                    thread_name,
                )
            return {
                "status": "unauthorized",
                "allowed": False,
                "sender_email": sender_email,
            }

        if event_type == "ADDED_TO_SPACE":
            if space_name and self._chat_service is not None:
                await asyncio.to_thread(
                    self._send_text_message,
                    space_name,
                    "👋 Connected to your local Linux ADK Agent! Send any message, `/research <topic>`, or ask me to check Gmail, Calendar, Drive, or browse with Headless Chrome.",
                    None,
                )
            return {"status": "added_to_space", "allowed": True, "sender_email": sender_email}

        if event_type != "MESSAGE":
            return {"status": "ignored", "allowed": True, "event_type": event_type}

        user_text = (msg_payload.get("argumentText") or msg_payload.get("text") or "").strip()
        if not user_text:
            return {"status": "empty_message", "allowed": True}

        deep_research = False
        if user_text.lower().startswith("/research "):
            deep_research = True
            user_text = user_text[len("/research ") :].strip()

        session_id = f"gchat_{(space_name or 'default').replace('/', '_')}"

        placeholder_name: Optional[str] = None
        if space_name and self._chat_service is not None:
            placeholder = await asyncio.to_thread(
                self._send_text_message,
                space_name,
                "⏳ *Thinking...*",
                thread_name,
            )
            placeholder_name = (placeholder or {}).get("name")

        try:
            final_reply = await self._execute_agent_turn(
                sender_email=sender_email,
                session_id=session_id,
                user_text=user_text,
                deep_research=deep_research,
                placeholder_name=placeholder_name,
            )

            if placeholder_name and self._chat_service is not None:
                await asyncio.to_thread(
                    self._patch_text_message, placeholder_name, final_reply[:4000]
                )
            elif space_name and self._chat_service is not None:
                await asyncio.to_thread(
                    self._send_text_message, space_name, final_reply[:4000], thread_name
                )

            if len(final_reply) > 4000 and space_name and self._chat_service is not None:
                for idx in range(4000, len(final_reply), 4000):
                    await asyncio.to_thread(
                        self._send_text_message,
                        space_name,
                        final_reply[idx : idx + 4000],
                        thread_name,
                    )

            return {
                "status": "ok",
                "allowed": True,
                "session_id": session_id,
                "reply": final_reply,
            }
        except Exception as exc:
            logger.exception("Error running ADK agent for Google Chat message: %s", exc)
            err_msg = f"❌ *Agent execution error:* `{exc}`"
            if placeholder_name and self._chat_service is not None:
                await asyncio.to_thread(self._patch_text_message, placeholder_name, err_msg)
            return {
                "status": "error",
                "allowed": True,
                "session_id": session_id,
                "error": str(exc),
            }

    async def _execute_agent_turn(
        self,
        *,
        sender_email: str,
        session_id: str,
        user_text: str,
        deep_research: bool,
        placeholder_name: Optional[str],
    ) -> str:
        target = self._resolve_runner_or_manager()
        tool_trace: list[str] = []
        final_text_parts: list[str] = []
        grounding_sources: list[dict[str, str]] = []

        if hasattr(target, "stream_chat_turn"):
            async for event_dict in target.stream_chat_turn(
                session_id=session_id,
                user_message=user_text,
                user_id=sender_email,
                deep_research=deep_research,
            ):
                if event_dict.get("type") == "error":
                    return f"⚠️ {event_dict.get('error') or event_dict.get('message')}"

                for tc in event_dict.get("tool_calls") or []:
                    tc_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
                    if tc_name:
                        label = f"🔧 `{tc_name}`"
                        if label not in tool_trace:
                            tool_trace.append(label)
                            if placeholder_name and self._chat_service is not None:
                                status_md = "⏳ *Working...*\n" + " → ".join(tool_trace[-4:])
                                await asyncio.to_thread(
                                    self._patch_text_message, placeholder_name, status_md
                                )

                if event_dict.get("text") and not event_dict.get("partial", False):
                    final_text_parts.append(str(event_dict["text"]))

                for src in event_dict.get("grounding_sources") or []:
                    if isinstance(src, dict) and src.get("uri") and src not in grounding_sources:
                        grounding_sources.append(src)

        elif hasattr(target, "run_turn_text"):
            res = target.run_turn_text(
                user_id=sender_email,
                session_id=session_id,
                user_message=user_text,
            )
            text_out = await res if inspect.isawaitable(res) else res
            final_text_parts.append(str(text_out))

        else:
            await self._ensure_session(target, sender_email, session_id)
            content = (
                genai_types.Content(
                    role="user",
                    parts=[genai_types.Part.from_text(text=user_text)],
                )
                if genai_types is not None
                else {"role": "user", "parts": [{"text": user_text}]}
            )
            async for adk_event in target.run_async(
                user_id=sender_email,
                session_id=session_id,
                new_message=content,
            ):
                fn_calls = (
                    adk_event.get_function_calls()
                    if hasattr(adk_event, "get_function_calls")
                    else []
                )
                for fn_call in fn_calls or []:
                    step_label = f"🔧 `{fn_call.name}`"
                    if step_label not in tool_trace:
                        tool_trace.append(step_label)
                        if placeholder_name and self._chat_service is not None:
                            status_md = "⏳ *Working...*\n" + " → ".join(tool_trace[-4:])
                            await asyncio.to_thread(
                                self._patch_text_message, placeholder_name, status_md
                            )

                is_final = (
                    adk_event.is_final_response()
                    if hasattr(adk_event, "is_final_response")
                    else True
                )
                if is_final and getattr(adk_event, "content", None) and adk_event.content.parts:
                    for part in adk_event.content.parts:
                        if getattr(part, "text", None):
                            final_text_parts.append(part.text)

        final_reply = "\n\n".join(final_text_parts).strip() or "✅ Completed (no text output)."
        if tool_trace:
            final_reply = f"*Tools used:* {' · '.join(tool_trace)}\n\n{final_reply}"
        if grounding_sources:
            cites = "\n".join(
                f"• <{s['uri']}|{s.get('title') or s['uri']}>" for s in grounding_sources[:6]
            )
            final_reply = f"{final_reply}\n\n*Grounded Sources:*\n{cites}"
        return final_reply

    async def _ensure_session(self, runner: Any, user_id: str, session_id: str) -> None:
        session_svc = getattr(runner, "session_service", None)
        if session_svc is None:
            return
        app_name = getattr(runner, "app_name", "local_adk_agent")
        session = await session_svc.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id
        )
        if session is None:
            await session_svc.create_session(
                app_name=app_name,
                user_id=user_id,
                session_id=session_id,
                state={"origin_channel": "gchat", "source": "google_chat", "user_email": user_id},
            )

    def _send_text_message(
        self, space_name: str, text: str, thread_name: Optional[str] = None
    ) -> dict[str, Any]:
        if self._chat_service is None:
            return {}
        body: dict[str, Any] = {"text": text}
        kwargs: dict[str, Any] = {"parent": space_name, "body": body}
        if thread_name:
            body["thread"] = {"name": thread_name}
            kwargs["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
        return self._chat_service.spaces().messages().create(**kwargs).execute()

    def _patch_text_message(self, message_name: str, text: str) -> dict[str, Any]:
        if self._chat_service is None:
            return {}
        return (
            self._chat_service.spaces()
            .messages()
            .patch(name=message_name, updateMask="text", body={"text": text})
            .execute()
        )


_gchat_bridge_singleton: Optional[GChatPubSubBridge] = None
_gchat_bridge_lock = threading.Lock()


def get_gchat_bridge() -> GChatPubSubBridge:
    """Returns the process-wide GChatPubSubBridge singleton."""
    global _gchat_bridge_singleton
    with _gchat_bridge_lock:
        if _gchat_bridge_singleton is None:
            _gchat_bridge_singleton = GChatPubSubBridge()
        return _gchat_bridge_singleton
