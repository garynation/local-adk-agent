"""Shared Headless Chromium manager and Python Playwright ADK tools."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import shutil
import time
from typing import Any, Callable
import uuid

try:
  from google.adk.tools import FunctionTool
  from google.adk.tools.tool_context import ToolContext
except ImportError:  # pragma: no cover - fallback when google-adk is not installed in host test env

  class ToolContext:  # type: ignore[no-redef]
    def __init__(self, state: dict[str, Any] | None = None) -> None:
      self.state: dict[str, Any] = state if state is not None else {}

  class FunctionTool:  # type: ignore[no-redef]
    def __init__(self, func: Callable[..., Any]) -> None:
      self.func = func
      self.name = getattr(func, "__name__", "function_tool")
      self.description = getattr(func, "__doc__", "") or ""

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
      res = self.func(*args, **kwargs)
      if asyncio.iscoroutine(res):
        return await res
      return res


try:
  from playwright.async_api import BrowserContext
  from playwright.async_api import Playwright
  from playwright.async_api import async_playwright
  PLAYWRIGHT_AVAILABLE = True
except ImportError:  # pragma: no cover - fallback when playwright is not installed in host test env
  PLAYWRIGHT_AVAILABLE = False
  BrowserContext = Any  # type: ignore[assignment,misc]
  Playwright = Any  # type: ignore[assignment,misc]

  def async_playwright() -> Any:  # type: ignore[misc]
    raise RuntimeError(
        "Playwright is not installed in this Python environment. "
        "Run inside the container or install playwright (`pip install playwright && playwright install chromium`)."
    )


logger = logging.getLogger(__name__)


def get_data_dir() -> Path:
  """Resolves the active data directory from DATA_DIR / ADK_DATA_DIR env vars."""
  raw = os.environ.get("DATA_DIR") or os.environ.get("ADK_DATA_DIR") or "/data"
  return Path(raw)


def get_chrome_profile_dir() -> Path:
  """Returns the persistent Chromium user-data-dir path inside DATA_DIR."""
  return get_data_dir() / "chrome-profile"


def get_screenshots_dir() -> Path:
  """Returns the screenshots output directory inside DATA_DIR."""
  return get_data_dir() / "screenshots"


DATA_DIR = get_data_dir()
CHROME_PROFILE_DIR = get_chrome_profile_dir()
SCREENSHOTS_DIR = get_screenshots_dir()
CDP_PORT = int(os.environ.get("CDP_PORT", "9222"))
CDP_ENDPOINT = f"http://127.0.0.1:{CDP_PORT}"

CHROMIUM_ARGS = [
    f"--remote-debugging-port={CDP_PORT}",
    "--remote-debugging-address=127.0.0.1",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-sync",
    "--no-first-run",
    "--window-size=1440,900",
]


def cleanup_stale_singleton_locks(profile_dir: Path | None = None) -> list[str]:
  """Removes stale Chromium SingletonLock files left behind by prior container IDs.

  Args:
    profile_dir: Directory containing the Chromium profile. Defaults to
      `<DATA_DIR>/chrome-profile`.

  Returns:
    List of lock filenames that were found and removed.
  """
  target_dir = profile_dir if profile_dir is not None else get_chrome_profile_dir()
  target_dir.mkdir(parents=True, exist_ok=True)
  removed: list[str] = []
  for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
    lock_path = target_dir / lock_name
    if lock_path.exists() or lock_path.is_symlink():
      lock_path.unlink(missing_ok=True)
      removed.append(lock_name)
  return removed


class BrowserManager:
  """Manages a persistent Headless Chromium instance with a local CDP endpoint."""

  _instance: BrowserManager | None = None
  _lock: asyncio.Lock | None = None

  def __init__(
      self, profile_dir: Path | None = None, screenshots_dir: Path | None = None
  ) -> None:
    self._playwright: Playwright | None = None
    self._context: BrowserContext | None = None
    self._custom_profile_dir = profile_dir
    self._custom_screenshots_dir = screenshots_dir

  @property
  def profile_dir(self) -> Path:
    return (
        self._custom_profile_dir
        if self._custom_profile_dir is not None
        else get_chrome_profile_dir()
    )

  @property
  def screenshots_dir(self) -> Path:
    return (
        self._custom_screenshots_dir
        if self._custom_screenshots_dir is not None
        else get_screenshots_dir()
    )

  @classmethod
  def _get_lock(cls) -> asyncio.Lock:
    if cls._lock is None:
      cls._lock = asyncio.Lock()
    return cls._lock

  @classmethod
  def get_instance(cls) -> BrowserManager:
    if cls._instance is None:
      cls._instance = cls()
    return cls._instance

  @classmethod
  def reset_instance(cls) -> None:
    """Resets the singleton instance (primarily for unit testing)."""
    cls._instance = None
    cls._lock = None

  async def ensure_started(self) -> BrowserContext:
    """Starts the persistent Headless Chromium context if not already running."""
    async with self._get_lock():
      if self._context is not None:
        try:
          await self._context.cookies()
          return self._context
        except Exception:
          logger.warning("Chromium context unhealthy; restarting...")
          await self._shutdown_unlocked()

      profile_dir = self.profile_dir
      screenshots_dir = self.screenshots_dir
      screenshots_dir.mkdir(parents=True, exist_ok=True)
      cleanup_stale_singleton_locks(profile_dir)

      self._playwright = await async_playwright().start()
      self._context = await self._playwright.chromium.launch_persistent_context(
          user_data_dir=str(profile_dir),
          headless=True,
          args=CHROMIUM_ARGS,
          viewport={"width": 1440, "height": 900},
          user_agent=(
              "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
          ),
          accept_downloads=True,
          ignore_https_errors=False,
      )
      logger.info(
          "Headless Chromium started with profile %s and CDP endpoint %s",
          profile_dir,
          CDP_ENDPOINT,
      )
      return self._context

  async def _shutdown_unlocked(self) -> None:
    if self._context is not None:
      try:
        await self._context.close()
      except Exception:
        pass
      self._context = None
    if self._playwright is not None:
      try:
        await self._playwright.stop()
      except Exception:
        pass
      self._playwright = None

  async def shutdown(self) -> None:
    async with self._get_lock():
      await self._shutdown_unlocked()

  async def clear_profile_data(self) -> dict[str, Any]:
    """Clears cookies and storage in the persistent profile."""
    profile_dir = self.profile_dir
    async with self._get_lock():
      if self._context is not None:
        try:
          await self._context.clear_cookies()
        except Exception:
          pass
      await self._shutdown_unlocked()
      if profile_dir.exists():
        for child in profile_dir.iterdir():
          if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
          else:
            child.unlink(missing_ok=True)
      profile_dir.mkdir(parents=True, exist_ok=True)
    return {"status": "cleared", "profile_dir": str(profile_dir)}

  async def get_status(self) -> dict[str, Any]:
    """Returns health, cookie count, and screenshot metadata for /api/browser/status."""
    profile_dir = self.profile_dir
    screenshots_dir = self.screenshots_dir
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    screenshots = sorted(
        screenshots_dir.glob("*.png"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    latest_shot = (
        f"/api/screenshots/{screenshots[0].name}" if screenshots else None
    )

    if self._context is not None:
      try:
        cookies = await self._context.cookies()
        return {
            "status": "ready",
            "running": True,
            "cdp_endpoint": CDP_ENDPOINT,
            "profile_dir": str(profile_dir),
            "profile_exists": profile_dir.exists(),
            "cookie_count": len(cookies),
            "open_pages": len(self._context.pages),
            "screenshot_count": len(screenshots),
            "latest_screenshot": latest_shot,
        }
      except Exception as exc:
        logger.debug("Active context check failed during get_status: %s", exc)

    return {
        "status": "ready",
        "running": False,
        "cdp_endpoint": CDP_ENDPOINT,
        "profile_dir": str(profile_dir),
        "profile_exists": profile_dir.exists(),
        "cookie_count": 0,
        "open_pages": 0,
        "screenshot_count": len(screenshots),
        "latest_screenshot": latest_shot,
    }

  async def test_launch(self, url: str = "https://example.com") -> dict[str, Any]:
    """Launches or reuses Headless Chromium to load a test URL and capture a screenshot."""
    t0 = time.perf_counter()
    res = await browse_webpage(url=url, capture_screenshot=True)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    if res.get("status") == "success":
      return {
          "status": "ok",
          "page_title": res.get("title", ""),
          "url": res.get("url", url),
          "elapsed_ms": elapsed_ms,
          "screenshot_filename": res.get("screenshot_filename"),
          "screenshot_url": res.get("screenshot_url"),
      }
    return {
        "status": "error",
        "url": url,
        "elapsed_ms": elapsed_ms,
        "error": res.get("error", "Unknown browser error"),
    }


async def browse_webpage(
    url: str,
    wait_for_selector: str | None = None,
    capture_screenshot: bool = True,
    full_page_screenshot: bool = False,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
  """Navigates Headless Chrome to a URL, extracts readable text & links, and saves a screenshot.

  Args:
    url: Full HTTP/HTTPS URL to open in Headless Chrome.
    wait_for_selector: Optional CSS selector to wait for before extracting content.
    capture_screenshot: Whether to capture a PNG screenshot to <DATA_DIR>/screenshots/.
    full_page_screenshot: If True, captures the full scrollable page height.
    tool_context: Optional ADK ToolContext used to store screenshot metadata in session state.

  Returns:
    Dictionary containing page title, final URL, extracted text, top links,
    screenshot filename, and Web UI screenshot URL (`/api/screenshots/<file>`).
  """
  manager = BrowserManager.get_instance()
  try:
    context = await manager.ensure_started()
  except Exception as exc:
    return {"status": "error", "url": url, "error": str(exc)}

  page = await context.new_page()
  try:
    response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
      await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
      pass

    if wait_for_selector:
      await page.wait_for_selector(wait_for_selector, timeout=10000)

    title = await page.title()
    final_url = page.url

    extracted = await page.evaluate(
        """() => {
          const clone = document.body ? document.body.cloneNode(true) : null;
          if (!clone) return { text: "", links: [] };
          clone.querySelectorAll('script, style, noscript, svg').forEach(el => el.remove());
          const text = (clone.innerText || "").replace(/\\n{3,}/g, '\\n\\n').trim();
          const links = Array.from(document.querySelectorAll('a[href]'))
            .map(a => ({ text: (a.innerText || '').trim(), href: a.href }))
            .filter(l => l.text && l.href.startsWith('http'))
            .slice(0, 40);
          return { text: text.slice(0, 25000), links };
        }"""
    )

    screenshot_filename = None
    screenshot_url = None
    screenshot_path_str = None

    if capture_screenshot:
      screenshots_dir = manager.screenshots_dir
      screenshots_dir.mkdir(parents=True, exist_ok=True)
      ts = int(time.time() * 1000)
      short_id = uuid.uuid4().hex[:8]
      screenshot_filename = f"shot_{ts}_{short_id}.png"
      screenshot_path = screenshots_dir / screenshot_filename
      await page.screenshot(
          path=str(screenshot_path), full_page=full_page_screenshot, type="png"
      )
      screenshot_url = f"/api/screenshots/{screenshot_filename}"
      screenshot_path_str = str(screenshot_path)

      if tool_context is not None and hasattr(tool_context, "state"):
        tool_context.state["last_browser_screenshot"] = {
            "filename": screenshot_filename,
            "url": screenshot_url,
            "path": screenshot_path_str,
            "page_title": title,
            "page_url": final_url,
        }

    return {
        "status": "success",
        "http_status": response.status if response else None,
        "title": title,
        "url": final_url,
        "content": extracted["text"],
        "links": extracted["links"],
        "screenshot_filename": screenshot_filename,
        "screenshot_url": screenshot_url,
        "screenshot_path": screenshot_path_str,
    }
  except Exception as exc:
    return {"status": "error", "url": url, "error": str(exc)}
  finally:
    await page.close()


def get_python_browser_tools() -> list[FunctionTool]:
  """Returns the native Python Playwright FunctionTools for the ADK agent."""
  return [FunctionTool(func=browse_webpage)]
