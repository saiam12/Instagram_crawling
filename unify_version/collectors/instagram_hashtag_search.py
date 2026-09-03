"""Android related-tag counts with browser-verified exact media counts."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit

from collectors.android_reel_metrics import (
    collect_android_related_hashtag_rows_and_counts,
    write_hashtag_post_counts,
)
from collectors.instagram_reels_browser import load_playwright, locate_browser_executable


GRAPHQL_SEARCH_CONNECTION = "xdt_api__v1__fbsearch__topsearch_connection"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _graphql_hashtags(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    connection = data.get(GRAPHQL_SEARCH_CONNECTION) if isinstance(data, dict) else None
    values = connection.get("hashtags") if isinstance(connection, dict) else None
    if not isinstance(values, list):
        return []
    hashtags: list[dict[str, Any]] = []
    for value in values:
        hashtag = value.get("hashtag") if isinstance(value, dict) else None
        if isinstance(hashtag, dict):
            hashtags.append(hashtag)
    return hashtags


def extract_graphql_exact_media_count(payload: Any, hashtag_name: str) -> int | None:
    """Return an exact media_count only when the web result matches the tag."""
    expected = str(hashtag_name).strip().lstrip("#").casefold()
    for hashtag in _graphql_hashtags(payload):
        name = str(hashtag.get("name", "")).strip().lstrip("#")
        media_count = hashtag.get("media_count")
        if (
            name.casefold() == expected
            and isinstance(media_count, int)
            and not isinstance(media_count, bool)
            and media_count >= 0
        ):
            return media_count
    return None


async def collect_python_exact_hashtag_counts(
    hashtags: Sequence[str],
    *,
    profile_dir: Path,
    timeout_seconds: float = 10,
    query_interval_seconds: float = 0.5,
) -> dict[str, int]:
    """Search each Android-discovered tag and retain only exact media counts."""
    queries = [str(value).strip().lstrip("#") for value in hashtags if str(value).strip().lstrip("#")]
    query_queue: asyncio.Queue[str | None] = asyncio.Queue()
    for query in queries:
        query_queue.put_nowait(query)
    query_queue.put_nowait(None)
    return await collect_python_exact_hashtag_count_stream(
        query_queue,
        profile_dir=profile_dir,
        timeout_seconds=timeout_seconds,
        query_interval_seconds=query_interval_seconds,
        total_queries=len(queries),
    )


async def collect_python_exact_hashtag_count_stream(
    query_queue: asyncio.Queue[str | None],
    *,
    profile_dir: Path,
    timeout_seconds: float = 10,
    query_interval_seconds: float = 0.5,
    total_queries: int | None = None,
) -> dict[str, int]:
    """Keep one browser open while Android streams hashtag names into it."""
    response_queue: asyncio.Queue[tuple[int, Any]] = asyncio.Queue()
    response_tasks: set[asyncio.Task[None]] = set()
    results: dict[str, int] = {}

    try:
        async_playwright = load_playwright()
        async with async_playwright() as runtime:
            profile_dir.mkdir(parents=True, exist_ok=True)
            context = await runtime.chromium.launch_persistent_context(
                str(profile_dir),
                executable_path=locate_browser_executable(),
                headless=True,
                viewport={"width": 1440, "height": 1000},
            )
            page = context.pages[0] if context.pages else await context.new_page()

            async def inspect_response(response: Any) -> None:
                if urlsplit(response.url).path.casefold() != "/api/graphql":
                    return
                if response.status == 429:
                    await response_queue.put((429, None))
                    return
                try:
                    payload = await response.json()
                except Exception:
                    try:
                        payload = json.loads(await response.text())
                    except Exception:
                        return
                if _graphql_hashtags(payload):
                    await response_queue.put((response.status, payload))

            def schedule_response(response: Any) -> None:
                task = asyncio.create_task(inspect_response(response))
                response_tasks.add(task)
                task.add_done_callback(response_tasks.discard)

            page.on("response", schedule_response)
            await page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(2_000)
            for selector in ('[aria-label="Search"]', '[aria-label="검색"]'):
                trigger = page.locator(selector).first
                if await trigger.count() and await trigger.is_visible():
                    await trigger.click()
                    break
            await page.wait_for_timeout(1_000)
            search_input = page.locator(
                'input[placeholder="Search"], input[placeholder="검색"], '
                'input[aria-label="Search input"], input[aria-label="검색 입력"]'
            ).first
            if not await search_input.count():
                raise RuntimeError("Instagram web search input was not visible.")

            rate_limited = False
            index = 0
            while True:
                queued_query = await query_queue.get()
                if queued_query is None:
                    break
                query = str(queued_query).strip().lstrip("#")
                if not query:
                    continue
                index += 1
                label = f"{index}/{total_queries}" if total_queries is not None else str(index)
                while not response_queue.empty():
                    response_queue.get_nowait()
                if rate_limited:
                    print(f"[PYTHON hashtag {label}] #{query} -> unavailable (rate limited)")
                    continue
                await search_input.fill("")
                await page.wait_for_timeout(200)
                await search_input.fill(f"#{query}")
                deadline = asyncio.get_running_loop().time() + timeout_seconds
                exact_media_count: int | None = None
                while exact_media_count is None:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        status, payload = await asyncio.wait_for(response_queue.get(), timeout=remaining)
                    except TimeoutError:
                        break
                    if status == 429:
                        rate_limited = True
                        break
                    exact_media_count = extract_graphql_exact_media_count(payload, query)
                if exact_media_count is not None:
                    results[query.casefold()] = exact_media_count
                    print(f"[PYTHON hashtag {label}] #{query} -> exact {exact_media_count:,}")
                else:
                    error = "Instagram web search was rate limited." if rate_limited else "GraphQL returned no representative hashtag media_count."
                    print(f"[PYTHON hashtag {label}] #{query} -> unavailable ({error})")
                await page.wait_for_timeout(int(query_interval_seconds * 1_000))
            if response_tasks:
                await asyncio.gather(*tuple(response_tasks), return_exceptions=True)
            await context.close()
    except Exception as error:
        print(f"[PYTHON hashtag] -> unavailable ({error})")
    return results


def overlay_android_rows_with_exact_python_media_counts(
    android_rows: Sequence[dict[str, object]],
    exact_media_counts: dict[str, int],
) -> list[dict[str, object]]:
    """Replace Android's compact count only when web search confirms that tag."""
    rows: list[dict[str, object]] = []
    for row in android_rows:
        exact_media_count = exact_media_counts.get(str(row.get("hashtag", "")).strip().lstrip("#").casefold())
        if exact_media_count is None:
            rows.append(dict(row))
            continue
        rows.append({
            **row,
            "media_count": exact_media_count,
            "post_count": exact_media_count,
            "raw_post_count": str(exact_media_count),
            "source": f"{row.get('source', 'android_search_tags')}+python_web_search_graphql_exact",
        })
    return rows


