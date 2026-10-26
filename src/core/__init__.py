"""Core configuration and ADK Runner management."""

from typing import Any

from .config_store import (
    CHROME_PROFILE_DIR,
    CLIENT_SECRET_PATH,
    CONFIG_PATH,
    DATA_DIR,
    GCHAT_SA_PATH,
    SCREENSHOTS_DIR,
    SECRET_KEY_PATH,
    SESSIONS_DB_PATH,
    WORKSPACE_CLIENT_SECRET_PATH,
    WORKSPACE_TOKEN_PATH,
    AppConfig,
    ConfigManager,
    ConfigStore,
    RuntimeConfig,
    get_config_manager,
    reset_config_manager,
)


def __getattr__(name: str) -> Any:
    if name in {
        "APP_NAME",
        "AdkRunnerManager",
        "get_runner_manager",
        "normalize_async_sqlite_url",
        "reset_runner_manager",
    }:
        from . import runner_manager

        return getattr(runner_manager, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "APP_NAME",
    "AdkRunnerManager",
    "AppConfig",
    "CHROME_PROFILE_DIR",
    "CLIENT_SECRET_PATH",
    "CONFIG_PATH",
    "ConfigManager",
    "ConfigStore",
    "DATA_DIR",
    "GCHAT_SA_PATH",
    "RuntimeConfig",
    "SCREENSHOTS_DIR",
    "SECRET_KEY_PATH",
    "SESSIONS_DB_PATH",
    "WORKSPACE_CLIENT_SECRET_PATH",
    "WORKSPACE_TOKEN_PATH",
    "get_config_manager",
    "get_runner_manager",
    "normalize_async_sqlite_url",
    "reset_config_manager",
    "reset_runner_manager",
]
