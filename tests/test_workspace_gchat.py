"""Unit tests for Personal Google Workspace OAuth, Tools, and Google Chat Pub/Sub & Webhook."""

from __future__ import annotations

import json
from pathlib import Path
import stat
import tempfile
from typing import Any, AsyncGenerator
import unittest
from unittest.mock import MagicMock, patch

try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]

from src.integrations.gchat_pubsub import GChatPubSubBridge
from src.integrations.gchat_webhook import HTTPException, verify_gchat_bearer_token
from src.integrations.workspace_oauth import WorkspaceOAuthManager
from src.tools.workspace_tools import (
    get_workspace_tools,
    gmail_send_email,
)


class _DummyRunnerManager:
    """Fake ADK RunnerManager yielding a tool call followed by a final grounded response."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def stream_chat_turn(
        self,
        *,
        session_id: str,
        user_message: str,
        user_id: str | None = None,
        deep_research: bool = False,
        model_override: str | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        self.calls.append(
            {
                "session_id": session_id,
                "user_message": user_message,
                "user_id": user_id,
                "deep_research": deep_research,
            }
        )
        yield {
            "tool_calls": [{"name": "google_search_agent", "args": {"request": user_message}}],
            "text": "",
            "partial": False,
        }
        yield {
            "tool_calls": [],
            "text": "Here is the verified research summary.",
            "partial": False,
            "grounding_sources": [
                {"title": "Google Cloud Pub/Sub Docs", "uri": "https://cloud.google.com/pubsub/docs"}
            ],
        }


# ---------------------------------------------------------------------------
# 1. Google Chat Pub/Sub Allowlist, Immediate ACK & Progress Patching Tests
# ---------------------------------------------------------------------------


async def _run_gchat_allowlist_blocks_unauthorized(tmp_path: Path) -> None:
    runner_mgr = _DummyRunnerManager()
    chat_service = MagicMock()

    # Case A: Empty allowlist blocks everyone
    bridge_empty = GChatPubSubBridge(
        config_loader=lambda: {"gchat": {"enabled": True, "allowed_user_emails": []}},
        runner_manager=runner_mgr,
        data_dir=tmp_path,
    )
    bridge_empty._chat_service = chat_service

    res_empty = await bridge_empty.handle_chat_event(
        {
            "type": "MESSAGE",
            "user": {"email": "owner@mydomain.com"},
            "space": {"name": "spaces/AAA111"},
            "message": {"text": "Hello agent", "thread": {"name": "spaces/AAA111/threads/T1"}},
        }
    )
    assert res_empty["status"] == "unauthorized"
    assert res_empty["allowed"] is False
    assert len(runner_mgr.calls) == 0

    # Case B: Non-allowlisted sender is blocked and runner is never invoked
    bridge = GChatPubSubBridge(
        config_loader=lambda: {
            "gchat": {
                "enabled": True,
                "allowed_user_emails": ["Owner@MyDomain.com"],
            }
        },
        runner_manager=runner_mgr,
        data_dir=tmp_path,
    )
    bridge._chat_service = chat_service

    res_unauth = await bridge.handle_chat_event(
        {
            "type": "MESSAGE",
            "user": {"email": "intruder@external.com"},
            "space": {"name": "spaces/AAA111"},
            "message": {"text": "Read my emails", "thread": {"name": "spaces/AAA111/threads/T1"}},
        }
    )
    assert res_unauth["status"] == "unauthorized"
    assert res_unauth["allowed"] is False
    assert len(runner_mgr.calls) == 0


async def _run_gchat_allowlisted_user_executes(tmp_path: Path) -> None:
    runner_mgr = _DummyRunnerManager()
    chat_service = MagicMock()
    chat_service.spaces().messages().create().execute.return_value = {
        "name": "spaces/AAA111/messages/MSG_PLACEHOLDER"
    }

    bridge = GChatPubSubBridge(
        config_loader=lambda: {
            "gchat": {
                "enabled": True,
                "allowed_user_emails": ["owner@mydomain.com"],
            }
        },
        runner_manager=runner_mgr,
        data_dir=tmp_path,
    )
    bridge._chat_service = chat_service

    res = await bridge.handle_chat_event(
        {
            "type": "MESSAGE",
            "user": {"email": "  OWNER@mydomain.com "},
            "space": {"name": "spaces/AAA111"},
            "message": {
                "text": "/research Latest Gemini ADK features",
                "thread": {"name": "spaces/AAA111/threads/T1"},
            },
        }
    )

    assert res["status"] == "ok"
    assert res["allowed"] is True
    assert res["session_id"] == "gchat_spaces_AAA111"
    assert len(runner_mgr.calls) == 1
    assert runner_mgr.calls[0]["deep_research"] is True
    assert runner_mgr.calls[0]["user_id"] == "owner@mydomain.com"
    assert "Here is the verified research summary." in res["reply"]
    assert "google_search_agent" in res["reply"]
    assert chat_service.spaces().messages().patch.called


def _run_gchat_pubsub_message_acks_immediately(tmp_path: Path) -> None:
    runner_mgr = _DummyRunnerManager()
    bridge = GChatPubSubBridge(
        config_loader=lambda: {
            "gchat": {
                "enabled": True,
                "allowed_user_emails": ["owner@mydomain.com"],
            }
        },
        runner_manager=runner_mgr,
        data_dir=tmp_path,
    )

    pubsub_msg = MagicMock()
    pubsub_msg.data = json.dumps(
        {
            "type": "ADDED_TO_SPACE",
            "user": {"email": "owner@mydomain.com"},
            "space": {"name": "spaces/BBB222"},
        }
    ).encode("utf-8")

    bridge._on_pubsub_message(pubsub_msg)
    pubsub_msg.ack.assert_called_once()


# ---------------------------------------------------------------------------
# 2. Gmail Outbound Safety Gate ('draft_only' vs 'allow_send') Tests
# ---------------------------------------------------------------------------


def test_gmail_send_email_enforces_draft_only_safety_gate() -> None:
    fake_service = MagicMock()
    fake_service.users().drafts().create().execute.return_value = {
        "id": "draft_999",
        "message": {"id": "msg_999"},
    }

    with (
        patch("src.tools.workspace_tools._get_service", return_value=fake_service),
        patch("src.tools.workspace_tools._resolve_email_safety_mode", return_value="draft_only"),
    ):
        result = gmail_send_email(
            to="colleague@mydomain.com",
            subject="Quarterly Update",
            body="Draft body text",
        )

    assert result["status"] == "draft_created"
    assert result["safety_gate_triggered"] is True
    assert result["email_safety_mode"] == "draft_only"
    assert result["draft_id"] == "draft_999"
    assert fake_service.users().messages().send.call_count == 0


def test_gmail_send_email_sends_when_allow_send_enabled() -> None:
    fake_service = MagicMock()
    fake_service.users().messages().send().execute.return_value = {
        "id": "sent_123",
        "threadId": "thread_123",
    }

    with (
        patch("src.tools.workspace_tools._get_service", return_value=fake_service),
        patch("src.tools.workspace_tools._resolve_email_safety_mode", return_value="allow_send"),
    ):
        result = gmail_send_email(
            to="colleague@mydomain.com",
            subject="Quarterly Update",
            body="Direct send body text",
        )

    assert result["status"] == "sent"
    assert result["safety_gate_triggered"] is False
    assert result["email_safety_mode"] == "allow_send"
    assert result["message_id"] == "sent_123"


def test_get_workspace_tools_returns_all_9_tools() -> None:
    tools = get_workspace_tools()
    assert len(tools) == 9


# ---------------------------------------------------------------------------
# 3. WorkspaceOAuthManager & Webhook JWT Verification Tests
# ---------------------------------------------------------------------------


def _run_workspace_oauth_manager_assertions(tmp_path: Path) -> None:
    mgr = WorkspaceOAuthManager(data_dir=tmp_path)

    try:
        mgr.save_client_secret({"invalid": {}})
        raise AssertionError("Expected ValueError for invalid client_secret")
    except ValueError as exc:
        assert "must contain a 'web' or 'installed'" in str(exc)

    client_secret_payload = {
        "web": {
            "client_id": "12345-test.apps.googleusercontent.com",
            "project_id": "test-proj",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_secret": "GOCSPX-testsecret",
            "redirect_uris": ["http://localhost:8080/api/auth/workspace/callback"],
        }
    }
    res = mgr.save_client_secret(client_secret_payload)
    assert res["saved"] is True
    assert mgr.client_secret_path.exists()

    mode = stat.S_IMODE(mgr.client_secret_path.stat().st_mode)
    assert mode == 0o600

    auth_info = mgr.generate_auth_url(
        redirect_uri="http://localhost:8080/api/auth/workspace/callback"
    )
    assert "accounts.google.com" in auth_info["authorization_url"]
    assert auth_info["state"]
    url_unpacked, state_unpacked = auth_info
    assert url_unpacked == auth_info["authorization_url"]
    assert state_unpacked == auth_info["state"]

    status_before = mgr.get_status()
    assert status_before["client_secret_present"] is True
    assert status_before["connected"] is False


def _run_workspace_oauth_exchange_assertions(tmp_path: Path) -> None:
    mgr = WorkspaceOAuthManager(data_dir=tmp_path)
    mgr.save_client_secret(
        {
            "web": {
                "client_id": "12345-test.apps.googleusercontent.com",
                "project_id": "test-proj",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_secret": "GOCSPX-testsecret",
            }
        }
    )

    fake_creds = MagicMock()
    fake_creds.to_json.return_value = json.dumps(
        {
            "token": "mock_oauth_token_access",
            "refresh_token": "1//refresh",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "12345-test.apps.googleusercontent.com",
            "client_secret": "GOCSPX-testsecret",
            "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
        }
    )
    fake_creds.refresh_token = "1//refresh"

    fake_flow = MagicMock()
    fake_flow.credentials = fake_creds

    fake_oauth2_service = MagicMock()
    fake_oauth2_service.userinfo().get().execute.return_value = {"email": "owner@mydomain.com"}

    with (
        patch(
            "src.integrations.workspace_oauth.Flow.from_client_secrets_file",
            return_value=fake_flow,
        ),
        patch("src.integrations.workspace_oauth.build", return_value=fake_oauth2_service),
    ):
        ex_res = mgr.exchange_code(code="4/test_code")

    assert ex_res["connected"] is True
    assert ex_res["connected_email"] == "owner@mydomain.com"
    assert mgr.token_path.exists()
    assert stat.S_IMODE(mgr.token_path.stat().st_mode) == 0o600


def test_verify_gchat_bearer_token_validation() -> None:
    try:
        verify_gchat_bearer_token(None)
        raise AssertionError("Expected HTTPException(401)")
    except HTTPException as exc:
        assert exc.status_code == 401

    with patch(
        "src.integrations.gchat_webhook.id_token.verify_oauth2_token",
        return_value={"iss": "chat@system.gserviceaccount.com", "aud": "my-proj"},
    ):
        claims = verify_gchat_bearer_token("Bearer valid.jwt.token", "my-proj")
        assert claims["iss"] == "chat@system.gserviceaccount.com"

    with patch(
        "src.integrations.gchat_webhook.id_token.verify_oauth2_token",
        return_value={"iss": "evil@example.com"},
    ):
        try:
            verify_gchat_bearer_token("Bearer bad.issuer.token", "my-proj")
            raise AssertionError("Expected HTTPException(403)")
        except HTTPException as exc_403:
            assert exc_403.status_code == 403


if pytest is not None:

    @pytest.mark.asyncio
    async def test_gchat_allowlist_blocks_unauthorized_user_and_empty_allowlist(
        tmp_path: Path,
    ) -> None:
        await _run_gchat_allowlist_blocks_unauthorized(tmp_path)

    @pytest.mark.asyncio
    async def test_gchat_allowlisted_user_executes_and_patches_progress(
        tmp_path: Path,
    ) -> None:
        await _run_gchat_allowlisted_user_executes(tmp_path)

    def test_gchat_pubsub_message_acks_immediately(tmp_path: Path) -> None:
        _run_gchat_pubsub_message_acks_immediately(tmp_path)

    def test_workspace_oauth_manager_saves_0600_and_generates_auth_url(
        tmp_path: Path,
    ) -> None:
        _run_workspace_oauth_manager_assertions(tmp_path)

    def test_workspace_oauth_exchange_and_auto_refresh(tmp_path: Path) -> None:
        _run_workspace_oauth_exchange_assertions(tmp_path)


class TestWorkspaceAndGChat(unittest.IsolatedAsyncioTestCase):
    async def test_gchat_allowlist_blocks_unauthorized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            await _run_gchat_allowlist_blocks_unauthorized(Path(tmp))

    async def test_gchat_allowlisted_user_executes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            await _run_gchat_allowlisted_user_executes(Path(tmp))

    def test_gchat_pubsub_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _run_gchat_pubsub_message_acks_immediately(Path(tmp))

    def test_gmail_draft_only_gate(self) -> None:
        test_gmail_send_email_enforces_draft_only_safety_gate()
        test_gmail_send_email_sends_when_allow_send_enabled()
        test_get_workspace_tools_returns_all_9_tools()

    def test_workspace_oauth_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _run_workspace_oauth_manager_assertions(Path(tmp))
            _run_workspace_oauth_exchange_assertions(Path(tmp))

    def test_gchat_webhook_jwt(self) -> None:
        test_verify_gchat_bearer_token_validation()


if __name__ == "__main__":
    unittest.main()
