from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from instagram.reel_resolver import ReelAccessError, ReelReference, download_reel, parse_reel_url
from models.reel_analysis import compose_result
from video.frame_sampler import build_candidates, extract_selected_frames, frames_for_analysis
from video.metadata import read_video_metadata
from video.scene_detector import detect_scene_changes
from vl.ollama_client import OllamaClient, OllamaError
from vl.parser import QwenResponseError


ROOT = Path(__file__).resolve().parent
RESULTS_DIR = ROOT / "results"
RESULTS_FILE = RESULTS_DIR / "reel_analyses.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze an Instagram Reel with local Qwen3-VL via Ollama.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--url", help="Instagram Reel URL")
    source.add_argument("--file", type=Path, help="Local video file; skips Instagram download")
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument("--browser", default=os.getenv("INSTAGRAM_BROWSER"), help="Optional yt-dlp browser cookie source, such as edge or chrome")
    auth.add_argument("--cookies", type=Path, help="Netscape cookies.txt file for Instagram authentication")
    parser.add_argument("--ollama-url", default=os.getenv("OLLAMA_URL", "http://localhost:11434"))
    parser.add_argument("--model", default=os.getenv("OLLAMA_MODEL", "qwen3-vl:8b"))
    parser.add_argument("--min-fps", type=float, default=3.0, help="Minimum sampled frames per second")
    parser.add_argument("--max-frames", type=int, help="Optional hard cap; it can reduce the minimum FPS")
    parser.add_argument("--num-ctx", type=int, default=int(os.getenv("OLLAMA_NUM_CTX", "4096")), help="Ollama context window")
    parser.add_argument("--batch-size", type=int, default=3, help="Frames per overlapping Qwen request")
    parser.add_argument("--batch-overlap", type=int, default=1, help="Frames shared by consecutive requests")
    return parser.parse_args(argv)


def _read_url() -> str:
    try:
        return input("Instagram Reel URL: ").strip()
    except EOFError as error:
        raise ReelAccessError("Unsupported URL") from error


def _copy_debug_frames(frames) -> None:
    debug_dir = ROOT / "debug_frames"
    debug_dir.mkdir(exist_ok=True)
    for frame in frames:
        shutil.copy2(frame.path, debug_dir / frame.path.name)


def save_result(result: dict) -> Path:
    RESULTS_DIR.mkdir(exist_ok=True)
    records: list[dict] = []
    if RESULTS_FILE.is_file():
        loaded = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
        if not isinstance(loaded, list):
            raise ValueError(f"Results file must contain a JSON array: {RESULTS_FILE}")
        records = loaded
    shortcode = result.get("shortcode")
    if shortcode:
        records = [record for record in records if record.get("shortcode") != shortcode]
    records.append(result)
    RESULTS_FILE.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return RESULTS_FILE


def analyze(options: argparse.Namespace) -> dict:
    if (
        options.min_fps <= 0
        or (options.max_frames is not None and options.max_frames <= 0)
        or options.num_ctx <= 0
        or options.batch_size <= 0
        or not 0 <= options.batch_overlap < options.batch_size
    ):
        raise ValueError("--min-fps, --num-ctx, and --batch-size must be positive; --max-frames must be positive when used; overlap must be smaller than batch size")
    reference: ReelReference | None = parse_reel_url(options.url) if options.url else None
    with tempfile.TemporaryDirectory(prefix="reel-analysis-") as temporary:
        temp_dir = Path(temporary)
        if options.file:
            video_path = options.file.expanduser().resolve()
            if not video_path.is_file():
                raise FileNotFoundError(f"Video file not found: {video_path}")
        else:
            assert reference is not None
            if options.cookies and not options.cookies.is_file():
                raise FileNotFoundError(f"Cookies file not found: {options.cookies}")
            video_path = download_reel(reference, temp_dir, options.browser, options.cookies)
        print("[3/6] Video metadata")
        metadata = read_video_metadata(video_path)
        print("[4/6] Extracting important frames")
        try:
            scene_changes = detect_scene_changes(video_path, metadata.duration_sec)
        except Exception as error:
            print(f"Scene detection failed; using minimum-FPS sampling only: {error}", file=sys.stderr)
            scene_changes = []
        frames = extract_selected_frames(
            video_path,
            build_candidates(metadata.duration_sec, scene_changes, options.min_fps),
            temp_dir / "frames",
            options.max_frames,
        )
        if not frames:
            raise RuntimeError("No frames were extracted")
        if os.getenv("DEBUG", "false").lower() == "true":
            _copy_debug_frames(frames)
        print("[5/6] Qwen3-VL analysis")
        client = OllamaClient(options.ollama_url, options.model, options.num_ctx)
        client.ensure_ready()
        analysis_frames = frames_for_analysis(frames)
        print(f"  {len(analysis_frames)}/{len(frames)} frames need Qwen analysis")
        analysis = client.analyze_frames(
            [frame.path for frame in analysis_frames],
            [frame.timestamp for frame in analysis_frames],
            options.batch_size,
            options.batch_overlap,
            lambda current, total: print(f"  Qwen batch {current}/{total}"),
        )
        print("[6/6] Cleaning temporary files")
        return compose_result(
            shortcode=reference.shortcode if reference else None,
            media_pk=reference.media_pk if reference else None,
            reel_url=reference.url if reference else None,
            metadata=metadata,
            frames=frames,
            analyzed_frames=analysis_frames,
            model=client.model,
            analysis=analysis,
        )


def main(argv: list[str] | None = None) -> int:
    options = parse_args(argv)
    try:
        if not options.url and not options.file:
            options.url = _read_url()
        if options.url:
            print("[1/6] Checking Instagram Reel URL")
            print("[2/6] Downloading video")
        result = analyze(options)
        result_path = save_result(result)
    except QwenResponseError as error:
        print(f"Error: {error}\nRaw Qwen response:\n{error.raw_response}", file=sys.stderr)
        return 1
    except (ReelAccessError, OllamaError, RuntimeError, ValueError, FileNotFoundError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Saved result: {result_path}")
    print("Analysis completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
