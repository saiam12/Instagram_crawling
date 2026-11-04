import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from src.exporters.instagram_collector import write_xlsx_workbook, read_reel_records_from_xlsx
from src.reels.refresh_inbox import RefreshInbox, closed_workbook
from src.reels.fashion_beauty_collection import read_history
from src.reels.fashion_beauty_scheduler import DatasetConfig
from scripts import instagram_reels_python as launcher


class RefreshInboxTests(unittest.TestCase):
    def setUp(self):
        settings_root = self.enterContext(tempfile.TemporaryDirectory())
        self.local_settings = Path(settings_root) / 'refresh.local.json'
        self.enterContext(patch('src.reels.refresh_inbox.REFRESH_LOCAL_SETTINGS', self.local_settings))

    def set_hourly_limit(self, value):
        self.local_settings.write_text(json.dumps({'max_collections_per_hour': value}), encoding='utf-8')

    def test_machine_limit_allows_exactly_700_and_retains_excess_input(self):
        self.set_hourly_limit(700)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.prepare(root)
            inbox = RefreshInbox(source, root)
            inbox.record_batch(60, 100)  # Automatic estimate: 4,800/hour.
            inbox.update_workload({'https://www.instagram.com/reels/existing/': 699}, 1)
            inbox.poll()
            self.assertEqual(len(inbox.drain()), 1)
            self.assertEqual([r['url'] for r in read_reel_records_from_xlsx(source)], ['https://www.instagram.com/reels/input1/'])
            self.assertEqual(len(read_history(DatasetConfig('refresh', root, ()))), 1)
            self.assertEqual(inbox.capacity_status()['hourly_capacity'], 700)

    def test_hourly_limit_counts_recollection_slots_instead_of_input_rows(self):
        self.set_hourly_limit(700)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.prepare(root)
            inbox = RefreshInbox(source, root)
            inbox.record_batch(60, 100)
            inbox.update_workload({'https://www.instagram.com/reels/existing/': 698}, 2)
            inbox.poll()
            self.assertEqual(len(inbox.drain()), 1)
            self.assertEqual(len(read_reel_records_from_xlsx(source)), 1)

    def test_machine_limit_does_not_raise_lower_measured_capacity(self):
        self.set_hourly_limit(700)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = RefreshInbox(self.prepare(root), root)
            inbox.record_batch(120, 10)
            self.assertEqual(inbox.capacity_status()['hourly_capacity'], 240)

    def test_machine_limit_applies_before_measurement(self):
        self.set_hourly_limit(1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.prepare(root)
            inbox = RefreshInbox(source, root)
            inbox.poll()
            self.assertEqual(len(inbox.drain()), 1)
            self.assertEqual(len(read_reel_records_from_xlsx(source)), 1)

    def test_limit_edit_rechecks_input_without_resaving_workbook(self):
        self.set_hourly_limit(1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.prepare(root)
            inbox = RefreshInbox(source, root)
            inbox.record_batch(60, 100)
            inbox.poll()
            self.assertEqual(len(inbox.drain()), 1)
            inbox.update_workload({'https://www.instagram.com/reels/input0/': 1}, 1)
            inbox.poll()
            unchanged = source.read_bytes()
            inbox.poll()
            self.assertEqual(source.read_bytes(), unchanged)
            self.set_hourly_limit(2)
            inbox.poll()
            self.assertEqual(len(inbox.drain()), 2)
            self.assertEqual(read_reel_records_from_xlsx(source), [])

    def test_invalid_limit_never_clears_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.prepare(root)
            original = source.read_bytes()
            for value in (True, 0, -1, 1.5, '700'):
                with self.subTest(value=value):
                    self.set_hourly_limit(value)
                    with self.assertRaises(ValueError):
                        RefreshInbox(source, root)
                    self.assertEqual(source.read_bytes(), original)

    def test_other_computers_do_not_inherit_the_700_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.prepare(root)
            for use_null in (False, True):
                if use_null:
                    self.set_hourly_limit(None)
                inbox = RefreshInbox(source, root)
                inbox.record_batch(60, 100)
                self.assertEqual(inbox.capacity_status()['hourly_capacity'], 4800)
                self.assertIsNone(inbox.capacity_status()['max_collections_per_hour'])

    def test_invalid_live_settings_preserve_input_and_queue(self):
        self.set_hourly_limit(700)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.prepare(root)
            inbox = RefreshInbox(source, root)
            original = source.read_bytes()
            self.local_settings.write_text('{', encoding='utf-8')
            with self.assertRaises(ValueError):
                inbox.poll()
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(inbox.jobs, [])

    def test_first_import_uses_configured_interval_before_admission(self):
        self.set_hourly_limit(700)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.prepare(root, 10)
            with patch.object(launcher, 'refresh_until_collection_count', return_value=0) as scheduled:
                result = launcher.main(['refresh', '--inbox', '--input-xlsx', str(source), '--data-dir', str(root),
                                        '--interval-hours', str(1 / 120)])
            self.assertEqual(result, 0)
            self.assertEqual(len(scheduled.call_args.args[1]), 5)  # 5 x 120 = 600; sixth would exceed 700.
            self.assertEqual(len(read_reel_records_from_xlsx(source)), 5)

    def test_restart_does_not_trust_old_lower_frequency_reservations(self):
        self.set_hourly_limit(700)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.prepare(root, 10)
            existing = 'https://www.instagram.com/reels/existing/'
            old = RefreshInbox(source, root)
            old.jobs = [{'id': 'previous', 'state': 'registered', 'urls': [existing]}]
            old.save()
            old.update_workload({existing: 1}, 1)
            restarted = RefreshInbox(source, root, new_url_cost=120)
            restarted.poll()
            accepted = [url for job in restarted.jobs if job['state'] == 'queued' for url in job['urls']]
            self.assertEqual(len(accepted), 4)  # Existing URL + four new URLs reserve 600/hour.
            self.assertEqual(len(read_reel_records_from_xlsx(source)), 6)
            self.assertEqual(restarted.jobs[0]['urls'], [existing])

    def test_collector_batch_reports_real_saved_count_and_elapsed_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            url = 'https://www.instagram.com/reels/target/'
            rows = [{'url': url, 'collected_at': '2020-01-01T00:00:00Z', 'collection_number': 1}]
            inbox = Mock()
            inbox.drain.return_value = [url]
            inbox.cancelled_urls.return_value = set()
            def collect(arguments):
                rows.append({'url': url, 'collected_at': '2026-10-29T00:00:00Z', 'collection_number': 2})
                return 0
            with patch.object(launcher, 'read_history', side_effect=lambda _: list(rows)), patch.object(launcher, 'collector_main', side_effect=collect), patch.object(launcher.time, 'sleep', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    launcher.refresh_until_collection_count(DatasetConfig('refresh', root, ()), [url], [], root / 'urls.txt', 2, 4, False, schedule_basis='last-collected', inbox=inbox)
            inbox.record_batch.assert_called_once()
            self.assertGreater(inbox.record_batch.call_args.args[0], 0)
            self.assertEqual(inbox.record_batch.call_args.args[1], 1)

    def test_measured_hourly_capacity_counts_reserved_recollections(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.prepare(root, 15)
            inbox = RefreshInbox(source, root)
            inbox.record_batch(120, 10)
            self.assertEqual(inbox.capacity_status()['hourly_capacity'], 240)
            inbox.update_workload({f'https://www.instagram.com/reels/reserved{i}/': 1 for i in range(235)}, 1)
            inbox.poll()
            self.assertEqual(len(inbox.drain()), 5)
            self.assertEqual(len(read_reel_records_from_xlsx(source)), 10)
            inbox.update_workload({}, 1)
            inbox.poll()  # Capacity change rechecks unedited remaining input.
            self.assertEqual(len(inbox.drain()), 15)
            self.assertEqual(read_reel_records_from_xlsx(source), [])

    def test_five_minute_workload_reduces_admission_and_measurement_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.prepare(root, 15)
            inbox = RefreshInbox(source, root)
            inbox.record_batch(240, 10)  # 120 snapshots/hour after margin.
            inbox.update_workload({}, 12)  # Every five minutes uses twelve snapshots/hour per Reel.
            inbox.poll()
            self.assertEqual(len(inbox.drain()), 10)
            self.assertEqual(len(read_reel_records_from_xlsx(source)), 5)
            self.assertEqual(RefreshInbox(source, root).capacity_status()['hourly_capacity'], 120)

    def test_no_successes_does_not_claim_positive_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.prepare(root)
            inbox = RefreshInbox(source, root)
            inbox.record_batch(60, 0)
            inbox.poll()
            self.assertEqual(len(read_reel_records_from_xlsx(source)), 2)
            self.assertEqual(inbox.drain(), [])
    def prepare(self, root, count=2):
        source = root / 'input.xlsx'
        write_xlsx_workbook(source, [('reels', [['url', 'collected_at', 'uploaded_at', 'collection_number', 'like_count'],
            *[[f'https://www.instagram.com/reels/input{i}/', '2026-10-29T06:00:00Z', '2026-10-29T04:00:00Z', '1', '10'] for i in range(count)]])])
        return source

    def test_closed_input_is_cleared_only_after_durable_queue_and_restart_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.prepare(root)
            inbox = RefreshInbox(source, root)
            inbox.poll()
            self.assertEqual(read_reel_records_from_xlsx(source), [])
            self.assertEqual(len(json.loads(inbox.state_path.read_text())[0]['urls']), 2)
            restarted = RefreshInbox(source, root)
            self.assertEqual(len(restarted.drain()), 2)
            rows = read_history(DatasetConfig('refresh', root, ()))
            self.assertEqual(len(rows), 2)
            self.assertTrue((root / 'reels.xlsx').exists())
            self.assertEqual(len(restarted.drain()), 2)
            self.assertEqual(len(read_history(DatasetConfig('refresh', root, ()))), 2)

    def test_launcher_passes_registered_queue_and_correct_age_basis(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.prepare(root)
            with patch.object(launcher, 'refresh_until_collection_count', return_value=0) as scheduled:
                self.assertEqual(launcher.main(['refresh', '--inbox', '--input-xlsx', str(source), '--data-dir', str(root), '--age-schedule', '--last-collected']), 0)
            self.assertEqual(len(scheduled.call_args.args[1]), 2)
            self.assertEqual(scheduled.call_args.kwargs['age_basis'], 'first-collected')
            self.assertIsNotNone(scheduled.call_args.kwargs['inbox'])
            self.assertEqual(read_reel_records_from_xlsx(source), [])
            self.assertTrue((root / 'reels.xlsx').exists())

    def test_excel_owner_file_defers_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.prepare(root)
            owner = source.with_name('~$' + source.name)
            owner.touch()
            inbox = RefreshInbox(source, root)
            with self.assertRaises(PermissionError): inbox.poll()
            self.assertEqual(len(read_reel_records_from_xlsx(source)), 2)
            self.assertFalse(inbox.state_path.exists())
            owner.unlink()
            inbox.poll()
            self.assertEqual(read_reel_records_from_xlsx(source), [])

    def test_queue_write_failure_keeps_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.prepare(root)
            original = source.read_bytes()
            inbox = RefreshInbox(source, root)
            with patch.object(inbox, 'save', side_effect=OSError('disk full')):
                with self.assertRaises(OSError): inbox.poll()
            self.assertEqual(source.read_bytes(), original)
            inbox.poll()
            self.assertTrue(inbox.state_path.exists())
            self.assertEqual(read_reel_records_from_xlsx(source), [])

    def test_test_limit_retains_unaccepted_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.prepare(root, 15)
            inbox = RefreshInbox(source, root, limit=10)
            inbox.poll()
            self.assertEqual(len(read_reel_records_from_xlsx(source)), 5)
            self.assertEqual(len(inbox.drain()), 10)

    def test_same_input_as_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError): RefreshInbox(root / 'reels.xlsx', root)

    def test_shortcode_cancellation_persists_and_keeps_saved_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.prepare(root)
            inbox = RefreshInbox(source, root)
            inbox.poll()
            self.assertEqual(len(inbox.drain()), 2)
            history = read_history(DatasetConfig('refresh', root, ()))
            write_xlsx_workbook(source, [('reels', [['url']]), ('수집종료', [['shortcode'], ['input0'], ['https://www.instagram.com/reels/unknown/'], ['invalid value']])])
            inbox.poll()
            self.assertEqual(inbox.drain(), ['https://www.instagram.com/reels/input1/'])
            self.assertEqual(read_history(DatasetConfig('refresh', root, ())), history)
            self.assertEqual([row['shortcode'] for row in read_reel_records_from_xlsx(source, sheet_name='수집종료')], ['invalid value'])
            restarted = RefreshInbox(source, root)
            self.assertNotIn('https://www.instagram.com/reels/input0/', restarted.drain())

    def test_cancellation_before_import_takes_priority_over_new_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'input.xlsx'
            write_xlsx_workbook(source, [('reels', [['url', 'collected_at'], ['https://www.instagram.com/reels/input0/', '2026-10-29T06:00:00Z']]), ('수집종료', [['shortcode'], ['input0']])])
            inbox = RefreshInbox(source, root)
            inbox.poll()
            self.assertEqual(inbox.drain(), [])
            self.assertEqual(read_history(DatasetConfig('refresh', root, ())), [])
            self.assertEqual(len(read_reel_records_from_xlsx(source)), 1)

    def test_actual_exclusive_file_handle_defers_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.prepare(root)
            inbox = RefreshInbox(source, root)
            with closed_workbook(source):
                with self.assertRaises(PermissionError): inbox.poll()
                self.assertFalse(inbox.state_path.exists())
            inbox.poll()
            self.assertEqual(len(inbox.drain()), 2)

    def test_background_watcher_accepts_new_rows_after_close(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.prepare(root)
            accepted = threading.Event()
            inbox = RefreshInbox(source, root)
            with patch('src.reels.refresh_inbox.append_collection_log', side_effect=lambda *a, **k: accepted.set()):
                inbox.start()
                try:
                    self.assertTrue(accepted.wait(5))
                    self.assertEqual(len(inbox.drain()), 2)
                    owner = source.with_name('~$' + source.name)
                    owner.touch()
                    accepted.clear()
                    self.prepare(root, 3)
                    self.assertFalse(accepted.wait(.2))
                    owner.unlink()
                    self.assertTrue(accepted.wait(5))
                    self.assertEqual(len(inbox.drain()), 3)
                    self.assertEqual(read_reel_records_from_xlsx(source), [])
                finally:
                    inbox.close()
