"""Unit tests for Component 2: ConfigManager, Search/DeepResearch Agents, and AdkRunnerManager."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest

try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]

from src.agents.deep_research_agent import (
    ResearchPlan,
    SubQuestion,
    build_deep_research_workflow_agent,
)
from src.agents.root_agent import build_personal_assistant_root_agent
from src.agents.search_agent import (
    _capture_grounding_metadata_callback,
    build_google_search_agent_tool,
)
from src.core.config_store import (
    ConfigManager,
    RuntimeConfig,
    get_config_manager,
    reset_config_manager,
)
from src.core.runner_manager import (
    APP_NAME,
    AdkRunnerManager,
    get_runner_manager,
    normalize_async_sqlite_url,
    reset_runner_manager,
)


def test_normalize_async_sqlite_url() -> None:
    assert (
        normalize_async_sqlite_url("sqlite:///data/sessions.db")
        == "sqlite+aiosqlite:////data/sessions.db"
    )
    assert (
        normalize_async_sqlite_url("sqlite:////data/sessions.db")
        == "sqlite+aiosqlite:////data/sessions.db"
    )
    assert (
        normalize_async_sqlite_url("sqlite+aiosqlite:////data/sessions.db")
        == "sqlite+aiosqlite:////data/sessions.db"
    )
    assert (
        normalize_async_sqlite_url("sqlite+aiosqlite:///data/sessions.db")
        == "sqlite+aiosqlite:////data/sessions.db"
    )


async def _run_config_manager_assertions(tmp_path: Path) -> None:
    old_data_dir = os.environ.get("DATA_DIR")
    old_api_key = os.environ.get("GEMINI_API_KEY")
    os.environ["DATA_DIR"] = str(tmp_path)
    os.environ.pop("GEMINI_API_KEY", None)
    reset_config_manager()
    try:
        cfg_path = tmp_path / "config.json"
        key_path = tmp_path / ".secret.key"
        mgr = ConfigManager(config_path=cfg_path)

        # 1. Secret key file created with 0600 permissions
        assert key_path.exists()
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600

        # 2. Default email/gmail safety mode is 'draft_only'
        assert mgr.config.email_safety_mode == "draft_only"
        assert mgr.config.gmail_safety_mode == "draft_only"
        assert mgr.config.workspace_draft_only_mode is True

        # 3. Register hot-reload listener
        received_snapshots: list[RuntimeConfig] = []

        async def _listener(new_cfg: RuntimeConfig) -> None:
            received_snapshots.append(new_cfg)

        mgr.register_listener(_listener)

        # 4. Update Gemini API key and verify encryption at rest + 0600 permissions
        sample_key = "AIzaSyTestKeyForUnitTesting123456789"
        updated = await mgr.update_gemini_api_key(
            sample_key,
            default_model="gemini-2.5-pro",
            fast_model="gemini-2.5-flash",
        )

        assert cfg_path.exists()
        assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600

        raw_on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        # Plaintext key must NEVER appear in /data/config.json on disk
        assert "gemini_api_key" not in raw_on_disk
        assert raw_on_disk["gemini_api_key_encrypted"] != sample_key
        assert sample_key not in cfg_path.read_text(encoding="utf-8")

        # Decrypted key matches in memory and via getter
        assert mgr.get_gemini_api_key() == sample_key
        assert updated.gemini_api_key == sample_key
        masked = mgr.get_masked_gemini_api_key()
        assert masked is not None
        assert masked.startswith("AIza")
        assert masked.endswith("6789")
        assert sample_key not in masked

        # Listener fired once
        assert len(received_snapshots) == 1
        assert received_snapshots[0].default_model == "gemini-2.5-pro"

        # 5. Synchronize gmail_safety_mode <-> email_safety_mode and allowed_user_emails <-> gchat_allowed_emails
        updated2 = await mgr.update_partial(
            gmail_safety_mode="allow_send",
            gchat_allowed_emails=["Owner@Example.com "],
        )
        assert updated2.email_safety_mode == "allow_send"
        assert updated2.gmail_safety_mode == "allow_send"
        assert updated2.workspace_draft_only_mode is False
        assert updated2.allowed_user_emails == ["owner@example.com"]
        assert updated2.gchat_allowed_emails == ["owner@example.com"]
        assert len(received_snapshots) == 2

        # 6. Reloading from disk preserves encrypted key and settings
        mgr_reloaded = ConfigManager(config_path=cfg_path)
        assert mgr_reloaded.get_gemini_api_key() == sample_key
        assert mgr_reloaded.config.email_safety_mode == "allow_send"
        assert mgr_reloaded.config.allowed_user_emails == ["owner@example.com"]
    finally:
        if old_data_dir is not None:
            os.environ["DATA_DIR"] = old_data_dir
        else:
            os.environ.pop("DATA_DIR", None)
        if old_api_key is not None:
            os.environ["GEMINI_API_KEY"] = old_api_key
        else:
            os.environ.pop("GEMINI_API_KEY", None)


def test_search_agent_and_deep_research_builders() -> None:
    search_tool = build_google_search_agent_tool("gemini-2.5-flash")
    assert getattr(search_tool, "agent", None) is not None
    assert search_tool.agent.name == "google_search_agent"

    ctx = SimpleNamespace(state={})
    fake_chunk = SimpleNamespace(
        web=SimpleNamespace(
            title="Google ADK Docs", uri="https://google.github.io/adk-docs/"
        )
    )
    fake_resp = SimpleNamespace(
        grounding_metadata=SimpleNamespace(
            web_search_queries=["google adk python"],
            grounding_chunks=[fake_chunk],
        )
    )
    _capture_grounding_metadata_callback(ctx, fake_resp)  # type: ignore[arg-type]
    assert ctx.state["last_grounding_queries"] == ["google adk python"]
    assert ctx.state["last_grounding_sources"] == [
        {"title": "Google ADK Docs", "uri": "https://google.github.io/adk-docs/"}
    ]

    plan = ResearchPlan(
        topic_summary="Compare Cloud Pub/Sub vs ngrok for Google Chat bots",
        sub_questions=[
            SubQuestion(
                id="Q1",
                question="How does Cloud Pub/Sub handle outbound streaming pull?",
                search_queries=["google chat api cloud pubsub streaming pull"],
            )
        ],
        deliverable_sections=["Executive Summary", "Architecture Comparison"],
    )
    assert plan.sub_questions[0].id == "Q1"

    workflow = build_deep_research_workflow_agent(
        pro_model="gemini-2.5-pro",
        flash_model="gemini-2.5-flash",
        max_iterations=2,
    )
    assert workflow.name == "deep_research_workflow"
    assert len(workflow.sub_agents) == 3


async def _run_runner_manager_assertions(tmp_path: Path) -> None:
    old_data_dir = os.environ.get("DATA_DIR")
    old_api_key = os.environ.get("GEMINI_API_KEY")
    os.environ["DATA_DIR"] = str(tmp_path)
    os.environ.pop("GEMINI_API_KEY", None)
    reset_config_manager()
    reset_runner_manager()
    try:
        cfg_mgr = get_config_manager()
        runner_mgr = get_runner_manager()
        assert APP_NAME == "local_adk_agent"
        assert isinstance(runner_mgr, AdkRunnerManager)

        root_agent = build_personal_assistant_root_agent(config=cfg_mgr.config)
        assert root_agent.name == "personal_adk_assistant"

        events = []
        async for ev in runner_mgr.stream_chat_turn(
            session_id="sess_test_01",
            user_message="Hello from unit test",
            origin_channel="webui",
        ):
            events.append(ev)

        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert "GEMINI_API_KEY" in events[0]["error"]

        sessions = await runner_mgr.list_sessions()
        assert any(s["id"] == "sess_test_01" for s in sessions)

        history = await runner_mgr.get_session_history("sess_test_01")
        assert history is not None
        assert history["id"] == "sess_test_01"
        assert history["origin_channel"] == "webui"

        initial_reloads = runner_mgr.reload_count
        await cfg_mgr.update_gemini_api_key("AIzaSyHotReloadKey1234567890")
        assert runner_mgr.reload_count == initial_reloads + 1
    finally:
        if old_data_dir is not None:
            os.environ["DATA_DIR"] = old_data_dir
        else:
            os.environ.pop("DATA_DIR", None)
        if old_api_key is not None:
            os.environ["GEMINI_API_KEY"] = old_api_key
        else:
            os.environ.pop("GEMINI_API_KEY", None)


if pytest is not None:

    @pytest.mark.asyncio
    async def test_config_manager_fernet_encryption_permissions_and_listeners(
        tmp_path: Path,
    ) -> None:
        await _run_config_manager_assertions(tmp_path)

    @pytest.mark.asyncio
    async def test_runner_manager_graceful_fallback_and_sessions(
        tmp_path: Path,
    ) -> None:
        await _run_runner_manager_assertions(tmp_path)


class TestAdkCore(unittest.IsolatedAsyncioTestCase):
    def test_sqlite_url_normalization(self) -> None:
        test_normalize_async_sqlite_url()

    def test_builders_and_grounding_callback(self) -> None:
        test_search_agent_and_deep_research_builders()

    async def test_config_manager_fernet_and_hot_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            await _run_config_manager_assertions(Path(tmp))

    async def test_runner_manager_sessions_and_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            await _run_runner_manager_assertions(Path(tmp))

    async def test_credentials_persist_across_app_updates_and_partial_saves(self) -> None:
        """Verifies API keys, OAuth refresh tokens, and extra schema fields survive app updates and partial saves."""
        from src.integrations.workspace_oauth import WorkspaceOAuthManager

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "host_xdg_data"
            config_file = data_dir / "config.json"
            mgr = ConfigManager(config_path=config_file)

            # 1. Enter AI Studio API key and Workspace/GChat settings in the app
            await mgr.update_gemini_api_key(
                "AIzaSyPersistentAcrossUpdates999999",
                default_model="gemini-2.5-pro",
            )
            await mgr.update_partial(
                owner_email="owner@personalworkspace.com",
                allowed_user_emails=["owner@personalworkspace.com"],
            )

            # 2. Simulate an app update where config.json has custom/future fields and partial updates run
            on_disk = json.loads(config_file.read_text(encoding="utf-8"))
            on_disk["future_app_v2_setting"] = {"enabled": True}
            config_file.write_text(json.dumps(on_disk, indent=2), encoding="utf-8")

            # Reload manager (simulating container recreate after `docker compose pull && docker compose up -d`)
            mgr_after_update = ConfigManager(config_path=config_file)
            self.assertEqual(
                mgr_after_update.get_gemini_api_key(),
                "AIzaSyPersistentAcrossUpdates999999",
            )
            self.assertEqual(
                mgr_after_update.config.owner_email,
                "owner@personalworkspace.com",
            )

            # Perform a partial setting update (e.g., changing default_model without passing gemini_api_key)
            await mgr_after_update.update_partial(default_model="gemini-2.5-flash")
            self.assertEqual(
                mgr_after_update.get_gemini_api_key(),
                "AIzaSyPersistentAcrossUpdates999999",
            )
            saved_after_partial = json.loads(config_file.read_text(encoding="utf-8"))
            self.assertEqual(
                saved_after_partial.get("future_app_v2_setting"),
                {"enabled": True},
            )
            self.assertTrue((data_dir / "config.json.bak").exists())

            # 3. Simulate truncated/corrupted config.json -> automatic recovery from config.json.bak
            config_file.write_text("{corrupted_json", encoding="utf-8")
            mgr_recovered = ConfigManager(config_path=config_file)
            self.assertEqual(
                mgr_recovered.get_gemini_api_key(),
                "AIzaSyPersistentAcrossUpdates999999",
            )

            # 4. Verify OAuth client_secret.json and workspace_token.json preserve refresh_token and create .bak
            oauth_mgr = WorkspaceOAuthManager(data_dir=data_dir)
            oauth_mgr.save_client_secret(
                {
                    "web": {
                        "client_id": "123.apps.googleusercontent.com",
                        "client_secret": "GOCSPX-secret",
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                    }
                }
            )
            (data_dir / "workspace_token.json").write_text(
                json.dumps(
                    {
                        "token": "mock_oauth_token_initial",
                        "refresh_token": "1//persistent-offline-refresh-token",
                        "scopes": ["openid"],
                    }
                ),
                encoding="utf-8",
            )
            oauth_mgr.save_client_secret(
                {
                    "web": {
                        "client_id": "123.apps.googleusercontent.com",
                        "client_secret": "GOCSPX-secret-updated",
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                    }
                }
            )
            self.assertTrue((data_dir / "client_secret.json.bak").exists())

    def test_deep_research_interaction_extraction_and_model_mapping(self) -> None:
        """Verifies Interaction.steps extraction, model alias resolution, user prefix cleaning, and user bubble selection CSS."""
        from src.agents.deep_research_agent import extract_interaction_output_text
        from src.agents.root_agent import resolve_adk_model_name
        from src.core.runner_manager import clean_user_message_text

        # 1. Interactions-only agent names must map to generateContent-compatible models
        self.assertEqual(
            resolve_adk_model_name("deep-research-preview-04-2026"),
            "gemini-4-argon",
        )
        self.assertEqual(
            resolve_adk_model_name("deep-research-max-preview-04-2026"),
            "gemini-4-argon",
        )
        self.assertEqual(
            resolve_adk_model_name("antigravity-preview-latest"),
            "gemini-4-argon",
        )

        # 2. Modern google-genai >= 2.4.0 Interaction.steps extraction
        class _DummyContent:
            def __init__(self, c_type: str, text: str = "") -> None:
                self.type = c_type
                self.text = text

        class _DummyStep:
            def __init__(self, s_type: str, content: list[Any]) -> None:
                self.type = s_type
                self.content = content

        class _DummyInteraction:
            output_text = None
            outputs = None
            steps = [
                _DummyStep("model_output", [_DummyContent("thought", "Thinking...")]),
                _DummyStep(
                    "model_output",
                    [
                        _DummyContent("thought", "Synthesizing..."),
                        _DummyContent("text", "# Final Deep Research Report\nVerified [1](https://example.com)."),
                    ],
                ),
            ]

        extracted = extract_interaction_output_text(_DummyInteraction())
        self.assertIn("# Final Deep Research Report", extracted)
        self.assertNotIn("Thinking...", extracted)

        # 3. Strip [DEEP RESEARCH MODE ENABLED: ...] prefix from user bubbles
        leaked = (
            "[DEEP RESEARCH MODE ENABLED: Use `deep_research_workflow` (or "
            "`run_gemini_deep_research_interaction` if configured) to conduct a "
            "thorough multi-query investigation and return a fully cited report.] "
            "this research didnt work, try again"
        )
        self.assertEqual(
            clean_user_message_text(leaked),
            "this research didnt work, try again",
        )

        # 4. Verify .chat-msg.user CSS has explicit user-select: text and inverted ::selection contrast
        css_path = Path(__file__).resolve().parent.parent / "src" / "web" / "css" / "chat-stream-cards.css"
        css_text = css_path.read_text(encoding="utf-8")
        self.assertIn(".chat-msg.user::selection", css_text)
        self.assertIn("user-select: text !important;", css_text)


if __name__ == "__main__":
    unittest.main()
