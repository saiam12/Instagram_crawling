"""Shared collector-mode parsing and Android compatibility dispatch."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Sequence


COLLECTOR_MODES = ("hybrid", "web", "android")
MODE_COMMANDS = frozenset({"collect", "refresh", "fashion", "beauty", "fashion-beauty"})
UNSUPPORTED_ANDROID_OPTIONS = frozenset({
    "--android-idle-hashtag-query",
    "--android-metrics",
    "--android-metrics-required",
    "--collect-hashtag-media-count",
    "--disable-recollect-cooldown",
    "--direct-reel-info-wait-seconds",
    "--exact-metric-attempts",
    "--exact-metric-retry-delay-seconds",
    "--followers-after-reels",
    "--followers-only",
    "--hashtag-candidates-per-keyword",
    "--max-upload-age-days",
    "--maxdays",
    "--new-urls-only",
    "--no-android-metrics",
    "--no-login",
    "--profile-dir",
    "--urls-file",
})

UNIFIED_ANDROID_OPTION_ALIASES = {
    "--android-adb-path": "--adb-path",
    "--android-device-id": "--device-id",
    "--android-ui-delay-seconds": "--interval-seconds",
}

UNIFIED_ROOT = Path(__file__).resolve().parent.parent
ANDROID_ROOT = UNIFIED_ROOT.parent / "android"


def _option_name(argument: str) -> str:
    return argument.split("=", 1)[0]


def extract_collector_mode(arguments: Sequence[str]) -> tuple[str, list[str]]:
    """Remove the mode selector and return ``(mode, remaining_arguments)``."""
    mode = "hybrid"
    explicit_mode = False
    remaining: list[str] = []
    index = 0
    while index < len(arguments):
        argument = str(arguments[index])
        requested: str | None = None
        consumes_next = False
        if argument == "--android-only":
            requested = "android"
        elif argument == "--collector-mode":
            if index + 1 >= len(arguments):
                raise ValueError("--collector-mode requires one of: hybrid, web, android")
            requested = str(arguments[index + 1]).strip().lower()
            consumes_next = True
        elif argument.startswith("--collector-mode="):
            requested = argument.split("=", 1)[1].strip().lower()
        elif argument == "--no-android-metrics":
            requested = "web"
        elif argument == "--android-metrics":
            requested = "hybrid"

        if requested is None:
            remaining.append(argument)
            index += 1
            continue
        if requested not in COLLECTOR_MODES:
            raise ValueError(
                f"Unknown collector mode: {requested}. Choose one of: {', '.join(COLLECTOR_MODES)}"
            )
        if explicit_mode and requested != mode:
            raise ValueError(f"Conflicting collector modes: {mode} and {requested}")
        mode = requested
        explicit_mode = True
        index += 2 if consumes_next else 1
    return mode, remaining


def translate_android_arguments(arguments: Sequence[str]) -> list[str]:
    """Translate unified Android option names to the Android collector CLI."""
    translated: list[str] = []
    for argument in arguments:
        option = _option_name(str(argument))
        if option in UNSUPPORTED_ANDROID_OPTIONS:
            raise ValueError(
                f"{option} is only supported by hybrid/web collection and cannot be used with --collector-mode android"
            )
        alias = UNIFIED_ANDROID_OPTION_ALIASES.get(option)
        if alias is None:
            translated.append(str(argument))
        elif "=" in str(argument):
            translated.append(f"{alias}={str(argument).split('=', 1)[1]}")
        else:
            translated.append(alias)
    if not any(_option_name(argument) == "--data-dir" for argument in translated):
        translated.extend(("--data-dir", str(UNIFIED_ROOT / "data_web")))
    return translated


def run_android_collection(command: str, arguments: Sequence[str]) -> int:
    """Run the legacy Android workflow through the unified entry point.

    The Android package remains the execution backend for now. Keeping this
    adapter small lets the standalone directory be removed after parity is
    verified without changing the unified command surface again.
    """
    if command == "followers":
        raise ValueError("Android mode does not support the followers command")
    if command not in MODE_COMMANDS:
        raise ValueError(f"--collector-mode android is not supported for the {command} command")
    if not ANDROID_ROOT.is_dir():
        raise RuntimeError(f"Android collector backend was not found: {ANDROID_ROOT}")

    translated = translate_android_arguments(arguments)
    sys.path.insert(0, str(ANDROID_ROOT))
    try:
        launcher = importlib.import_module("collect_android_reels")
        return int(launcher.main([command, *translated]))
    finally:
        sys.path.remove(str(ANDROID_ROOT))

