"""Playwright runtime discovery and lifecycle helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from . import collection_pause


def locate_browser_executable() -> str:
    configured = os.environ.get("INSTAGRAM_BROWSER_EXECUTABLE", "").strip()
    candidates = [
        configured,
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("Google Chrome was not found. Install Chrome or set INSTAGRAM_BROWSER_EXECUTABLE.")


def load_playwright() -> Callable[[], Any]:
    try:
        import playwright.async_api as playwright_api
        from playwright.async_api import async_playwright
        collection_pause.install_playwright_guards(playwright_api)
    except ImportError as error:
        raise RuntimeError(
            "Python Playwright is required. Install it with: python -m pip install playwright"
        ) from error
    return async_playwright


async def safe_close(value: Any) -> None:
    if value is None:
        return
    try:
        await value.close()
    except Exception:
        pass
