"""ADK Toolsets for Personal Google Workspace and Headless Chrome."""

from .workspace_tools import (
    build_workspace_toolset,
    calendar_create_event,
    calendar_list_events,
    docs_create_document,
    docs_read_document,
    drive_search_files,
    get_workspace_tools,
    gmail_create_draft,
    gmail_read_thread,
    gmail_search_messages,
    gmail_send_email,
)

try:
    from .browser_tools import (
        BrowserManager,
        browse_webpage,
        cleanup_stale_singleton_locks,
        get_python_browser_tools,
    )
    from .browser_mcp import (
        create_playwright_mcp_toolset,
        create_standalone_playwright_mcp_toolset,
    )
except ImportError:  # pragma: no cover
    pass

__all__ = [
    "BrowserManager",
    "browse_webpage",
    "build_workspace_toolset",
    "calendar_create_event",
    "calendar_list_events",
    "cleanup_stale_singleton_locks",
    "create_playwright_mcp_toolset",
    "create_standalone_playwright_mcp_toolset",
    "docs_create_document",
    "docs_read_document",
    "drive_search_files",
    "get_python_browser_tools",
    "get_workspace_tools",
    "gmail_create_draft",
    "gmail_read_thread",
    "gmail_search_messages",
    "gmail_send_email",
]
