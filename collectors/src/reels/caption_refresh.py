"""Queue and apply updated Reel captions when Android detects caption modification."""
from __future__ import annotations

import asyncio
import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .collection_log import append_collection_log
from .process_lock import AtomicProcessLock
from .reel_records import caption_without_hashtags, extract_hashtags, normalize_reel_url, truncate_caption
from .reel_history import (
    LongReelStore,
    REEL_HISTORY_DIRECTORY,
    REEL_HISTORY_FILENAME,
    write_csv_records,
    write_long_output_bundle,
)
from .korean_classification import apply_korean_overrides
from .instagram_reels_browser import enrich_reel_collection_rows
from .instagram_follower_enricher import read_csv_objects

logger = logging.getLogger(__name__)

CAPTION_REFRESH_QUEUE_DIRNAME = "caption_refresh_pending"
CAPTION_REFRESH_FAILED_DIRNAME = "caption_refresh_failed"
MAX_REFRESH_ATTEMPTS = 3


def _caption_refresh_dir(data_dir: Path | str) -> Path:
    destination = Path(data_dir).resolve()
    # If passed data_dir is already inside .collector or is dataset root
    queue_root = destination if destination.name == ".collector" else destination / ".collector"
    pending_dir = queue_root / "android_metric_queue" / CAPTION_REFRESH_QUEUE_DIRNAME
    pending_dir.mkdir(parents=True, exist_ok=True)
    return pending_dir


def _caption_refresh_failed_dir(data_dir: Path | str) -> Path:
    destination = Path(data_dir).resolve()
    queue_root = destination if destination.name == ".collector" else destination / ".collector"
    failed_dir = queue_root / "android_metric_queue" / CAPTION_REFRESH_FAILED_DIRNAME
    failed_dir.mkdir(parents=True, exist_ok=True)
    return failed_dir


def fail_caption_refresh_job(data_dir: Path | str, shortcode: str, reason: str = "") -> None:
    """Move an unrecoverable caption refresh job to failed directory."""
    pending_dir = _caption_refresh_dir(data_dir)
    failed_dir = _caption_refresh_failed_dir(data_dir)
    job_file = pending_dir / f"{shortcode}.json"
    if not job_file.exists():
        return
    try:
        data = json.loads(job_file.read_text(encoding="utf-8"))
        data["failed_at"] = datetime.now(timezone.utc).isoformat()
        data["failure_reason"] = reason
        failed_file = failed_dir / f"{shortcode}.json"
        failed_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        job_file.unlink(missing_ok=True)
        append_collection_log(
            data_dir,
            "python",
            "caption_refresh_failed",
            shortcode=shortcode,
            reason=reason,
        )
    except Exception as err:
        logger.warning("Failed to archive failed caption job %s: %s", shortcode, err)


