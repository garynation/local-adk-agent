"""Personal Google Workspace OAuth 2.0 Manager and FastAPI Routes.

Manages `/data/client_secret.json` (`0600`) and `/data/workspace_token.json` (`0600`),
OAuth 2.0 authorization URL generation, callback code exchange, automatic token
refresh, and status reporting.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import secrets
import tempfile
import threading
from typing import Any, Callable, Iterator, Optional
import urllib.parse

try:
    from fastapi import APIRouter, HTTPException, Query, Request
    from fastapi.responses import RedirectResponse
except ImportError:  # pragma: no cover - host fallback when fastapi is not installed

    class HTTPException(Exception):  # type: ignore[no-redef]
        def __init__(self, status_code: int, detail: str = "") -> None:
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class RedirectResponse:  # type: ignore[no-redef]
        def __init__(self, url: str, status_code: int = 307) -> None:
            self.url = url
            self.status_code = status_code

    class Request:  # type: ignore[no-redef]
        pass

    def Query(default: Any = None, **kwargs: Any) -> Any:  # type: ignore[misc]
        return default

    class APIRouter:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            self.routes: list[Any] = []

        def get(self, path: str, **kwargs: Any) -> Callable[[Any], Any]:
            def _dec(fn: Any) -> Any:
                self.routes.append(("GET", path, fn))
                return fn

            return _dec

        def post(self, path: str, **kwargs: Any) -> Callable[[Any], Any]:
            def _dec(fn: Any) -> Any:
                self.routes.append(("POST", path, fn))
                return fn

            return _dec


try:
    from google.auth.transport.requests import Request as GoogleAuthRequest
except ImportError:  # pragma: no cover

    class GoogleAuthRequest:  # type: ignore[no-redef]
        pass


try:
    from google.oauth2.credentials import Credentials
except ImportError:  # pragma: no cover

    class Credentials:  # type: ignore[no-redef]
        def __init__(
            self,
            token: Optional[str] = None,
            refresh_token: Optional[str] = None,
            scopes: Optional[list[str]] = None,
            expiry: Any = None,
            **kwargs: Any,
        ) -> None:
            self.token = token
            self.refresh_token = refresh_token
            self.scopes = scopes or []
            self.expiry = expiry
            self.expired = False
            self.valid = bool(token or refresh_token)
            self._extra = kwargs

        @classmethod
        def from_authorized_user_file(
            cls, filename: str, scopes: Optional[list[str]] = None
        ) -> Credentials:
            data = json.loads(Path(filename).read_text(encoding="utf-8"))
            return cls(
                token=data.get("token"),
                refresh_token=data.get("refresh_token"),
                scopes=scopes or data.get("scopes") or [],
            )

        def refresh(self, request: Any) -> None:
            self.expired = False
            self.valid = True

        def to_json(self) -> str:
            return json.dumps(
                {
                    "token": self.token,
                    "refresh_token": self.refresh_token,
                    "scopes": list(self.scopes or []),
                }
            )


try:
    from google_auth_oauthlib.flow import Flow
except ImportError:  # pragma: no cover

    class Flow:  # type: ignore[no-redef]
        def __init__(
            self,
            client_config: dict[str, Any],
            scopes: list[str],
            redirect_uri: Optional[str] = None,
            state: Optional[str] = None,
        ) -> None:
            self.client_config = client_config
            self.scopes = scopes
            self.redirect_uri = redirect_uri
            self.state = state
            self.code_verifier: Optional[str] = None
            self.credentials = Credentials(
                token="mock_oauth_token_fallback",
                refresh_token="1//fallback",
                scopes=scopes,
            )

        @classmethod
        def from_client_secrets_file(
            cls,
            client_secrets_file: str,
            scopes: list[str],
            redirect_uri: Optional[str] = None,
            state: Optional[str] = None,
        ) -> Flow:
            data = json.loads(Path(client_secrets_file).read_text(encoding="utf-8"))
            return cls(
                client_config=data,
                scopes=scopes,
                redirect_uri=redirect_uri,
                state=state,
            )

        def authorization_url(self, **kwargs: Any) -> tuple[str, str]:
            state = self.state or secrets.token_urlsafe(24)
            section = (
                self.client_config.get("web")
                or self.client_config.get("installed")
                or {}
            )
            auth_uri = section.get(
                "auth_uri", "https://accounts.google.com/o/oauth2/auth"
            )
            params = {
                "response_type": "code",
                "client_id": section.get("client_id", "client"),
                "redirect_uri": self.redirect_uri or "",
                "scope": " ".join(self.scopes or []),
                "state": state,
                **{k: str(v) for k, v in kwargs.items()},
            }
            return f"{auth_uri}?{urllib.parse.urlencode(params)}", state

        def fetch_token(self, **kwargs: Any) -> dict[str, Any]:
            return {"access_token": "mock_oauth_token_fallback"}


try:
    from googleapiclient.discovery import build
except ImportError:  # pragma: no cover

    def build(*args: Any, **kwargs: Any) -> Any:  # type: ignore[misc]
        raise RuntimeError("google-api-python-client is required for live API calls.")


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth/workspace", tags=["workspace-oauth"])

DEFAULT_WORKSPACE_SCOPES: list[str] = [
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


def _get_data_dir() -> Path:
    try:
        from src.core.config_store import _resolve_data_dir

        return _resolve_data_dir()
    except Exception:
        explicit = os.environ.get("DATA_DIR") or os.environ.get("ADK_DATA_DIR")
        if explicit and explicit.strip():
            return Path(explicit.strip()).expanduser()
        return Path("/data")


DATA_DIR = _get_data_dir()
CLIENT_SECRET_PATH = DATA_DIR / "client_secret.json"
WORKSPACE_CLIENT_SECRET_PATH = DATA_DIR / "workspace_client_secret.json"
TOKEN_PATH = DATA_DIR / "workspace_token.json"
WORKSPACE_TOKEN_PATH = TOKEN_PATH


class DualSyncAsyncDict(dict):
    """A dict that can be used synchronously OR awaited in an async route, and unpacked as (auth_url, state)."""

    def __await__(self) -> Iterator[Any]:
        async def _coro() -> DualSyncAsyncDict:
            return self

        return _coro().__await__()

    def __iter__(self) -> Iterator[Any]:
        if "authorization_url" in self and "state" in self and len(self) <= 4:
            yield self["authorization_url"]
            yield self["state"]
        else:
            yield from super().__iter__()


def _write_secure_file(path: Path, content: str) -> None:
    """Atomically writes content to path with 0600 permissions and a rolling .bak backup."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        bak_path = path.with_name(f"{path.name}.bak")
        try:
            bak_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            os.chmod(bak_path, 0o600)
        except OSError:
            pass
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _read_existing_refresh_token(token_path: Path) -> Optional[str]:
    """Reads an existing OAuth refresh_token from workspace_token.json (or .bak) so re-auth never loses offline access."""
    for candidate in (token_path, token_path.with_name(f"{token_path.name}.bak")):
        if not candidate.exists():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("refresh_token"):
                return str(data["refresh_token"])
        except Exception:
            continue
    return None


