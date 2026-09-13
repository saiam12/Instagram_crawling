from __future__ import annotations

import asyncio
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import collectors.instagram_hashtag_search as hashtag_search
from collectors.instagram_hashtag_search import (
    collect_hashtag_count_report,
    extract_graphql_exact_media_count,
    overlay_android_rows_with_exact_python_media_counts,
)


class InstagramHashtagSearchTests(unittest.TestCase):
    def test_graphql_parser_keeps_only_the_exact_hashtag_media_count(self) -> None:
        payload = {
            "data": {
                "xdt_api__v1__fbsearch__topsearch_connection": {
                    "hashtags": [
                        {"hashtag": {"name": f"fashion{index}", "media_count": 10_000 + index}}
                        for index in range(1, 7)
                    ]
                }
            }
        }

        self.assertEqual(extract_graphql_exact_media_count(payload, "fashion3"), 10_003)
        self.assertIsNone(extract_graphql_exact_media_count(payload, "fashion"))

    def test_web_exact_count_overlays_android_compact_count_but_keeps_unmatched_android_values(self) -> None:
        android_rows = [{
            "collected_at": "2026-09-01T00:00:01Z", "query_hashtag": "fashion", "rank": "",
            "hashtag": "fashion", "media_count": "28M", "post_count": 28_000_000,
            "raw_post_count": "28M posts", "source": "android_search_tags", "status": "collected", "error": "",
        }, {
            "collected_at": "2026-09-01T00:00:01Z", "query_hashtag": "fashion", "rank": "",
            "hashtag": "fashionstyle", "media_count": "1.3M", "post_count": 1_300_000,
            "raw_post_count": "1.3M posts", "source": "android_search_tags", "status": "collected", "error": "",
        }]
        combined = overlay_android_rows_with_exact_python_media_counts(android_rows, {"fashionstyle": 1_321_987})

        self.assertEqual([row["post_count"] for row in combined], [28_000_000, 1_321_987])
        self.assertEqual(combined[0]["media_count"], "28M")
        self.assertEqual(combined[0]["source"], "android_search_tags")
        self.assertEqual(combined[1]["media_count"], 1_321_987)
        self.assertIn("python_web_search_graphql_exact", combined[1]["source"])

    def test_report_streams_android_tags_to_python_while_android_keeps_collecting(self) -> None:
        android_calls: list[list[str]] = []
        android_progress: list[tuple[int | None, int | None]] = []
        web_calls: list[str] = []
        python_consumed_first_tag = threading.Event()

        def fake_android(
            hashtags: list[str],
            *,
            on_rows: object = None,
            progress_index: int | None = None,
            progress_total: int | None = None,
            **_kwargs: object,
        ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
            query = hashtags[0]
            android_calls.append(hashtags)
            android_progress.append((progress_index, progress_total))
            names = {"fashion": ("fashion", "shared"), "beauty": ("shared", "beauty")}[query]
            rows = [
                {"query_hashtag": query, "hashtag": name, "media_count": "1K", "post_count": 1_000,
                 "source": "android_search_tags", "status": "collected", "error": ""}
                for name in names
            ]
            self.assertTrue(callable(on_rows))
            on_rows(rows)
            if query == "fashion" and not python_consumed_first_tag.wait(timeout=2):
                raise AssertionError("Python did not begin the first web lookup while Android was still collecting.")
            return rows, [{"query_hashtag": query, "related_hashtag_count": len(names), "status": "collected", "error": ""}]

        async def fake_web(query_queue: asyncio.Queue[str | None], **_kwargs: object) -> dict[str, int]:
            while True:
                name = await query_queue.get()
                if name is None:
                    break
                web_calls.append(name)
                if name == "fashion":
                    python_consumed_first_tag.set()
            return {name.casefold(): len(name) for name in web_calls}

        with (
            patch.object(hashtag_search, "collect_android_related_hashtag_rows_and_counts", fake_android),
            patch.object(hashtag_search, "collect_python_exact_hashtag_count_stream", fake_web),
            patch.object(
                hashtag_search,
                "write_hashtag_post_counts",
                return_value={"xlsx": Path("hashtags.xlsx")},
            ) as patched_writer,
        ):
            rows, _paths, summaries = asyncio.run(collect_hashtag_count_report(
                Path("data_web"), ["fashion", "beauty"], profile_dir=Path("profile"),
            ))

        self.assertEqual(android_calls, [["fashion"], ["beauty"]])
        self.assertEqual(android_progress, [(1, 2), (2, 2)])
        self.assertEqual(web_calls, ["fashion", "shared", "beauty"])
        self.assertEqual([row["media_count"] for row in rows], [7, 6, 6, 6])
        self.assertEqual([summary["query_hashtag"] for summary in summaries], ["fashion", "beauty"])
        self.assertGreaterEqual(patched_writer.call_count, 3)
        self.assertTrue(all(
            call.kwargs.get("replace_matching_snapshots") is True
            for call in patched_writer.call_args_list
        ))


if __name__ == "__main__":
    unittest.main()
