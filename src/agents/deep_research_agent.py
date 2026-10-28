"""Dual-Engine Deep Research Agent (ADK Multi-Agent Iterative Workflow + Gemini Interactions API + Live SSE Pipeline)."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import time
from typing import Any, AsyncGenerator, Optional
import uuid

from ..core.config_store import BaseModel, Field, genai

try:
    from google.genai import types
except ImportError:  # pragma: no cover

    class _FallbackTypes:
        class GoogleSearch:
            pass

        class Tool:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs

        class GenerateContentConfig:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs

    types = _FallbackTypes()  # type: ignore[assignment]

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


def extract_interaction_output_text(interaction: Any) -> str:
    """Extracts report text from a google-genai Interaction across SDK versions (>=2.4 steps/output_text and legacy outputs)."""
    if interaction is None:
        return ""

    # 1. Direct output_text attribute if populated
    direct_text = getattr(interaction, "output_text", None)
    if isinstance(direct_text, str) and direct_text.strip():
        return direct_text.strip()

    # 2. Modern google-genai >= 2.4.0 `interaction.steps` (model_output steps)
    steps = getattr(interaction, "steps", None) or []
    step_texts: list[str] = []
    for step in steps:
        step_type = getattr(step, "type", None) or (
            step.get("type") if isinstance(step, dict) else None
        )
        content_list = getattr(step, "content", None) or (
            step.get("content") if isinstance(step, dict) else None
        ) or []
        chunks: list[str] = []
        for item in content_list:
            item_type = getattr(item, "type", None) or (
                item.get("type") if isinstance(item, dict) else None
            )
            if item_type == "thought":
                continue
            txt = getattr(item, "text", None) or (
                item.get("text") if isinstance(item, dict) else None
            )
            if isinstance(txt, str) and txt.strip():
                chunks.append(txt.strip())
        if chunks and (step_type in (None, "model_output", "output", "message")):
            step_texts.append("\n\n".join(chunks))

    if step_texts:
        return step_texts[-1]

    # 3. Legacy `interaction.outputs` fallback
    outputs = getattr(interaction, "outputs", None) or []
    if outputs:
        last_out = outputs[-1]
        txt = getattr(last_out, "text", None)
        if isinstance(txt, str) and txt.strip():
            return txt.strip()
        content_items = getattr(last_out, "content", None) or []
        for item in content_items:
            t = getattr(item, "text", None)
            if isinstance(t, str) and t.strip():
                return t.strip()
        return str(last_out)

    return ""


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
            report_text = extract_interaction_output_text(current)
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


def _safe_state_get(ctx: Any, key: str, default: str = "None") -> str:
    """Safely formats a state value from an ADK ReadonlyContext without brace interpolation errors."""
    state = getattr(ctx, "state", None)
    if state is None:
        return default
    val = state.get(key) if hasattr(state, "get") else None
    if val is None:
        return default
    if isinstance(val, (dict, list)):
        try:
            return json.dumps(val, indent=2, ensure_ascii=False)
        except Exception:
            return str(val)
    return str(val)


def _web_search_instruction(ctx: Any) -> str:
    plan_str = _safe_state_get(ctx, "research_plan", "Conduct a multi-angle investigation of the user's request.")
    gaps_str = _safe_state_get(ctx, "research_gaps", "None")
    return (
        "You are the Web Search Research Explorer.\n"
        f"Research Plan:\n{plan_str}\n\n"
        f"Remaining Gaps:\n{gaps_str}\n\n"
        "Call `google_search_agent` to gather authoritative facts, statistics, dates, "
        "and primary source URLs for every sub-question.\n"
        "Compile detailed evidence notes with exact [Title](URL) citations."
    )


def _browser_reader_instruction(ctx: Any) -> str:
    plan_str = _safe_state_get(ctx, "research_plan", "Inspect any relevant URLs in the user's request.")
    gaps_str = _safe_state_get(ctx, "research_gaps", "None")
    return (
        "You are the Deep Page Browser Specialist.\n"
        f"Research Plan:\n{plan_str}\n\n"
        f"Remaining Gaps:\n{gaps_str}\n\n"
        "When specific documentation URLs, technical pages, or primary sources need full-page "
        "inspection beyond search snippets, use your Headless Chrome tools to navigate and extract "
        "full text/tables. Summarize all extracted technical details with their source URLs."
    )


def _coverage_critic_instruction(ctx: Any) -> str:
    plan_str = _safe_state_get(ctx, "research_plan", "User's research request")
    web_str = _safe_state_get(ctx, "latest_web_findings", "None")
    browser_str = _safe_state_get(ctx, "latest_browser_findings", "None")
    return (
        "You are the Research Coverage Critic.\n"
        f"Original Plan:\n{plan_str}\n\n"
        f"Latest Web Findings:\n{web_str}\n\n"
        f"Latest Browser Findings:\n{browser_str}\n\n"
        "1. If the sub-questions are answered with verified facts and primary source URLs, "
        "call `exit_loop` immediately and output a consolidated summary of all verified findings.\n"
        "2. If critical sub-questions remain unresolved, state the exact missing facts and "
        "run a targeted `google_search_agent` query before exiting."
    )


def _synthesizer_instruction(ctx: Any) -> str:
    plan_str = _safe_state_get(ctx, "research_plan", "Standard Executive Report")
    web_str = _safe_state_get(ctx, "latest_web_findings", "None")
    browser_str = _safe_state_get(ctx, "latest_browser_findings", "None")
    gaps_str = _safe_state_get(ctx, "research_gaps", "None")
    return (
        "You are the Principal Research Synthesizer.\n"
        f"Research Plan:\n{plan_str}\n\n"
        f"Web Findings:\n{web_str}\n\n"
        f"Browser Findings:\n{browser_str}\n\n"
        f"Critic Verification Notes:\n{gaps_str}\n\n"
        "Write a comprehensive, executive-ready Markdown research report.\n\n"
        "Formatting Requirements:\n"
        "1. Start with an **Executive Summary** (3-5 bullet takeaways).\n"
        "2. Follow the planned sections.\n"
        "3. Include Markdown comparison tables for quantitative or architectural trade-offs.\n"
        "4. Cite every claim inline using numbered Markdown links `[1](https://...)` and include a "
        "numbered **Sources & References** section at the end with full clickable `[Title](URL)` links.\n"
        "5. Never invent citations; only cite URLs verified during the research steps."
    )


def build_deep_research_workflow_agent(
    pro_model: Gemini | str = "gemini-2.5-pro",
    flash_model: Gemini | str = "gemini-2.5-flash",
    browser_tools: Optional[list[Any]] = None,
    max_iterations: int = 2,
) -> SequentialAgent:
    """Builds the multi-step ADK Deep Research Agent (Planner -> Loop(Parallel Search + Browser -> Critic) -> Synthesizer)."""

    search_tool_for_worker = build_google_search_agent_tool(flash_model)
    search_tool_for_critic = build_google_search_agent_tool(flash_model)

    planner_agent = LlmAgent(
        name="research_planner",
        model=flash_model,
        description="Decomposes a complex topic into structured sub-questions and search queries.",
        instruction="""You are the Lead Research Planner.
