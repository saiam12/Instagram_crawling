from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class ReelAccessError(RuntimeError):
    """A user-facing error while resolving or downloading a Reel."""


@dataclass(frozen=True)
class ReelReference:
    url: str
    shortcode: str
    media_pk: str | None = None


_SHORTCODE = re.compile(r"^[A-Za-z0-9_-]+$")


def _failure_detail(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1][:300] if lines else "No detail returned by yt-dlp"


def parse_reel_url(value: str) -> ReelReference:
    """Validate a Reel permalink and return its canonical identity."""
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"instagram.com", "www.instagram.com"}:
        raise ReelAccessError("Unsupported URL")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0].lower() not in {"reel", "reels"} or not _SHORTCODE.fullmatch(parts[1]):
        raise ReelAccessError("Unsupported URL")
    shortcode = parts[1]
    return ReelReference(f"https://www.instagram.com/reel/{shortcode}/", shortcode)


def download_reel(
    reference: ReelReference,
    directory: Path,
    browser: str | None = None,
    cookies_file: Path | None = None,
) -> Path:
    """Download a permitted Reel into *directory* using yt-dlp only."""
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--no-warnings",
        "--print",
        "after_move:filepath",
        "-o",
        str(directory / "%(id)s.%(ext)s"),
    ]
    if browser:
        command.extend(["--cookies-from-browser", browser])
    if cookies_file:
        command.extend(["--cookies", str(cookies_file)])
    command.append(reference.url)
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as error:
        raise ReelAccessError("Video extraction failed: yt-dlp is not installed") from error
    if completed.returncode:
        output = f"{completed.stdout}\n{completed.stderr}"
        detail = output.lower()
        if "no module named yt_dlp" in detail:
            message = "Video extraction failed: yt-dlp is not installed"
        elif "login" in detail or "cookies" in detail:
            message = "Login required"
        elif "not available" in detail or "not found" in detail:
            message = "Reel not found"
        elif "403" in detail or "blocked" in detail or "429" in detail:
            message = "Instagram access blocked"
        else:
            message = "Video extraction failed"
        raise ReelAccessError(f"{message}: {_failure_detail(output)}")
    paths = [Path(line.strip()) for line in completed.stdout.splitlines() if line.strip()]
    for path in reversed(paths):
        if path.is_file():
            return path
    videos = [path for path in directory.iterdir() if path.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm"}]
    if len(videos) == 1:
        return videos[0]
    raise ReelAccessError("Video extraction failed")