async def collect_hashtag_count_report(
    data_dir: Path,
    hashtags: Sequence[str],
    *,
    profile_dir: Path,
    adb_path: Path | None = None,
    device_id: str | None = None,
    ui_delay_seconds: float = 0.2,
) -> tuple[list[dict[str, object]], dict[str, Path], list[dict[str, object]]]:
    """Stream Android-discovered tags to one browser worker while Android keeps scrolling."""
    rows: list[dict[str, object]] = []
    summaries: list[dict[str, object]] = []
    query_queue: asyncio.Queue[str | None] = asyncio.Queue()
    event_loop = asyncio.get_running_loop()
    queued_tags: set[str] = set()

    def publish_android_rows(new_rows: Sequence[dict[str, object]]) -> None:
        if new_rows:
            write_hashtag_post_counts(
                data_dir,
                new_rows,
                replace_matching_snapshots=True,
            )
        for row in new_rows:
            name = str(row.get("hashtag", "")).strip().lstrip("#")
            key = name.casefold()
            if not name or key in queued_tags:
                continue
            queued_tags.add(key)
            try:
                event_loop.call_soon_threadsafe(query_queue.put_nowait, name)
            except RuntimeError:
                # A forced terminal close can end the event loop while the
                # Android worker finishes its current page. The rows above
                # are already on disk, so no browser handoff is needed.
                pass

    browser_task = asyncio.create_task(
        collect_python_exact_hashtag_count_stream(query_queue, profile_dir=profile_dir)
    )
    try:
        queries = [str(hashtag).strip().lstrip("#") for hashtag in hashtags if str(hashtag).strip().lstrip("#")]
        for progress_index, hashtag in enumerate(queries, start=1):
            related_rows, android_summaries = await asyncio.to_thread(
                collect_android_related_hashtag_rows_and_counts,
                [hashtag],
                adb_path=adb_path,
                device_id=device_id,
                ui_delay_seconds=ui_delay_seconds,
                on_rows=publish_android_rows,
                progress_index=progress_index,
                progress_total=len(queries),
            )
            summaries.extend(android_summaries)
            rows.extend(related_rows)
            write_hashtag_post_counts(
                data_dir,
                related_rows,
                replace_matching_snapshots=True,
            )
    except asyncio.CancelledError:
        if rows:
            await asyncio.to_thread(
                write_hashtag_post_counts,
                data_dir,
                rows,
                replace_matching_snapshots=True,
            )
        browser_task.cancel()
        await asyncio.gather(browser_task, return_exceptions=True)
        raise
    finally:
        # Flush callbacks submitted by the Android worker before ending the
        # browser stream; otherwise its sentinel could overtake the last tag.
        await asyncio.sleep(0)
        query_queue.put_nowait(None)
    exact_media_counts = await browser_task
    rows = overlay_android_rows_with_exact_python_media_counts(rows, exact_media_counts)
    return rows, write_hashtag_post_counts(
        data_dir,
        rows,
        replace_matching_snapshots=True,
    ), summaries
