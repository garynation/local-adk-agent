"""Persistent async cron/interval task scheduler (/data/schedules.json) for web scraping and ADK automations."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Optional
import uuid

from .config_store import _resolve_data_dir, get_config_manager

logger = logging.getLogger(__name__)


def _parse_cron_field(field_str: str, min_val: int, max_val: int) -> set[int]:
    """Parses a single 5-field cron token (*, */n, a-b, a,b,c) into a set of matching integers."""
    token = field_str.strip()
    matched: set[int] = set()
    for part in token.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            base, step_str = part.split("/", 1)
            step = max(1, int(step_str))
        else:
            base = part

        if base == "*":
            start, end = min_val, max_val
        elif "-" in base:
            s_str, e_str = base.split("-", 1)
            start, end = int(s_str), int(e_str)
        else:
            val = int(base)
            if "/" in part:
                start, end = val, max_val
            else:
                start, end = val, val

        for v in range(max(min_val, start), min(max_val, end) + 1, step):
            matched.add(v)
    return matched


def cron_matches_dt(cron_expr: str, dt: datetime) -> bool:
    """Returns True if a 5-field cron expression (min hour dom month dow) matches `dt`."""
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    try:
        minutes = _parse_cron_field(fields[0], 0, 59)
        hours = _parse_cron_field(fields[1], 0, 23)
        doms = _parse_cron_field(fields[2], 1, 31)
        months = _parse_cron_field(fields[3], 1, 12)
        # Cron dow: 0 or 7 = Sunday, 1 = Monday ... 6 = Saturday
        raw_dows = _parse_cron_field(fields[4], 0, 7)
        dows = {0 if d == 7 else d for d in raw_dows}
        cron_dow = (dt.weekday() + 1) % 7
        return (
            dt.minute in minutes
            and dt.hour in hours
            and dt.day in doms
            and dt.month in months
            and cron_dow in dows
        )
    except Exception:
        return False


def compute_next_run_timestamp(schedule_expr: str, now_ts: Optional[float] = None) -> float:
    """Computes the next UNIX timestamp for either `interval:<seconds>` or a 5-field cron expression."""
    base_ts = now_ts if now_ts is not None else time.time()
    expr = (schedule_expr or "0 8 * * *").strip()

    if expr.startswith("interval:"):
        try:
            seconds = max(60, int(expr.split(":", 1)[1]))
            return base_ts + seconds
        except Exception:
            return base_ts + 3600

    # Preset shorthands
    presets = {
        "every_15m": "*/15 * * * *",
        "every_30m": "*/30 * * * *",
        "every_1h": "0 * * * *",
        "every_6h": "0 */6 * * *",
        "every_12h": "0 */12 * * *",
        "daily_0800": "0 8 * * *",
    }
    cron_str = presets.get(expr.lower(), expr)

    start_dt = datetime.fromtimestamp(base_ts).replace(second=0, microsecond=0) + timedelta(minutes=1)
    # Scan up to 8 days ahead at 1-minute resolution
    for offset in range(0, 60 * 24 * 8):
        candidate = start_dt + timedelta(minutes=offset)
        if cron_matches_dt(cron_str, candidate):
            return candidate.timestamp()
    return base_ts + 3600


class TaskScheduler:
    """Async background task scheduler backed by /data/schedules.json with Headless Chrome concurrency locking."""

    _singleton: Optional[TaskScheduler] = None
    _singleton_lock = threading.RLock()

    def __init__(self, schedules_path: Optional[Path] = None) -> None:
        data_root = _resolve_data_dir()
        self._schedules_path = (
            Path(schedules_path).expanduser()
            if schedules_path is not None
            else (data_root / "schedules.json")
        )
        self._backup_path = self._schedules_path.with_name(f"{self._schedules_path.name}.bak")
        self._exports_dir = self._schedules_path.parent / "exports"
        self._exports_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._scrape_lock = asyncio.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self._loop_task: Optional[asyncio.Task[None]] = None
        self._running = False
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        with self._lock:
            for candidate in (self._schedules_path, self._backup_path):
                if not candidate.exists():
                    continue
                try:
                    raw = json.loads(candidate.read_text(encoding="utf-8"))
                    items = raw.get("schedules", []) if isinstance(raw, dict) else raw
                    if isinstance(items, list):
                        self._tasks = {
                            str(item["id"]): dict(item)
                            for item in items
                            if isinstance(item, dict) and item.get("id")
                        }
                        return
                except Exception as exc:
                    logger.warning("Failed reading %s: %s", candidate, exc)

    def _write_to_disk(self) -> None:
        with self._lock:
            self._schedules_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"schedules": list(self._tasks.values())},
                indent=2,
            )
            if self._schedules_path.exists():
                try:
                    self._backup_path.write_text(
                        self._schedules_path.read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                    os.chmod(self._backup_path, 0o600)
                except OSError:
                    pass

            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._schedules_path.parent),
                prefix=".schedules.",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, self._schedules_path)
                os.chmod(self._schedules_path, 0o600)
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            items = [dict(t) for t in self._tasks.values()]
        items.sort(key=lambda x: float(x.get("created_at", 0)), reverse=True)
        return items

    def get_task(self, task_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            t = self._tasks.get(task_id)
            return dict(t) if t else None

    def create_or_update_task(
        self,
        *,
        name: str,
        prompt: str,
        schedule: str = "0 8 * * *",
        target_url: str = "",
        mode: str = "scrape",
        model: Optional[str] = None,
        enabled: bool = True,
        only_on_change: bool = False,
        notify_gchat: bool = False,
        task_id: Optional[str] = None,
    ) -> dict[str, Any]:
        now = time.time()
        tid = (task_id or "").strip() or f"task_{uuid.uuid4().hex[:8]}"
        clean_schedule = (schedule or "0 8 * * *").strip()
        next_run = compute_next_run_timestamp(clean_schedule, now)

        with self._lock:
            existing = self._tasks.get(tid, {})
            record: dict[str, Any] = {
                "id": tid,
                "name": name.strip() or "Scheduled Web Scrape",
                "prompt": prompt.strip(),
                "target_url": (target_url or "").strip(),
                "schedule": clean_schedule,
                "mode": (mode or "scrape").strip().lower(),
                "model": (model or "").strip() or None,
                "enabled": bool(enabled),
                "only_on_change": bool(only_on_change),
                "notify_gchat": bool(notify_gchat),
                "session_id": existing.get("session_id") or f"sched_{tid}",
                "created_at": existing.get("created_at", now),
                "updated_at": now,
                "next_run_at": next_run,
                "last_run_at": existing.get("last_run_at"),
                "last_status": existing.get("last_status", "scheduled"),
                "last_output": existing.get("last_output", ""),
                "last_screenshot_url": existing.get("last_screenshot_url"),
                "run_count": int(existing.get("run_count", 0)),
            }
            self._tasks[tid] = record
            self._write_to_disk()
            return dict(record)

    def toggle_task(self, task_id: str, enabled: Optional[bool] = None) -> Optional[dict[str, Any]]:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            new_state = (not task.get("enabled", True)) if enabled is None else bool(enabled)
            task["enabled"] = new_state
            task["updated_at"] = time.time()
            if new_state:
                task["next_run_at"] = compute_next_run_timestamp(task.get("schedule", "0 8 * * *"))
            self._write_to_disk()
            return dict(task)

    def delete_task(self, task_id: str) -> bool:
        with self._lock:
            if task_id not in self._tasks:
                return False
            self._tasks.pop(task_id, None)
            self._write_to_disk()
            return True

    async def execute_task(self, task_id: str, triggered_by: str = "schedule") -> dict[str, Any]:
        """Executes a scheduled scrape or research task via AdkRunnerManager and records output."""
        task = self.get_task(task_id)
        if not task:
            raise KeyError(f"Scheduled task '{task_id}' not found")

        from .runner_manager import get_runner_manager

        runner_mgr = get_runner_manager()
        cfg = get_config_manager().config

        # Build effective prompt with Headless Chrome target URL and optional change-detection diff context
        parts: list[str] = [
            f"### Scheduled Automation: {task['name']} (Trigger: {triggered_by})"
        ]
        if task.get("target_url"):
            parts.append(
                f"1. Use the Headless Chrome tool (`browse_webpage`) to navigate to `{task['target_url']}` "
                "with `capture_screenshot=True` and extract the live page content."
            )
        if task.get("prompt"):
            parts.append(task["prompt"])

        prev_output = (task.get("last_output") or "").strip()
        if task.get("only_on_change") and prev_output:
            parts.append(
                "\n### Previous Run Snapshot (For Change Detection)\n"
                "Compare the newly scraped data against this previous snapshot. "
                "If nothing meaningful has changed, begin your response with `[NO_CHANGE]` followed by a 1-line summary. "
                "If changes occurred, highlight the exact differences in a Markdown table:\n"
                f"```markdown\n{prev_output[:2000]}\n```"
            )

        effective_prompt = "\n\n".join(parts)
        is_deep_research = task.get("mode") == "deep_research"
        model_override = task.get("model") or cfg.default_model

        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id]["last_status"] = "running"
                self._write_to_disk()

        async with self._scrape_lock:
            result = await runner_mgr.run_turn_text(
                session_id=task["session_id"],
                user_message=effective_prompt,
                deep_research=is_deep_research,
                model_override=model_override,
                origin_channel="webui",
            )

        now = time.time()
        output_text = (result.get("text") or "").strip()
        status = "ok" if result.get("status") == "ok" else "error"
        changed = not output_text.startswith("[NO_CHANGE]")

        screenshot_info = result.get("browser_screenshot")
        screenshot_url: Optional[str] = None
        if isinstance(screenshot_info, dict):
            screenshot_url = screenshot_info.get("url") or screenshot_info.get("screenshot_url")
        elif isinstance(screenshot_info, str):
            screenshot_url = screenshot_info

        with self._lock:
            if task_id in self._tasks:
                t_ref = self._tasks[task_id]
                t_ref["last_run_at"] = now
                t_ref["next_run_at"] = compute_next_run_timestamp(t_ref.get("schedule", "0 8 * * *"), now)
                t_ref["last_status"] = status
                if output_text:
                    t_ref["last_output"] = output_text
                if screenshot_url:
                    t_ref["last_screenshot_url"] = screenshot_url
                t_ref["run_count"] = int(t_ref.get("run_count", 0)) + 1
                self._write_to_disk()
                updated_task = dict(t_ref)
            else:
                updated_task = task

        # Save latest markdown export to /data/exports/<task_id>.md
        if output_text:
            try:
                export_file = self._exports_dir / f"{task_id}_latest.md"
                export_file.write_text(output_text, encoding="utf-8")
            except OSError:
                pass

        return {
            "status": status,
            "task": updated_task,
            "changed": changed,
            "output": output_text,
            "session_id": task["session_id"],
        }

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._loop_task = asyncio.create_task(self._scheduler_loop())

    async def stop(self) -> None:
        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None

    async def _scheduler_loop(self) -> None:
        while self._running:
            try:
                now = time.time()
                due_ids: list[str] = []
                with self._lock:
                    for tid, task in self._tasks.items():
                        if not task.get("enabled", True):
                            continue
                        next_run = float(task.get("next_run_at") or 0)
                        if next_run <= now:
                            due_ids.append(tid)
                            task["next_run_at"] = compute_next_run_timestamp(
                                task.get("schedule", "0 8 * * *"), now
                            )
                    if due_ids:
                        self._write_to_disk()

                for tid in due_ids:
                    try:
                        await self.execute_task(tid, triggered_by="cron")
                    except Exception as exc:
                        logger.warning("Scheduled task %s failed: %s", tid, exc)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Scheduler loop error: %s", exc)
            await asyncio.sleep(20)


def get_task_scheduler() -> TaskScheduler:
    """Returns the process-wide TaskScheduler singleton, respecting DATA_DIR changes."""
    with TaskScheduler._singleton_lock:
        expected_path = _resolve_data_dir() / "schedules.json"
        if (
            TaskScheduler._singleton is None
            or TaskScheduler._singleton._schedules_path != expected_path
        ):
            TaskScheduler._singleton = TaskScheduler(schedules_path=expected_path)
        return TaskScheduler._singleton


# ---------------------------------------------------------------------------
# Native ADK FunctionTools for Natural-Language Scheduling in Chat / GChat
# ---------------------------------------------------------------------------
def create_scheduled_task(
    name: str,
    prompt: str,
    schedule: str = "0 8 * * *",
    target_url: str = "",
    mode: str = "scrape",
    only_on_change: bool = False,
) -> dict[str, Any]:
    """Creates a recurring scheduled task (such as Headless Chrome web scraping or Deep Research).

    Args:
        name: Human-readable title for the scheduled job (e.g. 'Daily GPU Price Scrape').
        prompt: Instructions for what to extract, compare, or summarize in Markdown.
        schedule: 5-field cron expression (e.g. '0 8 * * *' for 08:00 daily, '*/30 * * * *' for every 30m,
            '0 */6 * * *' for every 6h) or 'interval:<seconds>'.
        target_url: Optional URL to open and scrape with Headless Chrome on each run.
        mode: 'scrape' (Headless Chrome + Gemini), 'deep_research', or 'standard'.
        only_on_change: If True, compares each run against the previous snapshot to highlight changes.
    """
    scheduler = get_task_scheduler()
    created = scheduler.create_or_update_task(
        name=name,
        prompt=prompt,
        schedule=schedule,
        target_url=target_url,
        mode=mode,
        only_on_change=only_on_change,
        enabled=True,
    )
    return {"status": "scheduled", "task": created}


def list_scheduled_tasks() -> dict[str, Any]:
    """Lists all configured scheduled web scraping and research tasks and their latest status."""
    scheduler = get_task_scheduler()
    tasks = scheduler.list_tasks()
    return {"count": len(tasks), "schedules": tasks}


def delete_or_toggle_scheduled_task(task_id: str, action: str = "delete") -> dict[str, Any]:
    """Deletes, pauses, or resumes a scheduled task by ID.

    Args:
        task_id: The ID of the scheduled task (e.g. 'task_a1b2c3d4').
        action: 'delete', 'pause', or 'resume'.
    """
    scheduler = get_task_scheduler()
    act = (action or "delete").strip().lower()
    if act == "delete":
        removed = scheduler.delete_task(task_id)
        return {"status": "deleted" if removed else "not_found", "task_id": task_id}
    enabled = act != "pause"
    updated = scheduler.toggle_task(task_id, enabled=enabled)
    if not updated:
        return {"status": "not_found", "task_id": task_id}
    return {"status": "updated", "task": updated}


def get_scheduler_tools() -> list[Any]:
    """Returns ADK FunctionTool wrappers for natural-language task scheduling."""
    try:
        from google.adk.tools import FunctionTool
    except ImportError:
        from ..agents.deep_research_agent import FunctionTool  # type: ignore[assignment]

    return [
        FunctionTool(func=create_scheduled_task),
        FunctionTool(func=list_scheduled_tasks),
        FunctionTool(func=delete_or_toggle_scheduled_task),
    ]
