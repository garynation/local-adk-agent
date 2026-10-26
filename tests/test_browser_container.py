"""Unit tests for Headless Chromium container tools, lock cleanup, and secret hygiene."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

try:
  import pytest
except ImportError:  # pragma: no cover
  pytest = None  # type: ignore[assignment]

from src.tools.browser_mcp import create_playwright_mcp_toolset
from src.tools.browser_mcp import create_standalone_playwright_mcp_toolset
from src.tools.browser_tools import BrowserManager
from src.tools.browser_tools import browse_webpage
from src.tools.browser_tools import cleanup_stale_singleton_locks
from src.tools.browser_tools import get_chrome_profile_dir
from src.tools.browser_tools import get_data_dir
from src.tools.browser_tools import get_python_browser_tools
from src.tools.browser_tools import get_screenshots_dir

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_data_dir_env_resolution(tmp_path: Path) -> None:
  old_data_dir = os.environ.get("DATA_DIR")
  try:
    os.environ["DATA_DIR"] = str(tmp_path)
    assert get_data_dir() == tmp_path
    assert get_chrome_profile_dir() == tmp_path / "chrome-profile"
    assert get_screenshots_dir() == tmp_path / "screenshots"
  finally:
    if old_data_dir is not None:
      os.environ["DATA_DIR"] = old_data_dir
    else:
      os.environ.pop("DATA_DIR", None)


def _run_cleanup_stale_singleton_locks(tmp_path: Path) -> None:
  profile_dir = tmp_path / "chrome-profile"
  profile_dir.mkdir(parents=True, exist_ok=True)

  # Create a dangling symlink (simulating Chromium's hostname-pid SingletonLock)
  lock_symlink = profile_dir / "SingletonLock"
  lock_symlink.symlink_to("oldcontainerid-12345")

  # Create regular files for SingletonCookie and SingletonSocket
  (profile_dir / "SingletonCookie").write_text("cookie-lock", encoding="utf-8")
  (profile_dir / "SingletonSocket").write_text("socket-lock", encoding="utf-8")

  # Create a legitimate profile file that must NOT be deleted
  cookies_db = profile_dir / "Cookies"
  cookies_db.write_text("sqlite-cookies", encoding="utf-8")

  removed = cleanup_stale_singleton_locks(profile_dir)

  assert set(removed) == {"SingletonLock", "SingletonCookie", "SingletonSocket"}
  assert not lock_symlink.exists() and not lock_symlink.is_symlink()
  assert not (profile_dir / "SingletonCookie").exists()
  assert not (profile_dir / "SingletonSocket").exists()
  assert cookies_db.exists()


async def _run_browser_manager_singleton_and_status(tmp_path: Path) -> None:
  old_data_dir = os.environ.get("DATA_DIR")
  try:
    os.environ["DATA_DIR"] = str(tmp_path)
    BrowserManager.reset_instance()

    mgr1 = BrowserManager.get_instance()
    mgr2 = BrowserManager.get_instance()
    assert mgr1 is mgr2
    assert mgr1.profile_dir == tmp_path / "chrome-profile"
    assert mgr1.screenshots_dir == tmp_path / "screenshots"

    # Create a dummy screenshot to test status discovery
    mgr1.screenshots_dir.mkdir(parents=True, exist_ok=True)
    dummy_shot = mgr1.screenshots_dir / "shot_1000_abcd1234.png"
    dummy_shot.write_bytes(b"\x89PNG\r\n\x1a\n")

    status = await mgr1.get_status()
    assert status["status"] == "ready"
    assert status["running"] is False
    assert status["screenshot_count"] == 1
    assert status["latest_screenshot"] == "/api/screenshots/shot_1000_abcd1234.png"

    # Test clear_profile_data removes files inside profile_dir
    mgr1.profile_dir.mkdir(parents=True, exist_ok=True)
    (mgr1.profile_dir / "Default").mkdir(parents=True, exist_ok=True)
    (mgr1.profile_dir / "Default" / "Preferences").write_text("{}", encoding="utf-8")
    (mgr1.profile_dir / "Local State").write_text("{}", encoding="utf-8")

    cleared = await mgr1.clear_profile_data()
    assert cleared["status"] == "cleared"
    assert mgr1.profile_dir.exists()
    assert list(mgr1.profile_dir.iterdir()) == []
  finally:
    BrowserManager.reset_instance()
    if old_data_dir is not None:
      os.environ["DATA_DIR"] = old_data_dir
    else:
      os.environ.pop("DATA_DIR", None)


async def _run_browse_webpage_with_mocked_playwright(tmp_path: Path) -> None:
  old_data_dir = os.environ.get("DATA_DIR")
  try:
    os.environ["DATA_DIR"] = str(tmp_path)
    BrowserManager.reset_instance()

    mock_page = AsyncMock()
    mock_response = MagicMock()
    mock_response.status = 200
    mock_page.goto.return_value = mock_response
    mock_page.title.return_value = "Example Domain"
    mock_page.url = "https://example.com/"
    mock_page.evaluate.return_value = {
        "text": "Example Domain body content.",
        "links": [
            {
                "text": "More information...",
                "href": "https://www.iana.org/domains/example",
            }
        ],
    }

    async def fake_screenshot(path: str, **kwargs: object) -> bytes:
      Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")
      return b"\x89PNG\r\n\x1a\n"

    mock_page.screenshot.side_effect = fake_screenshot

    mock_context = AsyncMock()
    mock_context.new_page.return_value = mock_page
    mock_context.cookies.return_value = [{"name": "sid", "value": "123"}]
    mock_context.pages = [mock_page]

    mgr = BrowserManager.get_instance()
    with patch.object(mgr, "ensure_started", AsyncMock(return_value=mock_context)):
      dummy_tool_ctx = MagicMock()
      dummy_tool_ctx.state = {}

      result = await browse_webpage(
          url="https://example.com",
          capture_screenshot=True,
          tool_context=dummy_tool_ctx,
      )

    assert result["status"] == "success"
    assert result["http_status"] == 200
    assert result["title"] == "Example Domain"
    assert result["content"] == "Example Domain body content."
    assert result["screenshot_url"].startswith("/api/screenshots/shot_")
    assert Path(result["screenshot_path"]).exists()
    assert "last_browser_screenshot" in dummy_tool_ctx.state
    assert dummy_tool_ctx.state["last_browser_screenshot"]["page_title"] == "Example Domain"

    tools = get_python_browser_tools()
    assert len(tools) == 1
  finally:
    BrowserManager.reset_instance()
    if old_data_dir is not None:
      os.environ["DATA_DIR"] = old_data_dir
    else:
      os.environ.pop("DATA_DIR", None)


def _run_playwright_mcp_toolsets_creation(tmp_path: Path) -> None:
  old_data_dir = os.environ.get("DATA_DIR")
  try:
    os.environ["DATA_DIR"] = str(tmp_path)
    shared_toolset = create_playwright_mcp_toolset()
    standalone_toolset = create_standalone_playwright_mcp_toolset()
    assert shared_toolset is not None
    assert standalone_toolset is not None
  finally:
    if old_data_dir is not None:
      os.environ["DATA_DIR"] = old_data_dir
    else:
      os.environ.pop("DATA_DIR", None)


def test_repository_secret_hygiene_and_packaging_files() -> None:
  """Verifies required container files exist and zero secrets are present in repository files."""
  required_files = [
      "Dockerfile",
      "docker-compose.yml",
      "entrypoint.sh",
      "requirements.txt",
      "pyproject.toml",
      ".env.example",
      ".dockerignore",
      ".gitignore",
      "README.md",
  ]
  for rel_path in required_files:
    assert (REPO_ROOT / rel_path).exists(), f"Missing required file: {rel_path}"

  workflow_path = REPO_ROOT / ".github/workflows/docker-publish.yml"
  if not workflow_path.exists():
    workflow_path = REPO_ROOT / "ci/docker-publish.yml"
  assert workflow_path.exists(), "Missing docker-publish.yml workflow file"

  # Verify README opens with ### Summary
  readme_text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
  assert "### Summary" in readme_text

  # Verify GitHub workflow targets linux/amd64 and includes secret-hygiene-check
  workflow_text = workflow_path.read_text(
      encoding="utf-8"
  )
  assert "secret-hygiene-check:" in workflow_text
  assert "platforms: linux/amd64" in workflow_text

  # Verify no forbidden credential files exist in the repo root or data/
  forbidden_names = {
      "config.json",
      ".secret.key",
      "client_secret.json",
      "workspace_client_secret.json",
      "workspace_token.json",
      "gchat_service_account.json",
      ".env",
  }
  for path in REPO_ROOT.rglob("*"):
    if (
        ".git" in path.parts
        or "__pycache__" in path.parts
        or ".pytest_cache" in path.parts
    ):
      continue
    if path.is_file():
      assert path.name not in forbidden_names, f"Forbidden secret file found: {path}"
      if path.parent != REPO_ROOT / "tests" and path.name != "docker-publish.yml":
        content = path.read_text(encoding="utf-8", errors="ignore")
        assert not re.search(
            r"AIza[0-9A-Za-z_-]{35}", content
        ), f"API key found in {path}"
        assert (
            "-----BEGIN PRIVATE KEY-----" not in content
        ), f"Private key found in {path}"
        assert not re.search(
            r"GOCSPX-[0-9A-Za-z_-]{28}", content
        ), f"OAuth secret in {path}"


class TestBrowserContainer(unittest.IsolatedAsyncioTestCase):
  """Unittest & pytest compatible test suite for Component 1."""

  def test_data_dir_env_resolution(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      _run_data_dir_env_resolution(Path(tmp))

  def test_cleanup_stale_singleton_locks(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      _run_cleanup_stale_singleton_locks(Path(tmp))

  async def test_browser_manager_singleton_and_status(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      await _run_browser_manager_singleton_and_status(Path(tmp))

  async def test_browse_webpage_with_mocked_playwright(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      await _run_browse_webpage_with_mocked_playwright(Path(tmp))

  def test_playwright_mcp_toolsets_creation(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      _run_playwright_mcp_toolsets_creation(Path(tmp))

  def test_secret_hygiene(self) -> None:
    test_repository_secret_hygiene_and_packaging_files()


if __name__ == "__main__":
  unittest.main()
