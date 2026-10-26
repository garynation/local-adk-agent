"""Root Personal Assistant Agent combining Search Grounding, Deep Research, Workspace, and Headless Chrome."""

from __future__ import annotations

import logging
from typing import Any, Optional

try:
    from google.genai import types
except ImportError:  # pragma: no cover

    class _FallbackTypes:
        class HttpRetryOptions:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs

        class Part:
            def __init__(self, text: str = "") -> None:
                self.text = text

            @classmethod
            def from_text(cls, *, text: str) -> "_FallbackTypes.Part":
                return cls(text=text)

        class Content:
            def __init__(self, role: str = "user", parts: Optional[list[Any]] = None) -> None:
                self.role = role
                self.parts = list(parts or [])

    types = _FallbackTypes()  # type: ignore[assignment]

try:
    from google.adk import Agent
    from google.adk.models import Gemini
    from google.adk.tools import AgentTool, FunctionTool
except ImportError:  # pragma: no cover
    from .deep_research_agent import AgentTool, FunctionTool  # type: ignore[assignment]
    from .search_agent import Gemini, LlmAgent as Agent  # type: ignore[assignment]

from ..core.config_store import RuntimeConfig, get_config_manager
from .deep_research_agent import (
    build_deep_research_workflow_agent,
    run_gemini_deep_research_interaction,
)
from .search_agent import build_google_search_agent_tool

logger = logging.getLogger(__name__)

ROOT_SYSTEM_INSTRUCTION = """You are a personal AI assistant and Deep Research agent running on the user's private Linux system.
You have access to four core capability domains:
1. **Live Google Search Grounding (`google_search_agent`)**:
   - Use for up-to-date web facts, news, documentation, prices, and verification.
   - Always include clickable `[Title](URL)` citations from the search results.
2. **Deep Research (`deep_research_workflow` and `run_gemini_deep_research_interaction`)**:
   - When the user enables Deep Research mode or asks for an in-depth investigation, literature review,
     or multi-source comparison, invoke `deep_research_workflow` (or `run_gemini_deep_research_interaction`
     when native cloud Interactions API research is requested).
3. **Personal Google Workspace (Gmail, Calendar, Drive, Docs)**:
   - When Workspace tools are connected, search/read/draft emails, inspect or schedule Calendar events,
     and read/create Google Drive/Docs files in the user's personal Workspace domain.
   - Respect the configured Gmail safety mode (`draft_only` vs `allow_send`).
4. **Headless Chrome Automation (Playwright)**:
   - Use browser tools to navigate interactive websites, inspect DOM elements, fill forms, extract tables,
     and capture screenshots to `/data/screenshots/`.
"""


def _build_gemini_model(model_name: str, api_key: Optional[str]) -> Gemini:
    """Creates an ADK Gemini model explicitly bound to the AI Studio API key (vertexai=False)."""
    client_kwargs: dict[str, Any] = {"vertexai": False}
    if api_key:
        client_kwargs["api_key"] = api_key
    return Gemini(
        model=model_name,
        client_kwargs=client_kwargs,
        retry_options=types.HttpRetryOptions(initial_delay=1.0, attempts=3),
    )


def _discover_workspace_tools(cfg: RuntimeConfig) -> list[Any]:
    """Loads Personal Google Workspace tools from src.tools.workspace_tools."""
    try:
        from ..tools import workspace_tools

        if hasattr(workspace_tools, "get_workspace_tools"):
            return list(workspace_tools.get_workspace_tools())
        if hasattr(workspace_tools, "build_workspace_toolset"):
            return list(workspace_tools.build_workspace_toolset())
    except Exception as exc:
        logger.debug("Workspace tools not loaded yet: %s", exc)
    return []


def _discover_browser_tools(cfg: RuntimeConfig, include_mcp: bool = False) -> list[Any]:
    """Loads Headless Chrome Python tools (and optionally Playwright MCP toolset)."""
    tools: list[Any] = []
    try:
        from ..tools import browser_tools

        if hasattr(browser_tools, "get_python_browser_tools"):
            tools.extend(browser_tools.get_python_browser_tools())
        elif hasattr(browser_tools, "browse_webpage"):
            tools.append(FunctionTool(func=browser_tools.browse_webpage))
    except Exception as exc:
        logger.debug("Python browser tools not loaded yet: %s", exc)

    if include_mcp:
        try:
            from ..tools import browser_mcp

            if hasattr(browser_mcp, "create_playwright_mcp_toolset"):
                tools.append(browser_mcp.create_playwright_mcp_toolset())
        except Exception as exc:
            logger.debug("Playwright MCP toolset not loaded: %s", exc)

    return tools


def build_personal_assistant_root_agent(
    config: Optional[RuntimeConfig] = None,
    api_key: Optional[str] = None,
    model_override: Optional[str] = None,
    workspace_tools: Optional[list[Any]] = None,
    browser_tools: Optional[list[Any]] = None,
    include_playwright_mcp: bool = False,
) -> Agent:
    """Builds the Root ADK Personal Assistant Agent combining Search, Deep Research, Workspace, and Browser tools."""
    cfg_mgr = get_config_manager()
    cfg = config or cfg_mgr.config
    resolved_api_key = api_key if api_key is not None else cfg_mgr.get_gemini_api_key()

    primary_model_name = model_override or cfg.default_model
    pro_model = _build_gemini_model(primary_model_name, resolved_api_key)
    flash_model = _build_gemini_model(cfg.fast_model, resolved_api_key)

    search_agent_tool = build_google_search_agent_tool(flash_model)

    resolved_workspace_tools = (
        list(workspace_tools)
        if workspace_tools is not None
        else _discover_workspace_tools(cfg)
    )
    resolved_browser_tools = (
        list(browser_tools)
        if browser_tools is not None
        else _discover_browser_tools(cfg, include_mcp=include_playwright_mcp)
    )

    deep_research_workflow = build_deep_research_workflow_agent(
        pro_model=pro_model,
        flash_model=flash_model,
        browser_tools=resolved_browser_tools,
        max_iterations=cfg.max_research_iterations,
    )
    deep_research_workflow_tool = AgentTool(
        agent=deep_research_workflow,
        skip_summarization=False,
        propagate_grounding_metadata=True,
    )
    interactions_deep_research_tool = FunctionTool(
        func=run_gemini_deep_research_interaction
    )

    all_root_tools: list[Any] = [
        search_agent_tool,
        deep_research_workflow_tool,
        interactions_deep_research_tool,
        *resolved_workspace_tools,
        *resolved_browser_tools,
    ]

    return Agent(
        name="personal_adk_assistant",
        model=pro_model,
        description=(
            "Personal AI Studio ADK Assistant with Google Search Grounding, "
            "Deep Research, Personal Google Workspace, and Headless Chrome."
        ),
        instruction=ROOT_SYSTEM_INSTRUCTION,
        tools=all_root_tools,
    )
