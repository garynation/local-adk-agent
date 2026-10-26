"""Dual-Engine Deep Research Agent (ADK Multi-Agent Iterative Workflow + Gemini Interactions API)."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

from ..core.config_store import BaseModel, Field, genai

try:
    from google.adk.agents import LlmAgent, LoopAgent, ParallelAgent, SequentialAgent
    from google.adk.models import Gemini
    from google.adk.tools import AgentTool, FunctionTool, ToolContext, exit_loop
except ImportError:  # pragma: no cover - fallback when running unit tests without google-adk installed
    from .search_agent import Gemini, LlmAgent  # type: ignore[assignment]

    class ToolContext:  # type: ignore[no-redef]
        def __init__(self, state: Optional[dict[str, Any]] = None) -> None:
            self.state: dict[str, Any] = state if state is not None else {}

    def exit_loop(tool_context: Optional[ToolContext] = None) -> dict[str, str]:  # type: ignore[misc]
        return {"status": "exiting_loop"}

    class FunctionTool:  # type: ignore[no-redef]
        def __init__(self, func: Any) -> None:
            self.func = func
            self.name = getattr(func, "__name__", "function_tool")

    class AgentTool:  # type: ignore[no-redef]
        def __init__(
            self,
            agent: Any,
            *,
            skip_summarization: bool = False,
            propagate_grounding_metadata: bool = True,
        ) -> None:
            self.agent = agent
            self.name = getattr(agent, "name", "agent_tool")
            self.skip_summarization = skip_summarization
            self.propagate_grounding_metadata = propagate_grounding_metadata

    class ParallelAgent:  # type: ignore[no-redef]
        def __init__(self, name: str, description: str = "", sub_agents: Optional[list[Any]] = None) -> None:
            self.name = name
            self.description = description
            self.sub_agents = list(sub_agents or [])

    class LoopAgent:  # type: ignore[no-redef]
        def __init__(
            self,
            name: str,
            description: str = "",
            sub_agents: Optional[list[Any]] = None,
            max_iterations: int = 3,
        ) -> None:
            self.name = name
            self.description = description
            self.sub_agents = list(sub_agents or [])
            self.max_iterations = max_iterations

    class SequentialAgent:  # type: ignore[no-redef]
        def __init__(self, name: str, description: str = "", sub_agents: Optional[list[Any]] = None) -> None:
            self.name = name
            self.description = description
            self.sub_agents = list(sub_agents or [])

from .search_agent import build_google_search_agent_tool

logger = logging.getLogger(__name__)


class SubQuestion(BaseModel):
    id: str = Field(default="Q1", description="Short identifier such as Q1, Q2, Q3.")
    question: str = Field(default="", description="Specific research sub-question to answer.")
    search_queries: list[str] = Field(
        default_factory=list,
        description="2-3 targeted Google Search queries for this sub-question.",
    )
    candidate_urls_to_browse: list[str] = Field(
        default_factory=list,
        description="Specific documentation or article URLs to inspect with Headless Chrome if known.",
    )


class ResearchPlan(BaseModel):
    topic_summary: str = Field(default="", description="Clear statement of the research goal.")
    sub_questions: list[SubQuestion] = Field(
        default_factory=list,
        description="3 to 5 orthogonal sub-questions decomposing the research topic.",
    )
    deliverable_sections: list[str] = Field(
        default_factory=list,
        description="Ordered section headings for the final comprehensive report.",
    )


async def run_gemini_deep_research_interaction(
    research_prompt: str,
    tool_context: Optional[ToolContext] = None,
    agent_version: str = "deep-research-preview-04-2026",
    max_wait_seconds: int = 420,
) -> dict[str, Any]:
    """Runs Google's native Gemini Deep Research agent via the google-genai Interactions API."""
    state = tool_context.state if tool_context is not None else {}
    api_key = state.get("app:gemini_api_key") or os.environ.get("GEMINI_API_KEY")
    client = (
        genai.Client(api_key=api_key, vertexai=False)
        if api_key
        else genai.Client(vertexai=False)
    )

    try:
        interaction = await client.aio.interactions.create(
            input=research_prompt,
            agent=agent_version,
            background=True,
        )
    except Exception as exc:
        logger.warning(
            "Interactions API Deep Research (%s) unavailable (%s); instruct caller to use ADK deep_research_workflow.",
            agent_version,
            exc,
        )
        return {
            "status": "fallback_required",
            "reason": (
                f"Interactions API agent '{agent_version}' returned: {exc}. "
                "Use 'deep_research_workflow' tool instead."
            ),
        }

    interaction_id = getattr(interaction, "id", "unknown")
    state["active_deep_research_interaction_id"] = interaction_id
    start_ts = time.monotonic()
    delay = 5.0

    while (time.monotonic() - start_ts) < max_wait_seconds:
        current = await client.aio.interactions.get(interaction_id)
        status = getattr(current, "status", "unknown")
        state["deep_research_status"] = status

        if status == "completed":
            outputs = getattr(current, "outputs", None) or []
            report_text = ""
            if outputs:
                last_out = outputs[-1]
                report_text = getattr(last_out, "text", "") or str(last_out)
            return {
                "status": "completed",
                "interaction_id": interaction_id,
                "report_markdown": report_text,
            }

        if status in ("failed", "cancelled", "incomplete", "budget_exceeded"):
            err = getattr(current, "error", None)
            return {
                "status": status,
                "interaction_id": interaction_id,
                "error": str(err) if err else f"Interaction ended with status: {status}",
            }

        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 15.0)

    return {
        "status": "timeout",
        "interaction_id": interaction_id,
        "error": f"Deep research interaction {interaction_id} exceeded {max_wait_seconds}s.",
    }


