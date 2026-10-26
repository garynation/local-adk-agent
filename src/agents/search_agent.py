"""Google Search Grounding Sub-Agent wrapped in GoogleSearchAgentTool for multi-tool compatibility."""

from __future__ import annotations

from typing import Any, Optional

try:
    from google.adk.agents import LlmAgent
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.models import Gemini
    from google.adk.models.llm_response import LlmResponse
    from google.adk.tools.google_search_agent_tool import GoogleSearchAgentTool
    from google.adk.tools.google_search_tool import GoogleSearchTool
except ImportError:  # pragma: no cover - fallback when running unit tests without google-adk installed

    class CallbackContext:  # type: ignore[no-redef]
        def __init__(self, state: Optional[dict[str, Any]] = None) -> None:
            self.state: dict[str, Any] = state if state is not None else {}

    class LlmResponse:  # type: ignore[no-redef]
        def __init__(self, grounding_metadata: Any = None) -> None:
            self.grounding_metadata = grounding_metadata

    class Gemini:  # type: ignore[no-redef]
        def __init__(self, model: str = "gemini-2.5-flash", **kwargs: Any) -> None:
            self.model = model
            self.kwargs = kwargs

    class GoogleSearchTool:  # type: ignore[no-redef]
        def __init__(self, *, bypass_multi_tools_limit: bool = True) -> None:
            self.bypass_multi_tools_limit = bypass_multi_tools_limit
            self.name = "google_search"

    class LlmAgent:  # type: ignore[no-redef]
        def __init__(self, name: str, model: Any = None, **kwargs: Any) -> None:
            self.name = name
            self.model = model
            self.description = kwargs.get("description", "")
            self.instruction = kwargs.get("instruction", "")
            self.tools = list(kwargs.get("tools", []))
            self.after_model_callback = kwargs.get("after_model_callback")
            self.output_schema = kwargs.get("output_schema")
            self.output_key = kwargs.get("output_key")

    class GoogleSearchAgentTool:  # type: ignore[no-redef]
        def __init__(self, agent: LlmAgent) -> None:
            self.agent = agent
            self.name = agent.name
            self.propagate_grounding_metadata = True


SEARCH_AGENT_INSTRUCTION = """You are a specialized Google Search Grounding sub-agent.
Your sole responsibility is to execute live Google Search queries using your built-in
`google_search` grounding tool and return accurate, up-to-date facts with explicit
source URLs and publication dates.

Rules:
1. Always rely on your built-in Google Search grounding capability. Do not fabricate URLs.
2. Structure your response with:
   - Direct, factual findings answering the query.
   - Specific dates, version numbers, prices, or quotes found in the search results.
   - A 'Sources' list at the bottom with [Title](URL) for every referenced source.
"""


def _capture_grounding_metadata_callback(
    callback_context: CallbackContext,
    llm_response: LlmResponse,
) -> Optional[LlmResponse]:
    """Extracts grounding_metadata chunks & queries into session state for the Web UI & GChat."""
    gm = getattr(llm_response, "grounding_metadata", None)
    if not gm:
        return None

    queries: list[str] = list(getattr(gm, "web_search_queries", None) or [])
    sources: list[dict[str, str]] = []
    grounding_chunks = getattr(gm, "grounding_chunks", None)
    if grounding_chunks:
        for chunk in grounding_chunks:
            web = getattr(chunk, "web", None)
            uri = getattr(web, "uri", None) if web else None
            if uri:
                title = getattr(web, "title", None) or uri
                sources.append(
                    {
                        "title": title,
                        "uri": uri,
                    }
                )

    existing: list[dict[str, Any]] = list(
        callback_context.state.get("grounding_history", [])
    )
    existing.append(
        {
            "queries": queries,
            "sources": sources,
        }
    )
    callback_context.state["grounding_history"] = existing[-20:]
    callback_context.state["last_grounding_sources"] = sources
    callback_context.state["last_grounding_queries"] = queries
    return None


def build_google_search_agent_tool(
    model: Gemini | str = "gemini-2.5-flash",
) -> GoogleSearchAgentTool:
    """Creates a dedicated GoogleSearchAgentTool safe to combine with any MCP/function tools."""
    search_sub_agent = LlmAgent(
        name="google_search_agent",
        model=model,
        description=(
            "Performs live Google Search grounding to retrieve current web information, "
            "news, documentation, prices, and factual verification with source citations. "
            "Always call this tool when the user asks about recent events, live web facts, "
            "or external information."
        ),
        instruction=SEARCH_AGENT_INSTRUCTION,
        # Isolated to built-in GoogleSearchTool so Gemini 2.5 and 3.x never mix
        # server-side grounding with client-side function_declarations in the same call.
        tools=[GoogleSearchTool(bypass_multi_tools_limit=True)],
        after_model_callback=_capture_grounding_metadata_callback,
    )
    return GoogleSearchAgentTool(agent=search_sub_agent)