def _get_cfg_manager() -> Any:
    try:
        from src.core.config_store import get_config_manager

        return get_config_manager()
    except Exception:
        return None


class WorkspaceOAuthManager:
    """Manages OAuth 2.0 client_secret.json and workspace_token.json inside DATA_DIR."""

    def __init__(self, data_dir: Optional[Path | str] = None) -> None:
        self._explicit_data_dir = Path(data_dir) if data_dir else None
        self._lock = threading.RLock()
        self._oauth_states: dict[str, dict[str, Any]] = {}

    @property
    def data_dir(self) -> Path:
        if self._explicit_data_dir is not None:
            return self._explicit_data_dir
        return _get_data_dir()

    @property
    def client_secret_path(self) -> Path:
        primary = self.data_dir / "client_secret.json"
        legacy = self.data_dir / "workspace_client_secret.json"
        if primary.exists():
            return primary
        if legacy.exists():
            return legacy
        return primary

    @property
    def token_path(self) -> Path:
        return self.data_dir / "workspace_token.json"

    def save_client_secret(self, raw_json_or_dict: str | dict[str, Any]) -> dict[str, Any]:
        """Validates and saves OAuth 2.0 Client Secret JSON with 0600 permissions."""
        with self._lock:
            if isinstance(raw_json_or_dict, str):
                try:
                    parsed = json.loads(raw_json_or_dict)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid JSON in client_secret: {exc}") from exc
            elif isinstance(raw_json_or_dict, dict):
                if "client_secret_json" in raw_json_or_dict and isinstance(
                    raw_json_or_dict["client_secret_json"], (str, dict)
                ):
                    inner = raw_json_or_dict["client_secret_json"]
                    parsed = json.loads(inner) if isinstance(inner, str) else inner
                else:
                    parsed = raw_json_or_dict
            else:
                raise ValueError("client_secret must be a JSON string or dictionary.")

            if not isinstance(parsed, dict) or ("web" not in parsed and "installed" not in parsed):
                raise ValueError(
                    "Invalid OAuth client secret JSON: must contain a 'web' or 'installed' top-level key."
                )

            serialized = json.dumps(parsed, indent=2)
            primary = self.data_dir / "client_secret.json"
            alias = self.data_dir / "workspace_client_secret.json"
            _write_secure_file(primary, serialized)
            _write_secure_file(alias, serialized)
            return {"saved": True, "path": str(primary)}

    def _resolve_preferred_redirect_uri(
        self, requested_redirect_uri: Optional[str] = None
    ) -> str:
        """Resolves a Google-OAuth-compliant redirect URI.

        Google Cloud Console rejects raw private/LAN IP addresses (e.g.
        http://192.168.x.x:8080/...) with redirect_uri_mismatch / invalid
        redirect. If the user is browsing the Web UI via their server IP, we
        automatically use the redirect_uri declared in client_secret.json or
        http://localhost:8080/api/auth/workspace/callback.
        """
        default_localhost = "http://localhost:8080/api/auth/workspace/callback"
        secret_path = self.client_secret_path
        configured_uris: list[str] = []
        if secret_path.exists():
            try:
                raw = json.loads(secret_path.read_text(encoding="utf-8"))
                client_cfg = raw.get("web") or raw.get("installed") or {}
                uris = client_cfg.get("redirect_uris") or []
                if isinstance(uris, list):
                    configured_uris = [str(u).strip() for u in uris if str(u).strip()]
            except Exception:
                pass

        if requested_redirect_uri:
            if requested_redirect_uri in configured_uris:
                return requested_redirect_uri
            # Check if requested_redirect_uri uses a raw IP address over HTTP
            try:
                from urllib.parse import urlparse
                import ipaddress

                parsed_url = urlparse(requested_redirect_uri)
                host = (parsed_url.hostname or "").strip()
                is_ip = False
                if host:
                    try:
                        ip_obj = ipaddress.ip_address(host)
                        is_ip = not ip_obj.is_loopback
                    except ValueError:
                        is_ip = False
                if is_ip and parsed_url.scheme == "http":
                    for u in configured_uris:
                        if "/api/auth/workspace/callback" in u:
                            return u
                    return default_localhost
            except Exception:
                pass

        for u in configured_uris:
            if "/api/auth/workspace/callback" in u:
                return u

        return requested_redirect_uri or default_localhost

    def generate_auth_url(
        self,
        redirect_uri: str = "http://localhost:8080/api/auth/workspace/callback",
        scopes: Optional[list[str]] = None,
    ) -> DualSyncAsyncDict:
        """Generates the Google OAuth 2.0 authorization URL and stores state metadata."""
        with self._lock:
            secret_path = self.client_secret_path
            if not secret_path.exists():
                raise FileNotFoundError(
                    f"OAuth client_secret.json not found at {secret_path}. Upload or paste it in Settings first."
                )

            effective_redirect_uri = self._resolve_preferred_redirect_uri(redirect_uri)
            effective_scopes = list(scopes) if scopes else list(DEFAULT_WORKSPACE_SCOPES)
            cfg_mgr = _get_cfg_manager()
            if not scopes and cfg_mgr is not None:
                cfg = getattr(cfg_mgr, "config", None)
                cfg_scopes = getattr(cfg, "workspace_scopes", None)
                if cfg_scopes:
                    effective_scopes = list(cfg_scopes)

            flow = Flow.from_client_secrets_file(
                str(secret_path),
                scopes=effective_scopes,
                redirect_uri=effective_redirect_uri,
            )
            authorization_url, state = flow.authorization_url(
                access_type="offline",
                include_granted_scopes="true",
                prompt="consent",
            )
            state_record = {
                "redirect_uri": effective_redirect_uri,
                "scopes": effective_scopes,
                "code_verifier": getattr(flow, "code_verifier", None),
            }
            self._oauth_states[state] = state_record
            self._oauth_states["__latest__"] = state_record
            return DualSyncAsyncDict(
                {
                    "authorization_url": authorization_url,
                    "auth_url": authorization_url,
                    "state": state,
                    "redirect_uri": effective_redirect_uri,
                }
            )

    def exchange_code(
        self,
        code: str,
        redirect_uri: Optional[str] = None,
        state: Optional[str] = None,
        scopes: Optional[list[str]] = None,
    ) -> DualSyncAsyncDict:
        """Exchanges an OAuth 2.0 authorization code for tokens and writes workspace_token.json (0600)."""
        with self._lock:
            secret_path = self.client_secret_path
            if not secret_path.exists():
                raise FileNotFoundError("OAuth client_secret.json not found.")

            state_meta = self._oauth_states.pop(state, None) if state else None
            if state_meta is None:
                state_meta = self._oauth_states.get("__latest__")
            effective_redirect_uri = (
                (state_meta or {}).get("redirect_uri")
                or self._resolve_preferred_redirect_uri(redirect_uri)
                or "http://localhost:8080/api/auth/workspace/callback"
            )
            effective_scopes = (
                scopes
                or (state_meta or {}).get("scopes")
                or list(DEFAULT_WORKSPACE_SCOPES)
            )

            flow = Flow.from_client_secrets_file(
                str(secret_path),
                scopes=effective_scopes,
                state=state,
                redirect_uri=effective_redirect_uri,
            )
            if state_meta and state_meta.get("code_verifier"):
                flow.code_verifier = state_meta["code_verifier"]

            flow.fetch_token(code=code)
            creds = flow.credentials
            if not getattr(creds, "refresh_token", None):
                prior_refresh = _read_existing_refresh_token(self.token_path)
                if prior_refresh:
                    try:
                        creds.refresh_token = prior_refresh
                    except Exception:
                        pass
            _write_secure_file(self.token_path, creds.to_json())

            connected_email = ""
            try:
                oauth2_service = build("oauth2", "v2", credentials=creds, cache_discovery=False)
                user_info = oauth2_service.userinfo().get().execute()
                connected_email = (user_info.get("email") or "").strip()
            except Exception as exc:
                logger.debug("Could not fetch userinfo email after OAuth exchange: %s", exc)

            return DualSyncAsyncDict(
                {
                    "connected": True,
                    "email": connected_email,
                    "connected_email": connected_email,
                    "token_path": str(self.token_path),
                    "has_refresh_token": bool(creds.refresh_token),
                }
            )

    def get_valid_credentials(self, raise_on_missing: bool = False) -> Optional[Credentials]:
        """Loads and automatically refreshes OAuth 2.0 Credentials from workspace_token.json."""
        with self._lock:
            tok_path = self.token_path
            if not tok_path.exists():
                if raise_on_missing:
                    raise RuntimeError(
                        "Google Workspace account is not connected yet. Open Web UI Settings -> Connect Google Workspace."
                    )
                return None

            try:
                creds = Credentials.from_authorized_user_file(str(tok_path))
            except Exception as exc:
                if raise_on_missing:
                    raise RuntimeError(f"Failed to parse workspace_token.json: {exc}") from exc
                return None

            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(GoogleAuthRequest())
                    _write_secure_file(tok_path, creds.to_json())
                except Exception as exc:
                    logger.warning("Failed to refresh Workspace OAuth token: %s", exc)
                    if raise_on_missing:
                        raise RuntimeError(f"Failed to refresh Workspace OAuth token: {exc}") from exc
                    return None

            if not creds or not creds.valid:
                if raise_on_missing:
                    raise RuntimeError(
                        "Google Workspace OAuth token is invalid or expired without a refresh token."
                    )
                return None

            return creds

    def get_status(self) -> dict[str, Any]:
        """Returns current Workspace OAuth configuration and token health."""
        with self._lock:
            secret_exists = self.client_secret_path.exists()
            token_exists = self.token_path.exists()
            cfg_mgr = _get_cfg_manager()
            connected_email = ""
            email_safety_mode = "draft_only"
            scopes = list(DEFAULT_WORKSPACE_SCOPES)

            if cfg_mgr is not None:
                cfg = getattr(cfg_mgr, "config", None)
                if cfg is not None:
                    connected_email = (
                        getattr(cfg, "workspace_connected_email", "")
                        or getattr(cfg, "owner_email", "")
                    )
                    if connected_email == "local_owner":
                        connected_email = ""
                    email_safety_mode = getattr(cfg, "email_safety_mode", "draft_only")
                    scopes = list(getattr(cfg, "workspace_scopes", scopes))

            if not token_exists:
                return {
                    "connected": False,
                    "token_present": False,
                    "client_secret_present": secret_exists,
                    "connected_email": connected_email,
                    "scopes": scopes,
                    "has_refresh_token": False,
                    "expiry": None,
                    "email_safety_mode": email_safety_mode,
                }

            try:
                creds = self.get_valid_credentials(raise_on_missing=True)
                assert creds is not None
                return {
                    "connected": True,
                    "token_present": True,
                    "client_secret_present": secret_exists,
                    "connected_email": connected_email,
                    "scopes": list(creds.scopes or scopes),
                    "has_refresh_token": bool(creds.refresh_token),
                    "expiry": creds.expiry.isoformat() if creds.expiry else None,
                    "email_safety_mode": email_safety_mode,
                }
            except Exception as exc:
                return {
                    "connected": False,
                    "token_present": token_exists,
                    "client_secret_present": secret_exists,
                    "connected_email": connected_email,
                    "scopes": scopes,
                    "has_refresh_token": False,
                    "expiry": None,
                    "email_safety_mode": email_safety_mode,
                    "error": str(exc),
                }


