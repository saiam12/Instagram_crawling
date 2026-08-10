import csv
import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from instagram_collector import (
    APIUsage,
    DataStore,
    ExperimentManager,
    GraphAPIError,
    InstagramCollector,
    Target,
    TargetRegistry,
    read_reel_urls_from_xlsx,
    _xlsx_cell,
    _xlsx_days_since_upload,
    _xlsx_metric_with_delta,
    _xlsx_recollect_collected_at,
)


class FakeClient:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.stop_before_next = False
        self.contexts = []

    def get(self, path, params, context):
        self.contexts.append((path, params, context))
        return next(self.pages)


class CollectorTests(unittest.TestCase):
    def test_xlsx_sync_can_write_to_alternate_output(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DataStore(Path(directory))
            store.data_dir.mkdir(parents=True, exist_ok=True)
            (store.data_dir / "users.csv").write_text(
                "user_id,username,follower_count\n123,example,456\n", encoding="utf-8"
            )
            alternate = store.data_dir / "instagram_data_updated.xlsx"

            store.sync_xlsx(alternate)

            self.assertTrue(alternate.exists())
            self.assertFalse(store.workbook.exists())
            with zipfile.ZipFile(alternate) as workbook:
                sheet_xml = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")
            self.assertIn("follower_count", sheet_xml)
            self.assertIn(">456<", sheet_xml)

    def test_xlsx_lock_error_explains_that_csv_data_is_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DataStore(Path(directory))
            store.data_dir.mkdir(parents=True, exist_ok=True)
            (store.data_dir / "users.csv").write_text(
                "user_id,username\n123,example\n", encoding="utf-8"
            )
            with patch(
                "instagram_collector.write_xlsx_workbook",
                side_effect=PermissionError(5, "Access is denied"),
            ):
                with self.assertRaisesRegex(PermissionError, "open in Excel"):
                    store.sync_xlsx()

    def test_users_xlsx_sheet_keeps_only_requested_columns_in_requested_order(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DataStore(Path(directory))
            store.data_dir.mkdir(parents=True, exist_ok=True)
            (store.data_dir / "users.csv").write_text(
                "user_id,username,first_seen_at,last_seen_at,follower_count,"
                "follower_count_collected_at,follower_source,api_user_id,"
                "lookup_status,last_lookup_at,last_error\n"
                "123,example,old,new,456,2026-08-03T00:00:00Z,graph_api,"
                "17841400000000000,success,new,none\n",
                encoding="utf-8",
            )

            store.sync_xlsx()

            with zipfile.ZipFile(store.workbook) as workbook:
                sheet_xml = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")
            expected = [
                "user_id",
                "username",
                "follower_count_collected_at",
                "follower_count",
                "api_user_id",
                "lookup_status",
            ]
            positions = [sheet_xml.index(header) for header in expected]
            self.assertEqual(positions, sorted(positions))
            for hidden in (
                "first_seen_at",
                "last_seen_at",
                "follower_source",
                "last_lookup_at",
                "last_error",
            ):
                self.assertNotIn(hidden, sheet_xml)

    def test_reels_xlsx_sheet_reorders_and_filters_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DataStore(Path(directory))
            store.data_dir.mkdir(parents=True, exist_ok=True)
            (store.data_dir / "reels_web.csv").write_text(
                "collected_at,url,user_id,username,title,hashtags,audio_name,"
                "location_name,location_latitude,location_longitude,ad,uploaded_at,"
                "days_since_upload,like_count,comment_count,repost_count,follower_count,"
                "reaction_rate,follower_count_collected_at,follower_lookup_status,"
                "2nd collect_collected_at,2nd collect_days_since_upload,"
                "2nd collect_like_count,2nd collect_reaction_rate,3rd collect_collected_at,"
                "3rd collect_days_since_upload,3rd collect_like_count\n"
                "2026-08-03T00:00:00Z,https://example.test/reel,,,creator_name,,,"
                "Seoul,37.5,127.0,false,,1.25,1,2,3,,0.25,,api_error,"
                "2026-08-03T02:00:00Z,1.33,5,0.3,"
                "2026-08-03T04:00:00Z,1.42,9\n",
                encoding="utf-8",
            )

            store.sync_xlsx()

            with zipfile.ZipFile(store.workbook) as workbook:
                sheet_xml = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")
            expected = [
                "url",
                "collected_at",
                "user_id",
                "username",
                "title",
                "days_since_upload",
                "reaction_rate",
                "follower_count_collected_at",
            ]
            positions = [sheet_xml.index(header) for header in expected]
            self.assertEqual(positions, sorted(positions))
            self.assertNotIn("location_name", sheet_xml)
            self.assertNotIn("location_latitude", sheet_xml)
            self.assertNotIn("location_longitude", sheet_xml)
            self.assertNotIn("follower_lookup_status", sheet_xml)
            self.assertIn("creator_name", sheet_xml)
            self.assertIn("api_error", sheet_xml)
            self.assertIn("2nd collect_collected_at", sheet_xml)
            self.assertIn("2nd collect_reaction_rate", sheet_xml)
            self.assertIn("2026-08-03 11:00:00 (+2Hour)", sheet_xml)
            self.assertIn("+1day", sheet_xml)
            self.assertIn("5(+4)", sheet_xml)
            self.assertIn("9(+4)", sheet_xml)

    def test_follower_lookups_xlsx_hides_source_and_status(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DataStore(Path(directory))
            store.data_dir.mkdir(parents=True, exist_ok=True)
            (store.data_dir / "follower_lookups.csv").write_text(
                "collected_at,user_id,username,api_user_id,follower_count,"
                "source,lookup_status,error\n"
                "2026-08-03T00:00:00Z,123,example,17841400000000000,456,"
                "graph_api,api_error,Invalid user id\n",
                encoding="utf-8",
            )

            store.sync_xlsx()

            with zipfile.ZipFile(store.workbook) as workbook:
                sheet_xml = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")
            expected = [
                "collected_at",
                "user_id",
                "username",
                "api_user_id",
                "follower_count",
                "error",
            ]
            positions = [sheet_xml.index(header) for header in expected]
            self.assertEqual(positions, sorted(positions))
            self.assertNotIn("source", sheet_xml)
            self.assertNotIn("lookup_status", sheet_xml)
            self.assertIn("Invalid user id", sheet_xml)

    def test_xlsx_writes_counts_as_numbers_and_user_ids_as_text(self):
        self.assertEqual(
            _xlsx_cell("E2", "12345", "follower_count", False),
            '<c r="E2" s="4"><v>12345</v></c>',
        )
        identifier_cell = _xlsx_cell("A2", "17841443137530278", "user_id", False)
        self.assertIn(
            't="inlineStr"',
            identifier_cell,
        )
        self.assertIn('s="3"', identifier_cell)
        self.assertEqual(
            _xlsx_cell("O2", "125", "2nd collect_like_count", False),
            '<c r="O2" s="4"><v>125</v></c>',
        )
        self.assertEqual(
            _xlsx_cell("P2", "1.25", "2nd collect_days_since_upload", False),
            '<c r="P2" s="6"><v>1.25</v></c>',
        )
        self.assertEqual(
            _xlsx_cell("Q2", "37.5445", "location_latitude", False),
            '<c r="Q2" s="5"><v>37.5445</v></c>',
        )
        self.assertEqual(
            _xlsx_cell("R2", "0.245098", "2nd collect_reaction_rate", False),
            '<c r="R2" s="7"><v>0.245098</v></c>',
        )

    def test_xlsx_dates_are_displayed_in_korean_standard_time(self):
        cell = _xlsx_cell(
            "A2", "2026-08-03T00:00:00.000Z", "collected_at", False
        )
        serial = float(cell.split("<v>", 1)[1].split("</v>", 1)[0])
        expected = (
            datetime(2026, 8, 3, 9, 0, 0) - datetime(1899, 12, 30)
        ).total_seconds() / 86400
        self.assertAlmostEqual(serial, expected, places=8)
        self.assertEqual(
            _xlsx_recollect_collected_at(
                "2026-08-01T00:00:00.000Z", "2026-08-01T02:00:00.000Z"
            ),
            "2026-08-01 11:00:00 (+2Hour)",
        )

    def test_xlsx_recollect_metrics_show_change_from_previous_actual_value(self):
        self.assertEqual(_xlsx_metric_with_delta("21", "17"), "21(+4)")
        self.assertEqual(_xlsx_metric_with_delta("29", "21"), "29(+8)")
        self.assertEqual(_xlsx_metric_with_delta("100", "110"), "100(-10)")
        self.assertEqual(_xlsx_metric_with_delta("1500", "1490"), "1,500(+10)")
        self.assertEqual(_xlsx_metric_with_delta("1500", ""), "1,500")

    def test_xlsx_days_since_upload_truncates_decimals(self):
        self.assertEqual(_xlsx_days_since_upload("12.27"), "+12day")
        self.assertEqual(_xlsx_days_since_upload("0.84"), "+20hours")
        self.assertEqual(_xlsx_days_since_upload("0"), "+0hours")
        self.assertEqual(_xlsx_days_since_upload("166.01"), "+166day")
        self.assertEqual(_xlsx_days_since_upload(""), "")

    def test_reel_urls_are_read_from_xlsx_in_row_order_without_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DataStore(Path(directory))
            store.data_dir.mkdir(parents=True, exist_ok=True)
            (store.data_dir / "reels_web.csv").write_text(
                "collected_at,url,user_id\n"
                "2026-08-01T00:00:00Z,https://www.instagram.com/reel/AAA111/,1\n"
                "2026-08-02T00:00:00Z,https://www.instagram.com/reels/BBB222/,2\n"
                "2026-08-03T00:00:00Z,https://www.instagram.com/reels/AAA111/,1\n",
                encoding="utf-8",
            )
            store.sync_xlsx()

            self.assertEqual(
                read_reel_urls_from_xlsx(store.workbook),
                [
                    "https://www.instagram.com/reels/AAA111/",
                    "https://www.instagram.com/reels/BBB222/",
                ],
            )

    def test_usage_header_is_flattened(self):
        usage = APIUsage()
        headers = {
            "X-Business-Use-Case-Usage": json.dumps(
                {
                    "123": [
                        {
                            "type": "instagram",
                            "call_count": 42,
                            "total_cputime": 11,
                            "total_time": 9,
                            "estimated_time_to_regain_access": 3,
                        }
                    ]
                }
            )
        }
        self.assertTrue(usage.update(headers))
        self.assertEqual(usage.max_percent, 42)
        self.assertEqual(usage.estimated_time_to_regain_access, 3)

    def test_business_discovery_paginates_and_deduplicates(self):
        target = Target("sample", "business_discovery", "sample", True, "", "now")
        pages = [
            {
                "business_discovery": {
                    "id": "10",
                    "username": "sample",
                    "followers_count": 100,
                    "media": {
                        "data": [{"id": "p1", "like_count": 2}],
                        "paging": {"cursors": {"after": "cursor1"}},
                    },
                }
            },
            {
                "business_discovery": {
                    "id": "10",
                    "username": "sample",
                    "followers_count": 101,
                    "media": {"data": [{"id": "p1"}, {"id": "p2"}]},
                }
            },
        ]
        client = FakeClient(pages)
        result = InstagramCollector(client, "owner-id").fetch_target(target, 0, 100)
        self.assertTrue(result.complete)
        self.assertEqual({post["id"] for post in result.posts}, {"p1", "p2"})
        self.assertIn(".after(cursor1)", client.contexts[1][1]["fields"])

    def test_facebook_login_request_omits_unsupported_biography(self):
        target = Target("self", "authorized", "", True, "", "now")
        fields = InstagramCollector._build_fields(target, 100, None)
        self.assertNotIn("biography", fields)
        self.assertNotIn("website", fields)
        self.assertNotIn("profile_picture_url", fields)
        self.assertIn("followers_count", fields)

    def test_collector_retries_without_an_unsupported_field(self):
        class UnsupportedFieldClient:
            def __init__(self):
                self.stop_before_next = False
                self.requests = []

            def get(self, path, params, context):
                self.requests.append((path, params["fields"]))
                if "follows_count" in params["fields"]:
                    raise GraphAPIError(
                        "Graph API request failed (400): (#100) "
                        "Tried accessing nonexisting field (follows_count)"
                    )
                return {
                    "id": "10",
                    "username": "sample",
                    "followers_count": 100,
                }

        client = UnsupportedFieldClient()
        target = Target("self", "authorized", "", True, "", "now")
        result = InstagramCollector(client, "owner-id").fetch_target(target, 0, 100)
        self.assertTrue(result.complete)
        self.assertEqual(len(client.requests), 3)
        self.assertNotIn("follows_count", client.requests[1][1])
        self.assertEqual(client.requests[-1][0], "/owner-id/media")

    def test_authorized_collection_uses_the_media_edge(self):
        target = Target("self", "authorized", "", True, "", "now")
        client = FakeClient(
            [
                {"id": "10", "username": "sample", "followers_count": 100},
                {"data": [{"id": "post-1", "like_count": 2}]},
            ]
        )
        result = InstagramCollector(client, "owner-id").fetch_target(target, 0, 100)
        self.assertTrue(result.complete)
        self.assertEqual(client.contexts[0][0], "/owner-id")
        self.assertEqual(client.contexts[1][0], "/owner-id/media")
        self.assertEqual(client.contexts[1][1]["limit"], 100)

    def test_registry_and_timeseries_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = TargetRegistry(root / "targets.csv")
            target = registry.add("Example.User", False, "test")
            self.assertEqual(target.target_key, "example.user")
            self.assertEqual(len(registry.select(None)), 1)

            result = type(
                "Result",
                (),
                {
                    "target": target,
                    "account": {
                        "username": "example.user",
                        "followers_count": 10,
                        "media_count": 1,
                    },
                    "posts": [
                        {
                            "id": "post-1",
                            "username": "example.user",
                            "caption": "hello",
                            "timestamp": "2026-01-01T00:00:00Z",
                            "like_count": 5,
                            "comments_count": 2,
                        }
                    ],
                },
            )()
            store = DataStore(root / "data")
            store.persist_result(result, "run-1", "2026-01-02T00:00:00Z")
            store.persist_result(result, "run-2", "2026-01-03T00:00:00Z")

            with store.media_snapshots.open(encoding="utf-8-sig") as file:
                snapshots = list(csv.DictReader(file))
            with store.posts.open(encoding="utf-8-sig") as file:
                posts = list(csv.DictReader(file))
            self.assertEqual(len(snapshots), 2)
            self.assertEqual(len(posts), 1)
            self.assertEqual(posts[0]["first_seen_at"], "2026-01-02T00:00:00Z")
            self.assertEqual(posts[0]["last_seen_at"], "2026-01-03T00:00:00Z")

            store.persist_run(
                {
                    "run_id": "run-2",
                    "status": "success",
                    "targets_requested": 1,
                    "targets_completed": 1,
                }
            )
            (store.data_dir / "reels_web_legacy_20260802T000000Z.csv").write_text(
                "old_header\nold_value\n", encoding="utf-8"
            )
            store.sync_xlsx()
            self.assertTrue(store.workbook.exists())
            with zipfile.ZipFile(store.workbook) as workbook:
                self.assertIn("xl/workbook.xml", workbook.namelist())
                workbook_xml = workbook.read("xl/workbook.xml").decode("utf-8")
                first_sheet = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")
            self.assertIn("account_timeseries", workbook_xml)
            self.assertNotIn("reels_web_legacy", workbook_xml)
            self.assertIn("followers_count", first_sheet)
            self.assertIn('s="2"', first_sheet)

    def test_random_experiment_creates_test_milestones(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = TargetRegistry(root / "targets.csv")
            registry.import_usernames(["one", "two", "three"])
            manager = ExperimentManager(DataStore(root / "data"))
            experiment, selected, jobs = manager.start(
                registry.load(),
                sample_size=2,
                schedule_name="test",
                offsets=None,
                seed=1234,
                include_self=False,
            )
            self.assertEqual(len(selected), 2)
            self.assertEqual(len(jobs), 10)
            self.assertEqual(
                {job["milestone"] for job in jobs},
                {"baseline", "1h", "4h", "12h", "24h"},
            )
            due = manager.due_jobs(
                experiment["experiment_id"],
                max_jobs=0,
                now=datetime.now(timezone.utc) + timedelta(minutes=1),
            )
            self.assertEqual(len(due), 2)
            self.assertTrue(all(job["milestone"] == "baseline" for job in due))


if __name__ == "__main__":
    unittest.main()
