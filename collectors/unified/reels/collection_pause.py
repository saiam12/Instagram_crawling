"""Manual pause shared by the unified browser and its Android workers."""
from __future__ import annotations

import asyncio
import functools
import inspect
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

PAUSE_FILE = Path(__file__).resolve().parents[1] / ".collection-paused"
_input_lock = threading.Lock()
_installed = False


def is_paused() -> bool:
    return PAUSE_FILE.exists()


def pause() -> None:
    if not is_paused():
        PAUSE_FILE.touch()
        print("\n[PAUSED] HTTP 429: 전체 수집 일시정지. 후속조치 후 resume + Enter로 재개합니다.", flush=True)


def resume(command: str) -> bool:
    if command.strip().casefold() != "resume":
        return False
    PAUSE_FILE.unlink(missing_ok=True)
    print("[RESUMED] 수집을 이어서 진행합니다.", flush=True)
    return True


def _rate_limit_state_for_target(target):
    """Return the browser rate-limit state attached to a Playwright target.

    Playwright methods are wrapped at several levels (Page, Locator, Frame,
    and Context).  The collector attaches its shared state to the Page and
    Context, so look through the small set of owner links exposed by those
    objects without importing the browser collector back into this module.
    """
    pending = [target]
    visited = set()
    while pending:
        candidate = pending.pop(0)
        if candidate is None or id(candidate) in visited:
            continue
        visited.add(id(candidate))
        try:
            state = getattr(candidate, "_instagram_collector_rate_limit_state", None)
        except Exception:
            state = None
        if state is not None:
            return state
        for attribute in (
            "request",
            "page",
            "_page",
            "frame",
            "_frame",
            "context",
            "_context",
        ):
            try:
                owner = getattr(candidate, attribute, None)
            except Exception:
                owner = None
            if owner is not None and not callable(owner):
                pending.append(owner)
    return None


def _notify_rate_limit_resume(target) -> None:
    state = _rate_limit_state_for_target(target)
    if state is None:
        return
    mark_resumed = getattr(state, "mark_resumed", None)
    if callable(mark_resumed):
        mark_resumed()


def _raise_if_terminal_rate_limit(target) -> None:
    state = _rate_limit_state_for_target(target)
    if state is None or not getattr(state, "terminate_after_resume", False):
        return
    raise_if_limited = getattr(state, "raise_if_limited", None)
    if callable(raise_if_limited):
        raise_if_limited()


def wait_sync(*, terminal: bool = False) -> None:
    while is_paused():
        if terminal and _input_lock.acquire(blocking=False):
            try:
                if not is_paused():
                    return
                command = sys.stdin.readline()
                if not command:
                    # Detached workers cannot authorize resumption.
                    terminal = False
                elif not resume(command):
                    print("일시정지 상태입니다. 재개하려면 resume을 입력하세요.", flush=True)
            finally:
                _input_lock.release()
        else:
            time.sleep(0.1)


async def wait() -> None:
    if is_paused():
        await asyncio.to_thread(wait_sync, terminal=True)


def is_instagram_limit(response) -> bool:
    host = urlparse(str(getattr(response, "url", ""))).hostname or ""
    return (host == "instagram.com" or host.endswith(".instagram.com")) and getattr(response, "status", None) == 429


def observe(response) -> None:
    if is_instagram_limit(response):
        state = _rate_limit_state_for_target(response)
        # A second 429 after the operator has resumed is terminal.  Leave no
        # pause marker behind, otherwise a subsequent process would inherit a
        # stale pause and wait for an unnecessary resume.
        if state is not None and (
            getattr(state, "resume_count", 0) > 0
            or getattr(state, "terminate_after_resume", False)
            or (
                getattr(state, "pause_seen", False)
                and not is_paused()
            )
        ):
            return
        pause()


async def install_context(context) -> None:
    if getattr(context, "_manual_pause_installed", False):
        return
    context._manual_pause_installed = True
    context.on("response", observe)

    async def route_request(route):
        # Do not hold routed requests until their navigation times out.
        if is_paused():
            await route.abort()
        else:
            await route.continue_()

    await context.route("**/*", route_request)


def guarded(method):
    @functools.wraps(method)
    async def call(*args, **kwargs):
        target = args[0] if args else None
        while True:
            await wait()
            _raise_if_terminal_rate_limit(target)
            try:
                result = await method(*args, **kwargs)
            except Exception:
                _raise_if_terminal_rate_limit(target)
                if not is_paused():
                    raise
                await wait()
                _notify_rate_limit_resume(target)
                _raise_if_terminal_rate_limit(target)
                # Resume the interrupted browser operation in the same session.
                continue
            if method.__name__ in {"new_context", "launch_persistent_context"}:
                await install_context(result)
            limited_navigation = method.__name__ in {"goto", "reload"} and is_instagram_limit(result)
            if limited_navigation:
                observe(result)
            _raise_if_terminal_rate_limit(target)
            await wait()
            _notify_rate_limit_resume(target)
            _raise_if_terminal_rate_limit(target)
            if limited_navigation:
                continue
            return result
    return call


def install_playwright_guards(api) -> None:
    """Gate public browser actions, including locators and keyboard/mouse input."""
    global _installed
    if _installed:
        return
    for name in ("BrowserType", "Browser", "BrowserContext", "Page", "Frame", "Locator", "ElementHandle", "Keyboard", "Mouse", "Touchscreen"):
        cls = getattr(api, name)
        for key, method in list(vars(cls).items()):
            if not key.startswith("_") and inspect.iscoroutinefunction(method):
                # Context routing and response listeners must stay operational.
                if key in {"route", "unroute", "unroute_all"}:
                    continue
                setattr(cls, key, guarded(method))
    _installed = True


async def wait_for_active_time(awaitable, timeout: float):
    """A scheduler deadline cannot cancel/close a manually paused browser."""
    task = asyncio.ensure_future(awaitable)
    remaining = timeout
    try:
        while not task.done():
            await wait()
            started = time.monotonic()
            done, _ = await asyncio.wait({task}, timeout=min(0.1, max(0, remaining)))
            if done:
                return task.result()
            if not is_paused():
                remaining -= time.monotonic() - started
            if remaining <= 0:
                raise TimeoutError()
        return task.result()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