_oauth_manager_singleton: Optional[WorkspaceOAuthManager] = None
_singleton_lock = threading.Lock()


def get_workspace_oauth_manager() -> WorkspaceOAuthManager:
    """Returns the process-wide WorkspaceOAuthManager singleton."""
    global _oauth_manager_singleton
    with _singleton_lock:
        if _oauth_manager_singleton is None:
            _oauth_manager_singleton = WorkspaceOAuthManager()
        return _oauth_manager_singleton


def load_workspace_credentials() -> Credentials:
    """Loads valid OAuth 2.0 Credentials or raises RuntimeError if unconfigured."""
    creds = get_workspace_oauth_manager().get_valid_credentials(raise_on_missing=True)
    assert creds is not None
    return creds


# ---------------------------------------------------------------------------
# FastAPI Router Endpoints (/api/auth/workspace/*)
# ---------------------------------------------------------------------------
@router.post("/client-secret")
async def save_workspace_client_secret_route(request: Request) -> dict[str, Any]:
    payload = await request.json()  # type: ignore[attr-defined]
    try:
        return get_workspace_oauth_manager().save_client_secret(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/start")
async def start_workspace_oauth_route(request: Request) -> dict[str, Any]:
    mgr = get_workspace_oauth_manager()
    if not mgr.client_secret_path.exists():
        raise HTTPException(
            status_code=400,
            detail="Upload or paste your GCP OAuth client_secret.json first.",
        )
    redirect_uri = str(request.url_for("workspace_oauth_callback"))  # type: ignore[attr-defined]
    return dict(mgr.generate_auth_url(redirect_uri=redirect_uri))


@router.get("/callback", name="workspace_oauth_callback")
async def workspace_oauth_callback(
    request: Request,
    code: str = Query(...),
    state: Optional[str] = Query(default=None),
) -> RedirectResponse:
    mgr = get_workspace_oauth_manager()
    redirect_uri = str(request.url_for("workspace_oauth_callback"))  # type: ignore[attr-defined]
    try:
        result = mgr.exchange_code(code=code, redirect_uri=redirect_uri, state=state)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"OAuth token exchange failed: {exc}") from exc

    email = result.get("connected_email") or ""
    cfg_mgr = _get_cfg_manager()
    if cfg_mgr is not None and hasattr(cfg_mgr, "update_partial"):
        updates: dict[str, Any] = {"workspace_enabled": True}
        if email:
            updates["owner_email"] = email
            cfg = getattr(cfg_mgr, "config", None)
            allowed = list(getattr(cfg, "allowed_user_emails", []) or [])
            if email.lower() not in [e.lower() for e in allowed]:
                allowed.append(email.lower())
                updates["allowed_user_emails"] = allowed
        await cfg_mgr.update_partial(**updates)

    return RedirectResponse(url="/?view=settings&settings=workspace&status=connected")


@router.get("/status")
async def workspace_oauth_status_route() -> dict[str, Any]:
    return get_workspace_oauth_manager().get_status()
