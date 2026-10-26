"""FastAPI TestClient and unittest tests for Local ADK Agent Web UI, REST APIs, and Auth Middleware."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
import unittest

try:
    import pytest  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]

try:
    from fastapi.testclient import TestClient  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    TestClient = None  # type: ignore[assignment]

from src.auth_middleware import (
    ADMIN_SESSION_COOKIE,
    compute_session_cookie_token,
    hash_admin_pin,
    is_ip_allowed,
    verify_admin_pin,
)

try:
    from src.main import create_app
except ImportError:  # pragma: no cover
    create_app = None  # type: ignore[assignment]


if pytest is not None and TestClient is not None and create_app is not None:

    @pytest.fixture()
    def temp_data_dir(tmp_path: Path, monkeypatch: Any) -> Path:
        """Create an isolated temporary DATA_DIR for each test."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "screenshots").mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("DATA_DIR", str(data_dir))
        return data_dir

    @pytest.fixture()
    def client(temp_data_dir: Path) -> Any:
        """Return a FastAPI TestClient wired to the isolated temp_data_dir."""
        app = create_app()
        return TestClient(app)



def test_health_endpoint(client: TestClient) -> None:
    """GET /api/health returns 200 OK with expected health schema."""
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "gemini_configured" in data
    assert "workspace_connected" in data
    assert "gchat_enabled" in data
    assert "sessions_db" in data
    assert "browser_ready" in data


def test_config_status_endpoint(client: TestClient) -> None:
    """GET /api/config/status returns 200 OK with masked configuration state."""
    response = client.get("/api/config/status")
    assert response.status_code == 200
    data = response.json()
    assert "gemini_configured" in data
    assert "gemini_key_masked" in data
    assert "default_model" in data
    assert "workspace_oauth_configured" in data
    assert "workspace_connected" in data
    assert "gchat_enabled" in data
    assert "browser_headless" in data
    assert "admin_pin_enabled" in data


def test_sessions_lifecycle(client: TestClient) -> None:
    """GET /api/sessions, POST /api/sessions, and DELETE /api/sessions/{id} work cleanly."""
    # Initially lists sessions
    list_res = client.get("/api/sessions")
    assert list_res.status_code == 200
    body = list_res.json()
    assert "sessions" in body
    assert isinstance(body["sessions"], list)

    # Create a new session
    create_res = client.post("/api/sessions", json={"session_id": "test-session-01"})
    assert create_res.status_code == 200
    created = create_res.json()
    assert created["status"] == "ok"
    assert created["session_id"] == "test-session-01"

    # Fetch session history
    detail_res = client.get("/api/sessions/test-session-01")
    assert detail_res.status_code == 200
    detail = detail_res.json()
    assert detail["session_id"] == "test-session-01"
    assert "messages" in detail

    # Delete session
    del_res = client.delete("/api/sessions/test-session-01")
    assert del_res.status_code == 200
    assert del_res.json()["status"] == "ok"


def test_index_html_rendering_and_static_assets(client: TestClient) -> None:
    """GET / renders the GM3 Expressive HTML shell and serves tokenized CSS."""
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    html = response.text
    assert 'id="view-workbench"' in html
    assert 'id="view-settings"' in html
    assert 'id="theme-toggle-btn"' in html
    assert "brightness_auto" in html
    assert "/static/css/gm3-color-tokens.css" in html

    # Verify static CSS asset serving
    css_res = client.get("/static/css/gm3-color-tokens.css")
    assert css_res.status_code == 200
    assert "--md-sys-color-primary" in css_res.text


def test_gemini_key_and_workspace_config_routes(client: TestClient) -> None:
    """POST /api/config/gemini-key and /api/auth/workspace/client-secret validate and persist."""
    # Invalid key rejected
    bad_res = client.post("/api/config/gemini-key", json={"api_key": "short", "model": "gemini-2.5-flash"})
    assert bad_res.status_code == 400

    # Valid-shaped key saved
    good_res = client.post(
        "/api/config/gemini-key",
        json={"api_key": "AIzaSyTestKeyForUnitTesting1234567890", "model": "gemini-2.5-flash"},
    )
    assert good_res.status_code == 200
    assert good_res.json()["status"] == "ok"

    # Status reflects saved key
    status_res = client.get("/api/config/status")
    assert status_res.status_code == 200
    assert status_res.json()["gemini_configured"] is True

    # Workspace client secret upload
    ws_res = client.post(
        "/api/auth/workspace/client-secret",
        json={
            "client_id": "123456-test.apps.googleusercontent.com",
            "client_secret": "GOCSPX-testsecret123",
            "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
        },
    )
    assert ws_res.status_code == 200
    assert ws_res.json()["workspace_oauth_configured"] is True


