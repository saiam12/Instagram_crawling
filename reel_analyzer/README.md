# Instagram Reel Analyzer

An independent CLI that downloads a permitted public Instagram Reel (or reads a local video), selects representative frames, and sends them to a local Ollama `qwen3-vl:8b` model. It does not alter the repository's existing collectors.

## Setup

```powershell
cd C:\Instagram-crawling\reel_analyzer
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Start Ollama and install the model:

```powershell
ollama pull qwen3-vl:8b
ollama list
ollama ps
```

## Run

```powershell
cd C:\Instagram-crawling\reel_analyzer
.\.venv\Scripts\python.exe reel_analyzer.py
.\.venv\Scripts\python.exe reel_analyzer.py --url "https://www.instagram.com/reel/SHORTCODE/"
.\.venv\Scripts\python.exe reel_analyzer.py --file .\test.mp4
```

The analyzer samples at least three frames per second, then adds opening and scene-change frames. Every sampled timestamp is saved in the result. Frames that are visually similar to the latest meaningful frame are recorded as `similar_to_previous` and are not sent to Qwen; scene changes and opening frames are always analyzed. The remaining frames are sent as overlapping batches of three, with one shared frame and a compact JSON handoff between batches. This keeps the default Ollama context at 4,096 tokens while preserving the sequence.

`--max-frames` is now optional. Do not set it when the minimum three-frames-per-second rule matters, because a hard cap can reduce that sampling rate. Use `--min-fps` only when you deliberately want a higher or lower baseline.

For a Reel that needs an existing browser session, opt in to cookie use; it is never enabled by default:

```powershell
$env:INSTAGRAM_BROWSER = "edge"  # or chrome
.\.venv\Scripts\python.exe reel_analyzer.py --url "https://www.instagram.com/reel/SHORTCODE/"
```

If yt-dlp cannot copy an open Chromium cookie database, export a fresh Netscape-format `cookies.txt` from your logged-in browser and use it instead. Treat this file as a password: it is ignored by Git and must not be shared or committed.

```powershell
.\.venv\Scripts\python.exe reel_analyzer.py --cookies .\cookies.txt --url "https://www.instagram.com/reel/SHORTCODE/"
```

Set `DEBUG=true` to preserve the selected JPEGs in `debug_frames`; otherwise downloaded video and extracted frames are kept only in a temporary directory and removed automatically.

Successful analyses are accumulated in `results\reel_analyses.json`. Running the same Reel again updates its existing shortcode entry.

## Verification

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_reel_analysis -v
```

The tests cover URL parsing, three-frames-per-second sampling, similar-frame skipping, priority selection and JSON extraction. They do not send any request to Instagram or Ollama.