def build_deep_research_workflow_agent(
    pro_model: Gemini | str = "gemini-2.5-pro",
    flash_model: Gemini | str = "gemini-2.5-flash",
    browser_tools: Optional[list[Any]] = None,
    max_iterations: int = 3,
) -> SequentialAgent:
    """Builds the multi-step ADK Deep Research Agent (Planner -> Loop(Parallel Search + Browser -> Critic) -> Synthesizer)."""

    search_tool_for_worker = build_google_search_agent_tool(flash_model)
    search_tool_for_critic = build_google_search_agent_tool(flash_model)

    planner_agent = LlmAgent(
        name="research_planner",
        model=pro_model,
        description="Decomposes a complex topic into structured sub-questions and search queries.",
        instruction="""You are the Lead Research Planner.
Given the user's research request, produce a structured ResearchPlan with 3-5 orthogonal
sub-questions, concrete Google Search queries for each, and the target report sections.
Also take into account any prior context in the conversation.""",
        output_schema=ResearchPlan,
        output_key="research_plan",
    )

    web_search_worker = LlmAgent(
        name="web_search_explorer",
        model=flash_model,
        description="Executes multi-angle Google Searches for the current research plan and follow-up gaps.",
        instruction="""You are the Web Search Research Explorer.
Read the research plan in {research_plan} and any remaining gaps in {research_gaps?}.
Call `google_search_agent` multiple times to gather authoritative facts, statistics, dates,
and primary source URLs for every sub-question.
Compile detailed evidence notes with exact [Title](URL) citations.""",
        tools=[search_tool_for_worker],
        output_key="latest_web_findings",
    )

    parallel_branches: list[Any] = [web_search_worker]
    if browser_tools:
        browser_reader_worker = LlmAgent(
            name="headless_browser_reader",
            model=flash_model,
            description="Uses Headless Chrome to open and extract full text/tables from high-value URLs.",
            instruction="""You are the Deep Page Browser Specialist.
Inspect {research_plan} and {research_gaps?}. When specific documentation URLs, technical pages,
or primary sources need full-page inspection beyond search snippets, use your Headless Chrome
tools to navigate, extract full text/tables, and capture a verification screenshot if helpful.
Summarize all extracted technical details with their source URLs.""",
            tools=list(browser_tools),
            output_key="latest_browser_findings",
        )
        parallel_branches.append(browser_reader_worker)

    parallel_investigation = ParallelAgent(
        name="parallel_investigation",
        description="Runs web search grounding and headless browser deep reading in parallel.",
        sub_agents=parallel_branches,
    )

    coverage_critic = LlmAgent(
        name="coverage_critic",
        model=pro_model,
        description="Evaluates gathered evidence against the research plan and either exits the loop or specifies follow-up queries.",
        instruction="""You are the Research Coverage Critic.
Review the original plan in {research_plan}, the latest web findings in {latest_web_findings?},
and browser findings in {latest_browser_findings?}.
1. If all sub-questions in {research_plan} are answered with verified facts and primary source URLs,
   call `exit_loop` immediately and output a consolidated summary of all verified findings.
2. If critical sub-questions or contradictions remain unresolved, DO NOT call `exit_loop`.
   Instead, state the exact missing facts and targeted follow-up search queries/URLs to investigate
   in the next iteration (and optionally run a quick `google_search_agent` check).""",
        tools=[exit_loop, search_tool_for_critic],
        output_key="research_gaps",
    )

    iterative_research_loop = LoopAgent(
        name="iterative_research_loop",
        description="Iteratively searches, browses, and verifies until the research plan is satisfied.",
        sub_agents=[parallel_investigation, coverage_critic],
        max_iterations=max_iterations,
    )

    report_synthesizer = LlmAgent(
        name="citation_report_synthesizer",
        model=pro_model,
        description="Synthesizes all gathered evidence into a structured, cited Markdown report.",
        instruction="""You are the Principal Research Synthesizer.
Using the research plan in {research_plan}, web findings in {latest_web_findings?},
browser findings in {latest_browser_findings?}, and critic notes in {research_gaps?},
write a comprehensive, executive-ready Markdown research report.

Formatting Requirements:
1. Start with an **Executive Summary** (3-5 bullet takeaways).
2. Follow the sections planned in {research_plan}.
3. Include Markdown comparison tables for quantitative or architectural trade-offs.
4. Cite every claim inline using numbered Markdown links `[1](https://...)` and include a
   numbered **Sources & References** section at the end with full clickable `[Title](URL)` links.
5. Never invent citations; only cite URLs verified during the research steps.""",
        output_key="final_research_report",
    )

    return SequentialAgent(
        name="deep_research_workflow",
        description=(
            "Executes a multi-step Deep Research workflow: creates a structured ResearchPlan, "
            "runs iterative parallel Google Search grounding and Headless Chrome page extraction, "
            "verifies coverage, and synthesizes a publication-grade report with inline citations."
        ),
        sub_agents=[
            planner_agent,
            iterative_research_loop,
            report_synthesizer,
        ],
    )