def test_auth_middleware_pin_and_cidr_guard(client: TestClient) -> None:
    """Verify PBKDF2-HMAC-SHA256 PIN hashing, CIDR validation, and middleware cookie protection."""
    # Unit test CIDR helper
    assert is_ip_allowed("127.0.0.1") is True
    assert is_ip_allowed("100.64.10.5") is True
    assert is_ip_allowed("192.168.1.50") is True
    assert is_ip_allowed("8.8.8.8") is False

    # Unit test PIN hashing
    record = hash_admin_pin("4321")
    assert verify_admin_pin("4321", record["salt"], record["pin_hash"]) is True
    assert verify_admin_pin("9999", record["salt"], record["pin_hash"]) is False

    # Enable Admin PIN via /api/config/security
    sec_res = client.post("/api/config/security", json={"pin": "2468"})
    assert sec_res.status_code == 200
    assert sec_res.json()["admin_pin_enabled"] is True

    # /api/health remains exempt even without cookie
    assert client.get("/api/health").status_code == 200

    # Protected route returns 401 when no session cookie is set
    unauth_Client = TestClient(client.app)
    unauth_res = unauth_Client.get("/api/config/status")
    assert unauth_res.status_code == 401

    # Login with valid PIN sets cookie and grants access
    login_res = unauth_Client.post("/api/auth/login", json={"pin": "2468"})
    assert login_res.status_code == 200
    assert ADMIN_SESSION_COOKIE in login_res.cookies

    auth_res = unauth_Client.get("/api/config/status")
    assert auth_res.status_code == 200


class TestWebUiAndAuthMiddleware(unittest.TestCase):
    """Standard library unittest suite for Web UI assets, Auth Middleware, and PIN/CIDR guards."""

    def test_cidr_allowlist_guard(self) -> None:
        self.assertTrue(is_ip_allowed("127.0.0.1"))
        self.assertTrue(is_ip_allowed("::1"))
        self.assertTrue(is_ip_allowed("100.64.10.5"))
        self.assertTrue(is_ip_allowed("192.168.1.50"))
        self.assertFalse(is_ip_allowed("8.8.8.8"))

    def test_pin_hashing_and_session_cookie_token(self) -> None:
        record = hash_admin_pin("4321")
        self.assertTrue(verify_admin_pin("4321", record["salt"], record["pin_hash"]))
        self.assertFalse(verify_admin_pin("9999", record["salt"], record["pin_hash"]))

        salt, pin_hash = hash_admin_pin("8642")
        self.assertTrue(verify_admin_pin("8642", pin_hash, salt))
        self.assertFalse(verify_admin_pin("0000", pin_hash, salt))

        token_a = compute_session_cookie_token("test-secret-key")
        token_b = compute_session_cookie_token("test-secret-key")
        token_other = compute_session_cookie_token("wrong-secret-key")
        self.assertEqual(token_a, token_b)
        self.assertNotEqual(token_a, token_other)

    def test_web_ui_gm3_static_files_and_views(self) -> None:
        web_dir = Path(__file__).resolve().parent.parent / "src" / "web"
        index_html = (web_dir / "index.html").read_text(encoding="utf-8")
        app_js = (web_dir / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="view-workbench"', index_html)
        self.assertIn('id="view-settings"', index_html)
        self.assertIn('id="theme-toggle-btn"', index_html)
        self.assertIn("brightness_auto", index_html)
        self.assertIn("/static/css/gm3-color-tokens.css", index_html)
        self.assertIn(".view-panel:not(.active)", index_html)
        self.assertIn("gemini-4.0-pro", index_html)
        self.assertIn("gemini-4.0-flash", index_html)
        self.assertNotIn("Tokyo flight itinerary", index_html)
        self.assertNotIn("Tokyo flight itinerary", app_js)
        self.assertNotIn("SEED_CONVERSATIONS", app_js)

        tokens_css = (web_dir / "css" / "gm3-color-tokens.css").read_text(encoding="utf-8")
        self.assertIn("--md-sys-color-primary", tokens_css)
        self.assertIn("--md-sys-color-surface-container-low", tokens_css)


if __name__ == "__main__":
    unittest.main()