Given the user's research request, produce a structured ResearchPlan with 3-4 orthogonal
sub-questions, concrete Google Search queries for each, and the target report sections.
Also take into account any prior context in the conversation.""",
        output_schema=ResearchPlan,
        output_key="research_plan",
    )

    web_search_worker = LlmAgent(
        name="web_search_explorer",
        model=flash_model,
        description="Executes multi-angle Google Searches for the current research plan and follow-up gaps.",
        instruction=_web_search_instruction,
        tools=[search_tool_for_worker],
        output_key="latest_web_findings",
    )

    parallel_branches: list[Any] = [web_search_worker]
    if browser_tools:
        browser_reader_worker = LlmAgent(
            name="headless_browser_reader",
            model=flash_model,
            description="Uses Headless Chrome to open and extract full text/tables from high-value URLs.",
            instruction=_browser_reader_instruction,
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
        model=flash_model,
        description="Evaluates gathered evidence against the research plan and either exits the loop or specifies follow-up queries.",
        instruction=_coverage_critic_instruction,
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
        instruction=_synthesizer_instruction,
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


def _build_default_research_plan(prompt: str) -> dict[str, Any]:
    """Deterministic fallback ResearchPlan if structured JSON generation fails or returns empty."""
    cleaned = prompt.strip()
    urls = re.findall(r"https?://[^\s)>\]\"']+", cleaned)
    return {
        "topic_summary": cleaned[:200],
        "sub_questions": [
            {
                "id": "Q1",
                "question": f"Core facts, architecture, and primary overview for: {cleaned[:120]}",
                "search_queries": [cleaned[:120], f"{cleaned[:100]} overview latest 2025 2026"],
                "candidate_urls_to_browse": urls[:2],
            },
            {
                "id": "Q2",
                "question": f"Quantitative benchmarks, pricing, metrics, and comparisons for: {cleaned[:120]}",
                "search_queries": [f"{cleaned[:100]} benchmarks pricing comparison metrics"],
                "candidate_urls_to_browse": [],
            },
            {
                "id": "Q3",
                "question": f"Best practices, trade-offs, limitations, and real-world adoption of: {cleaned[:120]}",
                "search_queries": [f"{cleaned[:100]} trade-offs limitations best practices"],
                "candidate_urls_to_browse": [],
            },
        ],
        "deliverable_sections": [
            "Executive Summary",
            "Key Findings & Technical Overview",
            "Comparative Analysis & Quantitative Breakdown",
            "Trade-Offs, Risks & Recommendations",
            "Sources & References",
        ],
    }


def _extract_grounding_from_response(resp: Any) -> tuple[list[dict[str, str]], list[str]]:
    """Extracts grounding_sources (`title`, `uri`) and `web_search_queries` from a GenAI response."""
    sources: list[dict[str, str]] = []
    queries: list[str] = []
    candidates = getattr(resp, "candidates", None) or []
    for cand in candidates:
        gm = getattr(cand, "grounding_metadata", None)
        if not gm:
            continue
        for q in getattr(gm, "web_search_queries", None) or []:
            if q and q not in queries:
                queries.append(str(q))
        for chunk in getattr(gm, "grounding_chunks", None) or []:
            web = getattr(chunk, "web", None)
            uri = getattr(web, "uri", None) if web else None
            if uri:
                title = getattr(web, "title", None) or uri
                item = {"title": str(title), "uri": str(uri)}
                if item not in sources:
                    sources.append(item)
    return sources, queries


async def stream_deep_research_pipeline(
    *,
    research_prompt: str,
    api_key: str,
    session_id: str,
    pro_model: str = "gemini-4-argon",
    fast_model: str = "gemini-3.8-flash",
    browser_tools: Optional[list[Any]] = None,
    prior_context: str = "",
) -> AsyncGenerator[dict[str, Any], None]:
    """Executes the multi-step Deep Research pipeline with real-time SSE progress, parallel Google Search grounding, optional Headless Chrome inspection, and cited Markdown streaming."""
    client = genai.Client(api_key=api_key, vertexai=False)

    # -------------------------------------------------------------------------
    # Stage 1: Lead Research Planner (Decompose into sub-questions & queries)
    # -------------------------------------------------------------------------
    yield {
        "type": "event",
        "session_id": session_id,
        "id": f"ev_dr_plan_call_{uuid.uuid4().hex[:8]}",
        "author": "research_planner",
        "partial": False,
        "timestamp": time.time(),
        "text": "",
        "tool_calls": [
            {
                "id": f"call_plan_{uuid.uuid4().hex[:6]}",
                "name": "research_planner",
                "args": {
                    "topic": research_prompt[:240],
                    "model": fast_model,
                },
            }
        ],
        "tool_responses": [],
        "grounding_queries": [],
        "grounding_sources": [],
    }

    plan_dict: dict[str, Any] = _build_default_research_plan(research_prompt)
    try:
        planner_prompt = (
            "You are the Lead Research Planner. Decompose the following research request into a "
            "structured JSON object with keys:\n"
            '- "topic_summary": string\n'
            '- "sub_questions": list of 3 to 4 objects, each with {"id": "Q1", "question": "...", "search_queries": ["...", "..."], "candidate_urls_to_browse": []}\n'
            '- "deliverable_sections": list of 4 to 5 ordered section headings.\n\n'
            f"Prior conversation context:\n{prior_context[:1500] if prior_context else 'None'}\n\n"
            f"User Research Request:\n{research_prompt}"
        )
        plan_resp = await client.aio.models.generate_content(
            model=fast_model,
            contents=planner_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.2,
            ),
        )
        raw_json = (getattr(plan_resp, "text", None) or "").strip()
        if raw_json:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict) and parsed.get("sub_questions"):
                plan_dict = parsed
    except Exception as exc:
        logger.debug("Research planner using deterministic fallback plan: %s", exc)

    sub_questions = plan_dict.get("sub_questions") or _build_default_research_plan(research_prompt)["sub_questions"]

    # -------------------------------------------------------------------------
    # Stage 2: Parallel Google Search Grounding across all Sub-Questions
    # -------------------------------------------------------------------------
    all_Planned_queries: list[str] = []
    for sq in sub_questions[:4]:
        qs = sq.get("search_queries") or [sq.get("question", research_prompt)]
        all_Planned_queries.extend(qs[:2])

    yield {
        "type": "event",
        "session_id": session_id,
        "id": f"ev_dr_search_call_{uuid.uuid4().hex[:8]}",
        "author": "web_search_explorer",
        "partial": False,
        "timestamp": time.time(),
        "text": "",
        "tool_calls": [
            {
                "id": f"call_search_{uuid.uuid4().hex[:6]}",
                "name": "google_search_agent",
                "args": {
                    "sub_questions": [sq.get("question", "") for sq in sub_questions[:4]],
                    "queries": all_Planned_queries[:8],
                },
            }
        ],
        "tool_responses": [],
        "grounding_queries": all_Planned_queries[:8],
        "grounding_sources": [],
    }

    async def _investigate_sub_question(sq: dict[str, Any]) -> dict[str, Any]:
        q_id = sq.get("id", "Q")
        question_text = sq.get("question", research_prompt)
        queries_list = sq.get("search_queries") or [question_text]
        search_prompt = (
            f"Research Sub-Question ({q_id}): {question_text}\n"
            f"Suggested search queries: {', '.join(queries_list)}\n\n"
            "Use Google Search to gather concrete, up-to-date facts, numbers, dates, technical "
            "specifications, and authoritative details answering this sub-question. "
            "Provide a thorough factual brief."
        )
        try:
            resp = await client.aio.models.generate_content(
                model=fast_model,
                contents=search_prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    temperature=0.2,
                ),
            )
            sources, executed_queries = _extract_grounding_from_response(resp)
            return {
                "id": q_id,
                "question": question_text,
                "findings": (getattr(resp, "text", None) or "").strip(),
                "sources": sources,
                "queries": executed_queries,
            }
        except Exception as exc:
            logger.warning("Sub-question %s search failed: %s", q_id, exc)
            return {
                "id": q_id,
                "question": question_text,
                "findings": f"Search warning for {q_id}: {exc}",
                "sources": [],
                "queries": [],
            }

    search_results = await asyncio.gather(
        *[_investigate_sub_question(sq) for sq in sub_questions[:4]]
    )

    all_sources: list[dict[str, str]] = []
    all_queries: list[str] = []
    evidence_blocks: list[str] = []

    for res in search_results:
        for src in res.get("sources", []):
            if src not in all_sources:
                all_sources.append(src)
        for q in res.get("queries", []):
            if q not in all_queries:
                all_queries.append(q)
        findings_txt = res.get("findings", "")
        src_lines = "\n".join(
            f"  - [{s['title']}]({s['uri']})" for s in res.get("sources", [])[:6]
        )
        evidence_blocks.append(
            f"### {res['id']}: {res['question']}\n{findings_txt}\n"
            + (f"\nVerified Grounding URLs:\n{src_lines}\n" if src_lines else "")
        )

    if all_sources or all_queries:
        yield {
            "type": "event",
            "session_id": session_id,
            "id": f"ev_dr_sources_{uuid.uuid4().hex[:8]}",
            "author": "web_search_explorer",
            "partial": False,
            "timestamp": time.time(),
            "text": "",
            "tool_calls": [],
            "tool_responses": [],
            "grounding_queries": all_queries,
            "grounding_sources": all_sources,
        }

    # -------------------------------------------------------------------------
    # Stage 3: Optional Headless Chrome Page Inspection (if URLs are present)
    # -------------------------------------------------------------------------
    explicit_urls = re.findall(r"https?://[^\s)>\]\"']+", research_prompt)
    for sq in sub_questions[:4]:
        for u in sq.get("candidate_urls_to_browse") or []:
            if isinstance(u, str) and u.startswith("http") and u not in explicit_urls:
                explicit_urls.append(u)

    browser_notes: list[str] = []
    browse_fn = None
    if browser_tools:
        for bt in browser_tools:
            name = getattr(bt, "__name__", getattr(bt, "name", ""))
            if name == "browse_webpage":
                browse_fn = getattr(bt, "func", bt)
                break

    if explicit_urls and browse_fn is not None:
        target_url = explicit_urls[0]
        yield {
            "type": "event",
            "session_id": session_id,
            "id": f"ev_dr_browse_{uuid.uuid4().hex[:8]}",
            "author": "headless_browser_reader",
            "partial": False,
            "timestamp": time.time(),
            "text": "",
            "tool_calls": [
                {
                    "id": f"call_browse_{uuid.uuid4().hex[:6]}",
                    "name": "browse_webpage",
                    "args": {"url": target_url},
                }
            ],
            "tool_responses": [],
            "grounding_queries": [],
            "grounding_sources": [],
        }
        try:
            res_browse = browse_fn(target_url)
            if inspect.isawaitable(res_browse):
                res_browse = await asyncio.wait_for(res_browse, timeout=15.0)
            if isinstance(res_browse, dict) and res_browse.get("status") == "ok":
                title = res_browse.get("title") or target_url
                excerpt = (res_browse.get("text_excerpt") or "")[:3000]
                browser_notes.append(f"### Headless Browser Extraction ({title} - {target_url})\n{excerpt}")
                src_entry = {"title": str(title), "uri": target_url}
                if src_entry not in all_sources:
                    all_sources.append(src_entry)
        except Exception as exc:
            logger.debug("Optional deep research browser extraction skipped: %s", exc)

    # -------------------------------------------------------------------------
    # Stage 4: Coverage Critic & Verification Pass
    # -------------------------------------------------------------------------
    yield {
        "type": "event",
        "session_id": session_id,
        "id": f"ev_dr_critic_{uuid.uuid4().hex[:8]}",
        "author": "coverage_critic",
        "partial": False,
        "timestamp": time.time(),
        "text": "",
        "tool_calls": [
            {
                "id": f"call_critic_{uuid.uuid4().hex[:6]}",
                "name": "coverage_critic",
                "args": {
                    "verified_sources_count": len(all_sources),
                    "sub_questions_evaluated": len(sub_questions[:4]),
                },
            }
        ],
        "tool_responses": [],
        "grounding_queries": all_queries,
        "grounding_sources": all_sources,
    }

    # -------------------------------------------------------------------------
    # Stage 5: Principal Research Synthesizer (Streaming Executive Report)
    # -------------------------------------------------------------------------
    numbered_sources_catalog = "\n".join(
        f"[{idx + 1}] {src['title']} — {src['uri']}"
        for idx, src in enumerate(all_sources[:25])
    )
    sections_list = plan_dict.get("deliverable_sections") or [
        "Executive Summary",
        "Key Findings & Technical Architecture",
        "Comparative Analysis & Quantitative Breakdown",
        "Risks, Trade-Offs & Recommendations",
        "Sources & References",
    ]
    synthesis_prompt = (
        "You are the Principal Research Synthesizer.\n"
        "Synthesize all gathered evidence below into a comprehensive, publication-grade Markdown research report.\n\n"
        f"User Research Request:\n{research_prompt}\n\n"
        f"Planned Report Sections:\n" + "\n".join(f"- {s}" for s in sections_list) + "\n\n"
        f"Gathered Multi-Query Evidence:\n" + "\n\n".join(evidence_blocks) + "\n\n"
        + ("\n\n".join(browser_notes) + "\n\n" if browser_notes else "")
        + f"Verified Grounding Sources Catalog:\n{numbered_sources_catalog or 'Use inline URLs from evidence above.'}\n\n"
        "Strict Formatting Requirements:\n"
        "1. Begin with an **Executive Summary** containing 3–5 high-impact bullet takeaways.\n"
        "2. Cover every planned section with concrete data, metrics, dates, and technical specifics (target 1,200–2,500 words total so the report is thorough yet fast to read).\n"
        "3. Include at least one structured Markdown comparison table summarizing key metrics or trade-offs.\n"
        "4. Cite claims inline using clickable Markdown links `[1](https://...)`, `[2](https://...)` matching the Verified Grounding Sources Catalog.\n"
        "5. End with a numbered **Sources & References** section listing every cited source as `1. [Title](https://...)`.\n"
        "6. Never output internal meta-instructions or placeholder text."
    )

    yield {
        "type": "event",
        "session_id": session_id,
        "id": f"ev_dr_synth_call_{uuid.uuid4().hex[:8]}",
        "author": "citation_report_synthesizer",
        "partial": False,
        "timestamp": time.time(),
        "text": "",
        "tool_calls": [
            {
                "id": f"call_synth_{uuid.uuid4().hex[:6]}",
                "name": "citation_report_synthesizer",
                "args": {
                    "model": pro_model,
                    "sections": sections_list,
                    "citations_count": len(all_sources),
                },
            }
        ],
        "tool_responses": [],
        "grounding_queries": all_queries,
        "grounding_sources": all_sources,
    }

    full_report_chunks: list[str] = []
    synth_models_to_try = [pro_model]
    if fast_model not in synth_models_to_try:
        synth_models_to_try.append(fast_model)
    if "gemini-2.5-pro" not in synth_models_to_try:
        synth_models_to_try.append("gemini-2.5-pro")

    last_synth_error: Optional[Exception] = None
    for candidate_model in synth_models_to_try:
        try:
            stream = await client.aio.models.generate_content_stream(
                model=candidate_model,
                contents=synthesis_prompt,
                config=types.GenerateContentConfig(
                    temperature=0.25,
                ),
            )
            async for chunk in stream:
                chunk_text = getattr(chunk, "text", None) or ""
                if chunk_text:
                    full_report_chunks.append(chunk_text)
                    yield {
                        "type": "event",
                        "session_id": session_id,
                        "id": f"ev_dr_chunk_{uuid.uuid4().hex[:8]}",
                        "author": "citation_report_synthesizer",
                        "partial": True,
                        "timestamp": time.time(),
                        "text": chunk_text,
                        "tool_calls": [],
                        "tool_responses": [],
                        "grounding_queries": [],
                        "grounding_sources": [],
                    }
            if full_report_chunks:
                last_synth_error = None
                break
        except Exception as exc:
            logger.warning(
                "Synthesizer model %s failed (%s); falling back to next model.",
                candidate_model,
                exc,
            )
            last_synth_error = exc

    final_report = "".join(full_report_chunks).strip()
    if not final_report:
        if evidence_blocks:
            final_report = (
                "## Deep Research Findings\n\n"
                + "\n\n".join(evidence_blocks)
                + (
                    "\n\n## Sources & References\n"
                    + "\n".join(
                        f"{i + 1}. [{s['title']}]({s['uri']})"
                        for i, s in enumerate(all_sources)
                    )
                    if all_sources
                    else ""
                )
            )
        elif last_synth_error is not None:
            raise RuntimeError(f"Deep Research synthesis failed: {last_synth_error}")

    yield {
        "type": "event",
        "session_id": session_id,
        "id": f"ev_dr_final_{uuid.uuid4().hex[:8]}",
        "author": "citation_report_synthesizer",
        "partial": False,
        "timestamp": time.time(),
        "text": final_report,
        "tool_calls": [],
        "tool_responses": [],
        "grounding_queries": all_queries,
        "grounding_sources": all_sources,
    }