def _update_job_attempts(data_dir: Path | str, shortcode: str, attempts: int, last_error: str = "") -> None:
    pending_dir = _caption_refresh_dir(data_dir)
    job_file = pending_dir / f"{shortcode}.json"
    if not job_file.exists():
        return
    try:
        data = json.loads(job_file.read_text(encoding="utf-8"))
        data["attempts"] = attempts
        data["last_error"] = last_error
        data["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
        job_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def enqueue_caption_refresh_job(
    data_dir: Path | str,
    *,
    url: str,
    shortcode: str,
    username: str = "",
) -> Path | None:
    """Enqueue a Reel whose caption changed for Web metadata re-fetch."""
    if not shortcode:
        normalized = normalize_reel_url(url)
        shortcode = normalized["shortcode"] if normalized else ""
    if not shortcode:
        return None
    pending_dir = _caption_refresh_dir(data_dir)
    job_file = pending_dir / f"{shortcode}.json"
    payload = {
        "url": url,
        "shortcode": shortcode,
        "username": username,
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    # Atomic write
    tmp_path = job_file.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(job_file)
    append_collection_log(
        data_dir,
        "android",
        "caption_refresh_enqueued",
        url=url,
        shortcode=shortcode,
        username=username,
    )
    return job_file


def list_pending_caption_refresh_jobs(data_dir: Path | str) -> list[dict[str, Any]]:
    """List pending caption refresh requests."""
    pending_dir = _caption_refresh_dir(data_dir)
    jobs: list[dict[str, Any]] = []
    for path in sorted(pending_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("shortcode"):
                jobs.append(data)
        except (OSError, ValueError):
            continue
    return jobs


def complete_caption_refresh_job(data_dir: Path | str, shortcode: str) -> None:
    """Remove a finished caption refresh job."""
    pending_dir = _caption_refresh_dir(data_dir)
    job_file = pending_dir / f"{shortcode}.json"
    job_file.unlink(missing_ok=True)


def apply_refreshed_captions_to_history(
    data_dir: Path | str,
    updates: dict[str, dict[str, str]],
) -> int:
    """Update title and hashtags in history and export bundles.

    updates: mapping from shortcode or url to dict with 'title' and 'hashtags'.
    """
    if not updates:
        return 0
    destination = Path(data_dir).resolve()
    history_path = destination / REEL_HISTORY_DIRECTORY / REEL_HISTORY_FILENAME
    if not history_path.exists() or not history_path.stat().st_size:
        return 0

    lock_path = destination / ".collector" / "android_metric_queue" / "history.lock.json"
    lock = AtomicProcessLock(lock_path, wait_seconds=30)
    if not lock.acquire():
        return 0

    try:
        fields, rows = read_csv_objects(history_path)
        updated_count = 0
        for row in rows:
            url = str(row.get("url") or "")
            normalized = normalize_reel_url(url)
            shortcode = normalized["shortcode"] if normalized else ""
            update = updates.get(shortcode) or updates.get(url)
            if update:
                new_title = update.get("title")
                new_hashtags = update.get("hashtags")
                changed = False
                if new_title is not None and row.get("title") != new_title:
                    row["title"] = new_title
                    changed = True
                if new_hashtags is not None and row.get("hashtags") != new_hashtags:
                    row["hashtags"] = new_hashtags
                    changed = True
                if changed:
                    updated_count += 1

        if updated_count:
            rows = enrich_reel_collection_rows(apply_korean_overrides(rows, destination))
            write_csv_records(history_path, rows, fields)
            write_long_output_bundle(history_path, rows, fields)
            from .android_metric_worker import write_count_history_xlsx
            write_count_history_xlsx(destination, rows)
            append_collection_log(
                destination,
                "python",
                "caption_refresh_applied",
                updated_rows=updated_count,
                affected_urls=sorted(list(updates.keys())),
            )
        return updated_count
    finally:
        lock.release()


async def fetch_caption_via_page_navigation(page: Any, shortcode: str) -> dict[str, Any]:
    """Fallback Method B: Navigate directly to Reel page to retrieve caption when direct API returns 429."""
    from urllib.parse import quote
    from .browser_runtime import safe_close
    from .instagram_reels_browser import (
        collect_embedded_reel_metadata,
        attach_reel_metadata_collector,
    )

    context = getattr(page, "context", None)
    target_page = page
    owns_page = False
    if context is not None and hasattr(context, "new_page"):
        try:
            target_page = await context.new_page()
            owns_page = True
        except Exception:
            target_page = page
            owns_page = False

    reel_metadata: dict[str, dict[str, Any]] = {}
    try:
        attach_reel_metadata_collector(target_page, reel_metadata)
        url = f"https://www.instagram.com/reels/{quote(shortcode, safe='')}/"
        try:
            response = await target_page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            status = getattr(response, "status", None)
            if status == 429:
                logger.warning("[CAPTION_REFRESH] Method B page navigation also received HTTP 429 for %s", shortcode)
                return {"rate_limited": True}
            await target_page.wait_for_timeout(2_000)
        except Exception as nav_err:
            if "429" in str(nav_err):
                return {"rate_limited": True}
            logger.warning("[CAPTION_REFRESH] Method B navigation to %s encountered error: %s", url, nav_err)

        # 1. Check response-collected metadata and embedded script metadata
        await collect_embedded_reel_metadata(target_page, reel_metadata)
        candidate = reel_metadata.get(shortcode, {})
        caption = str(candidate.get("caption") or "").strip()
        if caption:
            return {"caption": caption}

        # 2. Check DOM / meta tags as secondary fallback
        dom_caption = await target_page.evaluate(r"""() => {
            const metaOg = document.querySelector('meta[property="og:description"]');
            if (metaOg && metaOg.content) return metaOg.content;
            const metaDesc = document.querySelector('meta[name="description"]');
            if (metaDesc && metaDesc.content) return metaDesc.content;
            const h1 = document.querySelector('h1');
            if (h1 && h1.innerText) return h1.innerText;
            return '';
        }""")
        if dom_caption:
            clean_dom = re.sub(r'^.*?:\s*"?(.*?)"?$', r'\1', str(dom_caption), flags=re.DOTALL).strip()
            return {"caption": clean_dom or str(dom_caption).strip()}

        return {}
    except Exception as err:
        logger.warning("[CAPTION_REFRESH] Method B failed for %s: %s", shortcode, err)
        return {}
    finally:
        if owns_page:
            await safe_close(target_page)


async def drain_caption_refresh_queue(
    page: Any,
    data_dir: Path | str,
    *,
    max_items: int = 20,
    cooldown_seconds: float = 1.0,
) -> int:
    """Process pending caption refresh requests using the authenticated Web page."""
    jobs = list_pending_caption_refresh_jobs(data_dir)
    if not jobs:
        return 0

    from .instagram_reels_browser import request_reel_info_metadata

    updates: dict[str, dict[str, str]] = {}
    completed_shortcodes: list[str] = []

    for job in jobs[:max_items]:
        shortcode = str(job.get("shortcode") or "").strip()
        url = str(job.get("url") or "")
        if not shortcode:
            continue
        diagnostic: dict[str, str] = {}
        try:
            # Step 1: Try Method A (Fast Direct API)
            metadata = await request_reel_info_metadata(page, shortcode, diagnostic)
            err_msg = str(diagnostic.get("error") or "")
            diag_status = str(diagnostic.get("status") or "")

            # Step 2: Deleted / Not Found (HTTP 404 / 410):
            if "404" in err_msg or "410" in err_msg:
                logger.info(
                    "[CAPTION_REFRESH] Reel %s no longer available (HTTP 404/410). Archiving job.",
                    shortcode,
                )
                fail_caption_refresh_job(data_dir, shortcode, reason="not_found")
                continue

            # Step 3: If Method A hits 429 (Rate Limit) or fails, fallback to Method B (Page Navigation)
            if "429" in err_msg or "rate" in err_msg.casefold() or not metadata:
                if "429" in err_msg or "rate" in err_msg.casefold():
                    logger.info(
                        "[CAPTION_REFRESH] Method A encountered HTTP 429 for %s. Executing fallback Method B (Page Navigation)...",
                        shortcode,
                    )
                append_collection_log(
                    data_dir,
                    "python",
                    "caption_refresh_fallback_method_b",
                    shortcode=shortcode,
                    reason=err_msg or "method_a_empty",
                )
                metadata = await fetch_caption_via_page_navigation(page, shortcode)
                if metadata.get("rate_limited"):
                    logger.warning(
                        "[CAPTION_REFRESH] Method B also rate-limited for %s. Halting queue to allow cooldown.",
                        shortcode,
                    )
                    break

            # Step 4: Access Denied (HTTP 401 / 403) detected:
            if ("401" in err_msg or "403" in err_msg) and not metadata:
                logger.warning(
                    "[CAPTION_REFRESH] Instagram access denied (%s) for %s.",
                    err_msg,
                    shortcode,
                )
                attempts = int(job.get("attempts") or 0) + 1
                if attempts >= MAX_REFRESH_ATTEMPTS:
                    fail_caption_refresh_job(data_dir, shortcode, reason=f"access_denied: {err_msg}")
                else:
                    _update_job_attempts(data_dir, shortcode, attempts, err_msg)
                # Halt batch to prevent account challenge
                break

            full_caption = str(metadata.get("caption") or "").strip()
            if full_caption:
                clean_title = truncate_caption(caption_without_hashtags(full_caption), 300)
                hashtags = extract_hashtags(full_caption)
                updates[shortcode] = {
                    "title": clean_title,
                    "hashtags": hashtags,
                    "caption": full_caption,
                }
                completed_shortcodes.append(shortcode)
                safe_title = clean_title.encode("ascii", "namereplace").decode("ascii")
                logger.info("[CAPTION_REFRESH] Updated metadata for %s: title='%s' hashtags='%s'", shortcode, clean_title, hashtags)
            elif metadata and not metadata.get("rate_limited"):
                # Post exists but caption is empty
                updates[shortcode] = {
                    "title": "",
                    "hashtags": "",
                    "caption": "",
                }
                completed_shortcodes.append(shortcode)
            else:
                # Empty metadata without fatal HTTP code (timeout or temporary network glitch)
                attempts = int(job.get("attempts") or 0) + 1
                if attempts >= MAX_REFRESH_ATTEMPTS:
                    logger.warning("[CAPTION_REFRESH] Max retries reached for %s. Archiving as failed.", shortcode)
                    fail_caption_refresh_job(data_dir, shortcode, reason=f"max_retries_exceeded: {err_msg or diag_status}")
                else:
                    _update_job_attempts(data_dir, shortcode, attempts, err_msg or diag_status)

            # Cooldown safety pause between items
            if cooldown_seconds > 0 and job != jobs[:max_items][-1]:
                await asyncio.sleep(cooldown_seconds)
        except Exception as error:
            logger.warning("Failed to refresh caption for %s: %s", shortcode, error)
            continue

    if updates:
        apply_refreshed_captions_to_history(data_dir, updates)
        for shortcode in completed_shortcodes:
            complete_caption_refresh_job(data_dir, shortcode)

    return len(completed_shortcodes)
