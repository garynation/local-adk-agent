"""Persistent configuration store (/data/config.json) with Fernet encryption and hot-reload listeners."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import logging
import os
from pathlib import Path
import secrets
import tempfile
import threading
from typing import Any, Awaitable, Callable, Optional, Union

from cryptography.fernet import Fernet, InvalidToken

try:
    from google import genai
except ImportError:  # pragma: no cover - fallback when google-genai is not installed on host

    class _FallbackGenaiModule:
        class Client:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs
                self.aio = SimpleAio()

    class SimpleAio:
        class models:
            @staticmethod
            async def list(**kwargs: Any) -> Any:
                if False:
                    yield None

        class interactions:
            @staticmethod
            async def create(**kwargs: Any) -> Any:
                raise RuntimeError("google-genai Interactions API requires live connection")

            @staticmethod
            async def get(interaction_id: str) -> Any:
                raise RuntimeError("google-genai Interactions API requires live connection")

    genai = _FallbackGenaiModule()  # type: ignore[assignment]

try:
    from pydantic import BaseModel, Field, model_validator
    _HAS_PYDANTIC = True
except ImportError:  # pragma: no cover - stdlib fallback when pydantic is not installed on host
    _HAS_PYDANTIC = False

    class _FieldSpec:
        def __init__(
            self,
            default: Any = None,
            default_factory: Optional[Callable[[], Any]] = None,
            description: str = "",
            exclude: bool = False,
        ) -> None:
            self.default = default
            self.default_factory = default_factory
            self.description = description
            self.exclude = exclude

    def Field(  # type: ignore[misc]
        default: Any = None,
        *,
        default_factory: Optional[Callable[[], Any]] = None,
        description: str = "",
        exclude: bool = False,
    ) -> Any:
        return _FieldSpec(
            default=default,
            default_factory=default_factory,
            description=description,
            exclude=exclude,
        )

    def model_validator(*args: Any, **kwargs: Any) -> Callable[[Any], Any]:  # type: ignore[misc]
        def _decorator(fn: Any) -> Any:
            return fn

        return _decorator

    class BaseModel:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            if hasattr(self, "_synchronize_aliases"):
                kwargs = self.__class__._synchronize_aliases(kwargs)
            excluded: set[str] = set()
            for cls in reversed(self.__class__.__mro__):
                for k, v in cls.__dict__.items():
                    if k.startswith("_") or callable(v) or isinstance(v, (classmethod, staticmethod, property)):
                        continue
                    if isinstance(v, _FieldSpec):
                        if v.exclude:
                            excluded.add(k)
                        val = (
                            kwargs[k]
                            if k in kwargs
                            else (
                                v.default_factory()
                                if v.default_factory is not None
                                else copy.deepcopy(v.default)
                            )
                        )
                        setattr(self, k, val)
                    else:
                        val = kwargs[k] if k in kwargs else copy.deepcopy(v)
                        setattr(self, k, val)
            for k, v in kwargs.items():
                setattr(self, k, v)
            self._excluded_fields = excluded

        def model_dump(self, exclude: Optional[set[str]] = None) -> dict[str, Any]:
            ex = set(getattr(self, "_excluded_fields", set())) | set(exclude or set())
            out: dict[str, Any] = {}
            for k, v in self.__dict__.items():
                if k.startswith("_") or k in ex:
                    continue
                if isinstance(v, BaseModel):
                    out[k] = v.model_dump()
                elif isinstance(v, list):
                    out[k] = [
                        item.model_dump() if isinstance(item, BaseModel) else copy.deepcopy(item)
                        for item in v
                    ]
                else:
                    out[k] = copy.deepcopy(v)
            return out

        def model_dump_json(self, indent: Optional[int] = None) -> str:
            return json.dumps(self.model_dump(), indent=indent)

        def model_copy(self, deep: bool = False) -> Any:
            return copy.deepcopy(self) if deep else copy.copy(self)

        @classmethod
        def model_validate(cls, data: Any) -> Any:
            if isinstance(data, cls):
                return data.model_copy(deep=True)
            if isinstance(data, dict):
                return cls(**data)
            raise ValueError(f"Cannot validate {type(data)} as {cls.__name__}")

logger = logging.getLogger(__name__)


def _resolve_data_dir() -> Path:
    """Resolves the persistent credential & state directory outside the application source tree.

    Priority:
    1. Explicit `DATA_DIR` or `ADK_DATA_DIR` environment variable (supports `~` expansion).
    2. Container `/data` mount when running inside Docker (`/.dockerenv` or writable `/data`).
    3. Host XDG config path (`~/.config/local-adk-agent/data`) when running directly on Linux host,
       ensuring `git pull`, `git clean`, or app directory updates never overwrite credentials.
    """
    explicit = os.environ.get("DATA_DIR") or os.environ.get("ADK_DATA_DIR")
    if explicit and explicit.strip():
        return Path(explicit.strip()).expanduser()
    container_data = Path("/data")
    if Path("/.dockerenv").exists() or (container_data.exists() and os.access(container_data, os.W_OK)):
        return container_data
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    base_config = Path(xdg_config).expanduser() if xdg_config else (Path.home() / ".config")
    return base_config / "local-adk-agent" / "data"


DATA_DIR = _resolve_data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
SECRET_KEY_PATH = DATA_DIR / ".secret.key"
SESSIONS_DB_PATH = DATA_DIR / "sessions.db"
CLIENT_SECRET_PATH = DATA_DIR / "client_secret.json"
WORKSPACE_CLIENT_SECRET_PATH = DATA_DIR / "workspace_client_secret.json"
WORKSPACE_TOKEN_PATH = DATA_DIR / "workspace_token.json"
GCHAT_SA_PATH = DATA_DIR / "gchat_service_account.json"
CHROME_PROFILE_DIR = DATA_DIR / "chrome-profile"
SCREENSHOTS_DIR = DATA_DIR / "screenshots"

DEFAULT_WORKSPACE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]


class RuntimeConfig(BaseModel):
    """Canonical schema for /data/config.json shared across all modules."""

    # 1. Gemini / AI Studio settings
    gemini_api_key_encrypted: Optional[str] = None
    gemini_api_key: str = Field(default="", exclude=True)
    google_genai_use_vertexai: bool = False
    default_model: str = "gemini-4.0-pro"
    fast_model: str = "gemini-4.0-flash"
    temperature: float = 0.2
    deep_research_engine: str = Field(
        default="hybrid",
        description="'adk_workflow' | 'interactions_api' | 'hybrid'",
    )
    deep_research_interactions_agent: str = "deep-research-preview-04-2026"
    max_research_iterations: int = 3

    # 2. Personal Google Workspace settings
    owner_email: str = "local_owner"
    workspace_connected_email: str = ""
    workspace_enabled: bool = False
    email_safety_mode: str = Field(
        default="draft_only",
        description="'draft_only' | 'allow_send'",
    )
    gmail_safety_mode: str = Field(
        default="draft_only",
        description="Alias synchronized with email_safety_mode ('draft_only' | 'allow_send').",
    )
    workspace_draft_only_mode: bool = True
    workspace_scopes: list[str] = Field(
        default_factory=lambda: list(DEFAULT_WORKSPACE_SCOPES)
    )

    # 3. Google Chat Integration settings
    gchat_enabled: bool = False
    gchat_mode: str = Field(
        default="pubsub",
        description="'pubsub' | 'webhook'",
    )
    gchat_transport_mode: str = "pubsub"
    gcp_project_id: Optional[str] = None
    gchat_project_id: str = ""
    gcp_project_number: str = ""
    webhook_audience: str = ""
    pubsub_subscription_id: str = "adk-chat-inbound-sub"
    gchat_subscription_id: str = "adk-chat-inbound-sub"
    allowed_user_emails: list[str] = Field(default_factory=list)
    gchat_allowed_emails: list[str] = Field(default_factory=list)

    # 4. Headless Chrome / Playwright settings
    browser_headless: bool = True
    browser_capture_screenshots: bool = True

    # 5. Local Web UI Security
    webui_auth_enabled: bool = False
    webui_pin_salt: str = ""
    webui_pin_hash: Optional[str] = None
    webui_admin_pin_hash: Optional[str] = None
    webui_session_secret: str = Field(
        default_factory=lambda: secrets.token_hex(32)
    )
    webui_allowed_cidrs: list[str] = Field(
        default_factory=lambda: [
            "127.0.0.0/8",
            "::1/128",
            "100.64.0.0/10",
            "192.168.0.0/16",
            "10.0.0.0/8",
            "172.16.0.0/12",
        ]
    )

    @model_validator(mode="before")
    @classmethod
    def _synchronize_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)

        # Synchronize email_safety_mode / gmail_safety_mode / workspace_draft_only_mode
        email_mode = out.get("email_safety_mode")
        gmail_mode = out.get("gmail_safety_mode")
        draft_bool = out.get("workspace_draft_only_mode")

        resolved_mode = "draft_only"
        if email_mode in ("draft_only", "allow_send") and email_mode != "draft_only":
            resolved_mode = email_mode
        elif gmail_mode in ("draft_only", "allow_send") and gmail_mode != "draft_only":
            resolved_mode = gmail_mode
        elif email_mode in ("draft_only", "allow_send"):
            resolved_mode = email_mode
        elif gmail_mode in ("draft_only", "allow_send"):
            resolved_mode = gmail_mode
        elif draft_bool is False:
            resolved_mode = "allow_send"

        if (
            "workspace_draft_only_mode" in out
            and "email_safety_mode" not in out
            and "gmail_safety_mode" not in out
        ):
            resolved_mode = "draft_only" if out["workspace_draft_only_mode"] else "allow_send"

        out["email_safety_mode"] = resolved_mode
        out["gmail_safety_mode"] = resolved_mode
        out["workspace_draft_only_mode"] = resolved_mode == "draft_only"

        # Synchronize allowed_user_emails / gchat_allowed_emails
        allowed_a = out.get("allowed_user_emails")
        allowed_b = out.get("gchat_allowed_emails")
        chosen_emails: list[str] = []
        if isinstance(allowed_a, list) and allowed_a:
            chosen_emails = [str(e).strip().lower() for e in allowed_a if str(e).strip()]
        elif isinstance(allowed_b, list) and allowed_b:
            chosen_emails = [str(e).strip().lower() for e in allowed_b if str(e).strip()]
        elif isinstance(allowed_a, list):
            chosen_emails = [str(e).strip().lower() for e in allowed_a if str(e).strip()]
        elif isinstance(allowed_b, list):
            chosen_emails = [str(e).strip().lower() for e in allowed_b if str(e).strip()]
        out["allowed_user_emails"] = chosen_emails
        out["gchat_allowed_emails"] = list(chosen_emails)

        # Synchronize gcp_project_id / gchat_project_id
        proj_a = out.get("gcp_project_id")
        proj_b = out.get("gchat_project_id")
        resolved_proj = (proj_a or proj_b or "").strip()
        out["gcp_project_id"] = resolved_proj or None
        out["gchat_project_id"] = resolved_proj

        # Synchronize pubsub_subscription_id / gchat_subscription_id
        sub_a = out.get("pubsub_subscription_id")
        sub_b = out.get("gchat_subscription_id")
        resolved_sub = "adk-chat-inbound-sub"
        if sub_a and sub_a != "adk-chat-inbound-sub":
            resolved_sub = str(sub_a).strip()
        elif sub_b and sub_b != "adk-chat-inbound-sub":
            resolved_sub = str(sub_b).strip()
        elif sub_a:
            resolved_sub = str(sub_a).strip()
        elif sub_b:
            resolved_sub = str(sub_b).strip()
        out["pubsub_subscription_id"] = resolved_sub
        out["gchat_subscription_id"] = resolved_sub

        # Synchronize gchat_mode / gchat_transport_mode
        mode_a = out.get("gchat_mode")
        mode_b = out.get("gchat_transport_mode")
        resolved_gchat_mode = "pubsub"
        if mode_a in ("pubsub", "webhook") and mode_a != "pubsub":
            resolved_gchat_mode = mode_a
        elif mode_b in ("pubsub", "webhook") and mode_b != "pubsub":
            resolved_gchat_mode = mode_b
        elif mode_a in ("pubsub", "webhook"):
            resolved_gchat_mode = mode_a
        elif mode_b in ("pubsub", "webhook"):
            resolved_gchat_mode = mode_b
        out["gchat_mode"] = resolved_gchat_mode
        out["gchat_transport_mode"] = resolved_gchat_mode

        # Synchronize owner_email / workspace_connected_email
        ws_email = (out.get("workspace_connected_email") or "").strip()
        owner = (out.get("owner_email") or "local_owner").strip()
        if ws_email and owner == "local_owner":
            owner = ws_email
        elif owner != "local_owner" and not ws_email:
            ws_email = owner
        out["owner_email"] = owner
        out["workspace_connected_email"] = ws_email

        # Synchronize webui_pin_hash / webui_admin_pin_hash
        pin_h = out.get("webui_pin_hash") or out.get("webui_admin_pin_hash")
        out["webui_pin_hash"] = pin_h
        out["webui_admin_pin_hash"] = pin_h

        return out

    def get(self, key: str, default: Any = None) -> Any:
        """Provides dictionary-compatible .get() access including nested section views."""
        if key == "workspace":
            return {
                "enabled": self.workspace_enabled,
                "connected_email": self.workspace_connected_email or self.owner_email,
                "email_safety_mode": self.email_safety_mode,
                "gmail_safety_mode": self.gmail_safety_mode,
                "draft_only_mode": self.workspace_draft_only_mode,
                "scopes": list(self.workspace_scopes),
            }
        if key == "gchat":
            return {
                "enabled": self.gchat_enabled,
                "transport_mode": self.gchat_transport_mode,
                "mode": self.gchat_mode,
                "gcp_project_id": self.gcp_project_id or self.gchat_project_id,
                "project_id": self.gchat_project_id or (self.gcp_project_id or ""),
                "pubsub_subscription_id": self.pubsub_subscription_id,
                "subscription_id": self.gchat_subscription_id,
                "allowed_user_emails": list(self.allowed_user_emails),
                "allowed_emails": list(self.gchat_allowed_emails),
                "gcp_project_number": self.gcp_project_number,
                "webhook_audience": self.webhook_audience,
            }
        if hasattr(self, key):
            return getattr(self, key)
        return default

    def __getitem__(self, key: str) -> Any:
        val = self.get(key, ...)
        if val is ...:
            raise KeyError(key)
        return val


AppConfig = RuntimeConfig

ListenerType = Callable[[RuntimeConfig], Union[Awaitable[None], None]]


class ConfigManager:
    """Thread- and async-safe manager for /data/config.json with Fernet encryption and live listeners."""

    _singleton: Optional[ConfigManager] = None
    _singleton_lock = threading.RLock()

    def __init__(self, config_path: Optional[Path] = None) -> None:
        base_dir = _resolve_data_dir()
        self._config_path = (
            Path(config_path).expanduser()
            if config_path is not None
            else (base_dir / "config.json")
        )
        self._backup_config_path = self._config_path.with_name(f"{self._config_path.name}.bak")
        self._secret_key_path = self._config_path.parent / ".secret.key"
        self._lock = asyncio.Lock()
        self._thread_lock = threading.RLock()
        self._listeners: list[ListenerType] = []
        self._extra_fields: dict[str, Any] = {}
        self._ensure_directories()
        self._fernet = self._init_fernet()
        self._config = self._load_from_disk()
        self._sync_in_memory_key()
        self._export_env_vars()

    @classmethod
    def instance(cls) -> ConfigManager:
        return get_config_manager()

    def _ensure_directories(self) -> None:
        data_root = self._config_path.parent
        data_root.mkdir(parents=True, exist_ok=True)
        (data_root / "chrome-profile").mkdir(parents=True, exist_ok=True)
        (data_root / "screenshots").mkdir(parents=True, exist_ok=True)

    def _init_fernet(self) -> Fernet:
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        env_key = os.environ.get("CONFIG_ENCRYPTION_KEY")
        if env_key:
            return Fernet(env_key.strip().encode("utf-8"))
        if not self._secret_key_path.exists():
            key = Fernet.generate_key()
            self._secret_key_path.write_bytes(key)
            os.chmod(self._secret_key_path, 0o600)
        else:
            try:
                os.chmod(self._secret_key_path, 0o600)
            except OSError:
                pass
        return Fernet(self._secret_key_path.read_bytes().strip())

    def encrypt_secret(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.strip().encode("utf-8")).decode("utf-8")

    def decrypt_secret(self, token: Optional[str]) -> Optional[str]:
        if not token:
            return None
        try:
            return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")
        except InvalidToken:
            if token.startswith("AIza"):
                return token
            logger.error("Failed to decrypt secret in %s", self._config_path)
            return None

    def get_gemini_api_key(self) -> Optional[str]:
        decrypted = self.decrypt_secret(self._config.gemini_api_key_encrypted)
        if decrypted:
            return decrypted
        env_key = os.environ.get("GEMINI_API_KEY")
        return env_key.strip() if env_key and env_key.strip() else None

    def get_masked_gemini_api_key(self) -> Optional[str]:
        key = self.get_gemini_api_key()
        if not key:
            return None
        if len(key) < 8:
            return "••••"
        return f"{key[:4]}{'*' * (len(key) - 8)}{key[-4:]}"

    def _sync_in_memory_key(self) -> None:
        decrypted = self.get_gemini_api_key() or ""
        self._config.gemini_api_key = decrypted

    def _read_raw_json_with_backup(self) -> Optional[dict[str, Any]]:
        """Reads raw JSON from config.json, falling back to config.json.bak if needed."""
        for candidate in (self._config_path, self._backup_config_path):
            if not candidate.exists():
                continue
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception as exc:
                logger.warning("Could not parse %s: %s", candidate, exc)
        return None

    def _load_from_disk(self) -> RuntimeConfig:
        raw = self._read_raw_json_with_backup()
        if raw is None:
            cfg = RuntimeConfig()
            env_key = os.environ.get("GEMINI_API_KEY", "").strip()
            if env_key:
                cfg.gemini_api_key_encrypted = self.encrypt_secret(env_key)
                cfg.gemini_api_key = env_key
            return cfg

        migrated_plaintext = False
        if "gemini_api_key" in raw and raw["gemini_api_key"]:
            raw["gemini_api_key_encrypted"] = self.encrypt_secret(
                str(raw.pop("gemini_api_key"))
            )
            migrated_plaintext = True

        known_fields = set(RuntimeConfig().model_dump().keys()) | {"gemini_api_key"}
        self._extra_fields = {
            k: copy.deepcopy(v) for k, v in raw.items() if k not in known_fields
        }

        try:
            cfg = RuntimeConfig.model_validate(raw)
        except Exception as exc:
            logger.warning(
                "Schema validation warning on %s (%s); salvaging all valid fields and credentials.",
                self._config_path,
                exc,
            )
            salvaged = RuntimeConfig().model_dump()
            for k, v in raw.items():
                if k in salvaged and v is not None:
                    salvaged[k] = v
            cfg = RuntimeConfig.model_validate(salvaged)

        if migrated_plaintext:
            self._config = cfg
            self._write_to_disk()
        return cfg

    def _write_to_disk(self) -> None:
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        existing_raw = self._read_raw_json_with_backup() or {}

        # Never allow an accidental empty gemini_api_key_encrypted to overwrite an existing saved key on disk
        if (
            not self._config.gemini_api_key_encrypted
            and existing_raw.get("gemini_api_key_encrypted")
        ):
            self._config.gemini_api_key_encrypted = existing_raw["gemini_api_key_encrypted"]

        # Preserve any unknown/future schema keys from disk across app updates
        dump_dict = dict(self._extra_fields)
        for k, v in existing_raw.items():
            if k not in self._config.model_dump() and k != "gemini_api_key":
                dump_dict.setdefault(k, v)
        dump_dict.update(self._config.model_dump())

        payload = json.dumps(dump_dict, indent=2)

        # Save rolling 0600 backup of the previous valid config.json before replacing
        if self._config_path.exists():
            try:
                self._backup_config_path.write_text(
                    self._config_path.read_text(encoding="utf-8"), encoding="utf-8"
                )
                os.chmod(self._backup_config_path, 0o600)
            except OSError:
                pass

        fd, tmp_path = tempfile.mkstemp(
            dir=str(self._config_path.parent), prefix=".config.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self._config_path)
            os.chmod(self._config_path, 0o600)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _export_env_vars(self) -> None:
        """Synchronizes process environment variables for google-adk and google-genai."""
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = (
            "TRUE" if self._config.google_genai_use_vertexai else "FALSE"
        )
        api_key = self.get_gemini_api_key()
        if api_key:
            os.environ["GEMINI_API_KEY"] = api_key
            os.environ["GOOGLE_API_KEY"] = api_key

    @property
    def config(self) -> RuntimeConfig:
        with self._thread_lock:
            self._sync_in_memory_key()
            copy_cfg = self._config.model_copy(deep=True)
            copy_cfg.gemini_api_key = self._config.gemini_api_key
            return copy_cfg

    def get(self) -> RuntimeConfig:
        return self.config

    def get_config(self) -> RuntimeConfig:
        return self.config

    def register_listener(self, listener: ListenerType) -> None:
        self._listeners.append(listener)

    async def _notify_listeners(self, snapshot: RuntimeConfig) -> None:
        for listener in list(self._listeners):
            res = listener(snapshot)
            if inspect.isawaitable(res):
                await res

    async def validate_gemini_api_key(self, candidate_key: str) -> dict[str, Any]:
        """Verifies a candidate AI Studio API key against Google GenAI API before saving."""
        cleaned = candidate_key.strip()
        if not cleaned:
            return {"valid": False, "error": "API key cannot be empty."}
        try:
            client = genai.Client(api_key=cleaned, vertexai=False)
            available_models: list[str] = []
            pager = await client.aio.models.list(config={"page_size": 25})
            async for m in pager:
                if m and getattr(m, "name", None):
                    available_models.append(m.name.removeprefix("models/"))
            return {
                "valid": True,
                "models_sample": available_models[:10],
                "models_count": len(available_models),
            }
        except Exception as exc:
            return {"valid": False, "error": str(exc)}

    async def update_gemini_api_key(
        self,
        api_key: str,
        default_model: Optional[str] = None,
        fast_model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_research_iterations: Optional[int] = None,
    ) -> RuntimeConfig:
        """Encrypts, persists, exports env vars, and triggers ADK Runner hot-reload."""
        async with self._lock:
            with self._thread_lock:
                cleaned = api_key.strip()
                self._config.gemini_api_key_encrypted = self.encrypt_secret(cleaned)
                self._config.gemini_api_key = cleaned
                self._config.google_genai_use_vertexai = False
                if default_model:
                    self._config.default_model = default_model
                if fast_model:
                    self._config.fast_model = fast_model
                if temperature is not None:
                    self._config.temperature = float(temperature)
                if max_research_iterations is not None:
                    self._config.max_research_iterations = int(max_research_iterations)
                self._write_to_disk()
                self._export_env_vars()
                snapshot = self.config

        await self._notify_listeners(snapshot)
        return snapshot

    async def update_partial(self, **updates: Any) -> RuntimeConfig:
        """Updates arbitrary config fields and triggers ADK Runner hot-reload."""
        async with self._lock:
            snapshot = self._apply_updates_sync(**updates)
        await self._notify_listeners(snapshot)
        return snapshot

    def _apply_updates_sync(self, **updates: Any) -> RuntimeConfig:
        with self._thread_lock:
            merged = self._config.model_dump()
            if "gemini_api_key" in updates:
                raw_key = str(updates.pop("gemini_api_key") or "").strip()
                if raw_key:
                    merged["gemini_api_key_encrypted"] = self.encrypt_secret(raw_key)
            if "email_safety_mode" in updates and "gmail_safety_mode" not in updates:
                merged.pop("gmail_safety_mode", None)
                merged.pop("workspace_draft_only_mode", None)
            elif "gmail_safety_mode" in updates and "email_safety_mode" not in updates:
                merged.pop("email_safety_mode", None)
                merged.pop("workspace_draft_only_mode", None)
            elif (
                "workspace_draft_only_mode" in updates
                and "email_safety_mode" not in updates
                and "gmail_safety_mode" not in updates
            ):
                merged.pop("email_safety_mode", None)
                merged.pop("gmail_safety_mode", None)

            if "allowed_user_emails" in updates and "gchat_allowed_emails" not in updates:
                merged.pop("gchat_allowed_emails", None)
            elif "gchat_allowed_emails" in updates and "allowed_user_emails" not in updates:
                merged.pop("allowed_user_emails", None)

            if "gcp_project_id" in updates and "gchat_project_id" not in updates:
                merged.pop("gchat_project_id", None)
            elif "gchat_project_id" in updates and "gcp_project_id" not in updates:
                merged.pop("gcp_project_id", None)

            if "pubsub_subscription_id" in updates and "gchat_subscription_id" not in updates:
                merged.pop("gchat_subscription_id", None)
            elif "gchat_subscription_id" in updates and "pubsub_subscription_id" not in updates:
                merged.pop("pubsub_subscription_id", None)

            if "gchat_mode" in updates and "gchat_transport_mode" not in updates:
                merged.pop("gchat_transport_mode", None)
            elif "gchat_transport_mode" in updates and "gchat_mode" not in updates:
                merged.pop("gchat_mode", None)

            merged.update(updates)
            self._config = RuntimeConfig.model_validate(merged)
            self._sync_in_memory_key()
            self._write_to_disk()
            self._export_env_vars()
            return self.config

    def update(self, **updates: Any) -> RuntimeConfig:
        """Synchronous update method that also schedules async listeners when an event loop is running."""
        snapshot = self._apply_updates_sync(**updates)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._notify_listeners(snapshot))
        except RuntimeError:
            for listener in list(self._listeners):
                res = listener(snapshot)
                if inspect.isawaitable(res):
                    try:
                        asyncio.run(res)
                    except Exception:
                        pass
        return snapshot

    def update_workspace_connected_email(self, email: str) -> RuntimeConfig:
        clean_email = email.strip()
        allowed = list(self._config.allowed_user_emails)
        if clean_email and "@" in clean_email and clean_email.lower() not in allowed:
            allowed.append(clean_email.lower())
        return self.update(
            owner_email=clean_email or "local_owner",
            workspace_connected_email=clean_email,
            workspace_enabled=bool(clean_email),
            allowed_user_emails=allowed,
        )

    def set_admin_pin(self, pin: str) -> RuntimeConfig:
        clean_pin = pin.strip()
        if not clean_pin:
            return self.update(
                webui_auth_enabled=False,
                webui_pin_salt="",
                webui_pin_hash=None,
                webui_admin_pin_hash=None,
            )
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            clean_pin.encode("utf-8"),
            salt.encode("utf-8"),
            200_000,
        ).hex()
        return self.update(
            webui_auth_enabled=True,
            webui_pin_salt=salt,
            webui_pin_hash=digest,
            webui_admin_pin_hash=digest,
        )

    def verify_admin_pin(self, pin: str) -> bool:
        cfg = self.config
        target_hash = cfg.webui_pin_hash or cfg.webui_admin_pin_hash
        if not cfg.webui_auth_enabled or not target_hash:
            return True
        if not cfg.webui_pin_salt:
            return secrets.compare_digest(pin.strip(), target_hash)
        candidate = hashlib.pbkdf2_hmac(
            "sha256",
            pin.strip().encode("utf-8"),
            cfg.webui_pin_salt.encode("utf-8"),
            200_000,
        ).hex()
        return secrets.compare_digest(candidate, target_hash)

    def masked_dict(self) -> dict[str, Any]:
        """Returns safe configuration dictionary for the Web UI with masked API keys."""
        cfg = self.config
        data_root = self._config_path.parent
        client_secret_exists = (
            (data_root / "client_secret.json").exists()
            or (data_root / "workspace_client_secret.json").exists()
        )
        token_exists = (data_root / "workspace_token.json").exists()
        sa_exists = (data_root / "gchat_service_account.json").exists()
        masked_key = self.get_masked_gemini_api_key() or ""
        return {
            "gemini_key_configured": bool(self.get_gemini_api_key()),
            "gemini_key_masked": masked_key,
            "default_model": cfg.default_model,
            "fast_model": cfg.fast_model,
            "temperature": cfg.temperature,
            "max_research_iterations": cfg.max_research_iterations,
            "deep_research_engine": cfg.deep_research_engine,
            "owner_email": cfg.owner_email,
            "workspace_enabled": cfg.workspace_enabled or token_exists,
            "workspace_client_secret_present": client_secret_exists,
            "workspace_token_present": token_exists,
            "workspace_connected_email": cfg.workspace_connected_email,
            "workspace_scopes": list(cfg.workspace_scopes),
            "email_safety_mode": cfg.email_safety_mode,
            "gmail_safety_mode": cfg.gmail_safety_mode,
            "workspace_draft_only_mode": cfg.workspace_draft_only_mode,
            "gchat_enabled": cfg.gchat_enabled,
            "gchat_mode": cfg.gchat_mode,
            "gchat_transport_mode": cfg.gchat_transport_mode,
            "gchat_service_account_present": sa_exists,
            "gcp_project_id": cfg.gcp_project_id or cfg.gchat_project_id,
            "gchat_project_id": cfg.gchat_project_id or (cfg.gcp_project_id or ""),
            "pubsub_subscription_id": cfg.pubsub_subscription_id,
            "gchat_subscription_id": cfg.gchat_subscription_id,
            "allowed_user_emails": list(cfg.allowed_user_emails),
            "gchat_allowed_emails": list(cfg.gchat_allowed_emails),
            "browser_headless": cfg.browser_headless,
            "browser_capture_screenshots": cfg.browser_capture_screenshots,
            "webui_auth_enabled": cfg.webui_auth_enabled,
            "webui_allowed_cidrs": list(cfg.webui_allowed_cidrs),
        }


ConfigStore = ConfigManager


def get_config_manager(config_path: Optional[Path] = None) -> ConfigManager:
    """Returns the process-wide ConfigManager singleton, respecting DATA_DIR changes."""
    with ConfigManager._singleton_lock:
        expected_path = (
            Path(config_path)
            if config_path is not None
            else (_resolve_data_dir() / "config.json")
        )
        if (
            ConfigManager._singleton is None
            or ConfigManager._singleton._config_path != expected_path
        ):
            ConfigManager._singleton = ConfigManager(config_path=expected_path)
        return ConfigManager._singleton


def reset_config_manager() -> None:
    """Resets the singleton instance (primarily for unit tests)."""
    with ConfigManager._singleton_lock:
        ConfigManager._singleton = None
