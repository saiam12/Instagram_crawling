"""Shared comparison and infrastructure-error classification."""
from __future__ import annotations

from typing import Any
from .collection_settings import PYTHON_ANDROID_COMPARISON_COUNT_FIELDS
from .reel_records import exact_nonnegative_integer


def compare_python_and_android_counts(
    python_metrics: dict[str, Any] | None,
    android_metrics: dict[str, Any] | None,
) -> dict[str, dict[str, float | int]]:
    """Return count fields that exceed the tolerated cross-surface difference.

    Only exact integer values observed by both collectors are comparable.
    Differences up to 300 pass. Larger differences allow 30% up to 1,000,
    10% up to 5,000, and 5% above 5,000, using the larger count as the base.
    """
    browser = python_metrics or {}
    android = android_metrics or {}
    mismatches: dict[str, dict[str, float | int]] = {}
    for field in PYTHON_ANDROID_COMPARISON_COUNT_FIELDS:
        python_value = exact_nonnegative_integer(browser.get(field))
        android_value = exact_nonnegative_integer(android.get(field))
        if python_value is None or android_value is None:
            continue
        scale = max(python_value, android_value)
        difference = abs(python_value - android_value)
        if difference <= 300:
            continue
        allowed_ratio = 0.30 if scale <= 1_000 else 0.10 if scale <= 5_000 else 0.05
        ratio = difference / scale if scale else 0.0
        if ratio > allowed_ratio:
            mismatches[field] = {
                "python": python_value,
                "android": android_value,
                "difference": difference,
                "difference_ratio": ratio,
                "allowed_ratio": allowed_ratio,
            }
    return mismatches


def format_android_mismatch(attempt: int, mismatches: dict[str, dict[str, float | int]]) -> str:
    return f"attempt {attempt}: " + "; ".join(
        f"{field} python={values['python']} android={values['android']}"
        for field, values in sorted(mismatches.items())
    )


def _is_android_device_connection_error(error: Any) -> bool:
    message = str(error or "").casefold()
    return any(
        marker in message
        for marker in (
            "device offline",
            "android device busy",
            "device still authorizing",
            "device unauthorized",
            "device not found",
            "no devices/emulators found",
            "no online android emulator was found",
            "has not finished booting",
            "error: closed",
            "adb command timed out",
            "adb_utils.cpp",
            "cannot mkdir",
            "can't find service:",
        )
    ) or ("device '" in message and "' not found" in message)


def _is_android_ui_dump_timeout(error: Any) -> bool:
    return "adb command timed out" in str(error or "").casefold() and "exec-out sh -c" in str(error or "").casefold()


def _is_android_ui_read_failure(error: Any) -> bool:
    message = str(error or "").casefold()
    return _is_android_ui_dump_timeout(error) or "empty or unreadable screen" in message or "uiautomator cleanup failed" in message

