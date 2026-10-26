"""ADK McpToolset factories for @playwright/mcp over shared or standalone Headless Chromium."""

from __future__ import annotations

import os
import shutil
from typing import Any

try:
  from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
  from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
  from mcp import StdioServerParameters
except ImportError:  # pragma: no cover - fallback when google-adk or mcp is not installed in host test env

  class StdioServerParameters:  # type: ignore[no-redef]
    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
      self.command = command
      self.args = list(args or [])
      self.env = dict(env or {})

  class StdioConnectionParams:  # type: ignore[no-redef]
    def __init__(
        self, server_params: StdioServerParameters, timeout: float = 60.0
    ) -> None:
      self.server_params = server_params
      self.timeout = timeout

  class McpToolset:  # type: ignore[no-redef]
    def __init__(
        self,
        connection_params: StdioConnectionParams,
        tool_name_prefix: str = "",
        tool_list_cache_ttl_seconds: float = 300.0,
        **kwargs: Any,
    ) -> None:
      self.connection_params = connection_params
      self.tool_name_prefix = tool_name_prefix
      self.tool_list_cache_ttl_seconds = tool_list_cache_ttl_seconds
      self.kwargs = kwargs


from .browser_tools import CDP_ENDPOINT
from .browser_tools import get_chrome_profile_dir
from .browser_tools import get_data_dir
from .browser_tools import get_screenshots_dir


def create_playwright_mcp_toolset(cdp_endpoint: str = CDP_ENDPOINT) -> McpToolset:
  """Creates the ADK McpToolset driving Headless Chromium via @playwright/mcp over CDP.

  Uses the globally pre-installed `playwright-mcp` binary inside the Docker image
  (falling back to `npx --no-install @playwright/mcp`) and connects over CDP to the
  persistent Chromium context rooted at `<DATA_DIR>/chrome-profile`.
  """
  data_dir = str(get_data_dir())
  screenshots_dir = str(get_screenshots_dir())
  browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/ms-playwright")

  if shutil.which("playwright-mcp"):
    command = "playwright-mcp"
    args = [
        f"--cdp-endpoint={cdp_endpoint}",
        f"--output-dir={screenshots_dir}",
    ]
  else:
    command = "npx"
    args = [
        "--no-install",
        "@playwright/mcp",
        f"--cdp-endpoint={cdp_endpoint}",
        f"--output-dir={screenshots_dir}",
    ]

  return McpToolset(
      connection_params=StdioConnectionParams(
          server_params=StdioServerParameters(
              command=command,
              args=args,
              env={
                  "HOME": data_dir,
                  "PLAYWRIGHT_BROWSERS_PATH": browsers_path,
              },
          ),
          timeout=60.0,
      ),
      tool_name_prefix="playwright_",
      tool_list_cache_ttl_seconds=300.0,
  )


def create_standalone_playwright_mcp_toolset() -> McpToolset:
  """Alternative standalone launcher if @playwright/mcp manages Chromium directly."""
  data_dir = str(get_data_dir())
  profile_dir = str(get_chrome_profile_dir())
  screenshots_dir = str(get_screenshots_dir())
  browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/ms-playwright")

  return McpToolset(
      connection_params=StdioConnectionParams(
          server_params=StdioServerParameters(
              command="npx",
              args=[
                  "--no-install",
                  "@playwright/mcp",
                  "--browser=chromium",
                  "--headless",
                  "--no-sandbox",
                  f"--user-data-dir={profile_dir}",
                  f"--output-dir={screenshots_dir}",
              ],
              env={
                  "HOME": data_dir,
                  "PLAYWRIGHT_BROWSERS_PATH": browsers_path,
              },
          ),
          timeout=60.0,
      ),
      tool_name_prefix="playwright_",
      tool_list_cache_ttl_seconds=300.0,
  )
