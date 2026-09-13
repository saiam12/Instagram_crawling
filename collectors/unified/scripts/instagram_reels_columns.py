"""Backward-compatible launcher for the single standard Reel output."""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from collectors.instagram_reels_browser import main as collector_main  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    return collector_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
