"""Shared web/hybrid collector-mode parsing."""

from __future__ import annotations

from typing import Sequence


COLLECTOR_MODES = ("hybrid", "web")
MODE_COMMANDS = frozenset({"collect", "refresh", "fashion", "beauty", "fashion-beauty"})


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
            raise ValueError("--android-only is no longer supported. Choose --collector-mode web or hybrid.")
        elif argument == "--collector-mode":
            if index + 1 >= len(arguments):
                raise ValueError("--collector-mode requires one of: hybrid, web")
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
