"""Playwright runtime discovery and lifecycle helpers."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Callable

from . import collection_pause


def locate_browser_executable() -> str:
    configured = os.environ.get("INSTAGRAM_BROWSER_EXECUTABLE", "").strip()
    if configured:
        executable = Path(os.path.expandvars(configured.strip('"'))).expanduser()
        if executable.is_file():
            return str(executable)
        raise RuntimeError(f"INSTAGRAM_BROWSER_EXECUTABLE does not point to a browser executable: {executable}")
    roots = [
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    candidates = []
    for relative, name in (
        ("Microsoft/Edge/Application/msedge.exe", "msedge"),
        ("Google/Chrome/Application/chrome.exe", "chrome"),
    ):
        candidates.extend(str(Path(root) / relative) for root in roots if root)
        candidates.append(shutil.which(name))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("Edge or Chrome was not found. Install a browser or set INSTAGRAM_BROWSER_EXECUTABLE to its executable path.")


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
