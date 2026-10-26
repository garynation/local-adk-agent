"""ADK Agent definitions: Search Grounding, Deep Research, and Root Personal Assistant."""

from typing import Any

__all__ = [
    "ROOT_SYSTEM_INSTRUCTION",
    "ResearchPlan",
    "SEARCH_AGENT_INSTRUCTION",
    "SubQuestion",
    "_capture_grounding_metadata_callback",
    "build_deep_research_workflow_agent",
    "build_google_search_agent_tool",
    "build_personal_assistant_root_agent",
    "run_gemini_deep_research_interaction",
]


def __getattr__(name: str) -> Any:
    if name in {
        "ResearchPlan",
        "SubQuestion",
        "build_deep_research_workflow_agent",
        "run_gemini_deep_research_interaction",
    }:
        from . import deep_research_agent

        return getattr(deep_research_agent, name)
    if name in {
        "ROOT_SYSTEM_INSTRUCTION",
        "build_personal_assistant_root_agent",
    }:
        from . import root_agent

        return getattr(root_agent, name)
    if name in {
        "SEARCH_AGENT_INSTRUCTION",
        "_capture_grounding_metadata_callback",
        "build_google_search_agent_tool",
    }:
        from . import search_agent

        return getattr(search_agent, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

