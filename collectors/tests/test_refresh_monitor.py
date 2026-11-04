import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'monitor' / 'backend'))
import state as monitor_state
import server as monitor_server


class RefreshMonitorTests(unittest.TestCase):
    def test_refresh_runtime_preserves_saved_run_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '.collector').mkdir()
            saved = {'started_at': '2026-10-29T06:00:00Z', 'finished_at': '2026-10-29T07:00:00Z',
                     'state': 'completed', 'age_intervals': [1, 3, 6, 12], 'age_basis': 'first-collected',
                     'schedule_basis': 'last-collected', 'interval_hours': 4, 'test_mode': False}
            (root / '.collector/refresh_status.json').write_text(json.dumps(saved))
            runtime = monitor_state.refresh_summary(root)['runtime']
            self.assertEqual(runtime['age_intervals'], [1, 3, 6, 12])
            self.assertEqual(runtime['finished_at'], saved['finished_at'])
            self.assertEqual(runtime['scope'], 'run')

    def test_legacy_runtime_is_explicitly_a_batch_not_a_refresh_session(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'collector_status.json').write_text(json.dumps({
                'started_at': '2026-10-29T06:00:00Z', 'finished_at': '2026-10-29T06:04:00Z'}))
            runtime = monitor_state.refresh_summary(root)['runtime']
            self.assertEqual(runtime['scope'], 'batch')
            self.assertIsNone(runtime['age_intervals'])
            self.assertEqual(runtime['finished_at'], '2026-10-29T06:04:00Z')

    def test_discovers_refresh_separately_and_keeps_collector_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder) / 'data'
            for name in ('collector', 'refresh_test', 'data_imported', 'data_other'):
                (base / name / '.collector').mkdir(parents=True)
            (base / 'data_imported/.collector/refresh_inbox').mkdir()
            with patch.object(monitor_state, 'COLLECTORS', base.parent):
                items = monitor_state.Snapshot(base / 'collector').datasets([])
            sources = {item['id'].removeprefix('data/'): item['source'] for item in items}
            self.assertEqual(sources, {'collector': 'collector', 'refresh_test': 'refresh', 'data_imported': 'refresh'})

    def test_refresh_queue_deduplicates_and_applies_cancellations(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inbox = root / '.collector/refresh_inbox'
            inbox.mkdir(parents=True)
            (inbox / 'queue.json').write_text(json.dumps([
                {'state': 'registered', 'urls': ['A', 'B']},
                {'state': 'queued', 'urls': ['B', 'C', 'C']},
                {'kind': 'cancel', 'state': 'cancelled', 'urls': ['B']},
            ]))
            (inbox / 'capacity.json').write_text(json.dumps({'status': {'measured': True, 'hourly_capacity': 48}, 'reservations': {'A': 2}}))
            result = monitor_state.refresh_summary(root)
            self.assertEqual((result['registered'], result['queued'], result['cancelled']), (1, 1, 1))
            self.assertEqual(result['hourly_capacity'], 48)
            self.assertEqual(result['reserved'], 2)

    def test_legacy_refresh_has_unknown_inbox_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            result = monitor_state.refresh_summary(Path(folder))
            self.assertFalse(result['has_inbox'])
            self.assertIsNone(result['queued'])
            self.assertIsNone(result['hourly_capacity'])

    def test_partial_queue_write_does_not_break_other_dataset_status(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inbox = root / '.collector/refresh_inbox'
            inbox.mkdir(parents=True)
            for raw in ('{', '{"not": "a list"}', '[{"urls": null}]'):
                (inbox / 'queue.json').write_text(raw)
                result = monitor_state.refresh_summary(root)
                self.assertIsNone(result['queued'])
                self.assertIn('읽지 못했습니다', result['warning'])

    def test_data_and_events_cannot_cross_source_tabs(self):
        selected = {'id': 'refresh_test', 'source': 'refresh', 'logs': []}
        monitor = SimpleNamespace(lock=threading.RLock(), details={'refresh_test': selected}, events=[])
        for endpoint in ('data', 'events'):
            handler = object.__new__(monitor_server.Handler)
            handler.server = SimpleNamespace(monitor=monitor)
            handler.path = f'/api/{endpoint}?source=collector&dataset=refresh_test'
            with patch.object(handler, 'allowed_host', return_value=True), patch.object(handler, 'send') as send:
                handler.do_GET()
            self.assertEqual(send.call_args.args[0]['items'], [])

    def test_overview_filters_sources_and_validates_selection(self):
        monitor = object.__new__(monitor_server.Monitor)
        monitor.lock = threading.RLock()
        monitor.details = {'a': {'source': 'collector'}, 'b': {'source': 'refresh'}}
        monitor.updated_at, monitor.last_error = '', ''
        monitor.snapshot = type('Snapshot', (), {'root': monitor_server.COLLECTORS / 'collector'})()
        with patch.object(monitor, 'jobs', return_value=[]), patch.object(monitor_server, 'project_korean', side_effect=lambda item, _: item):
            for item in monitor.details.values():
                item['web'] = {'process': {'alive': False, 'pid': 0}}
            self.assertEqual([d['source'] for d in monitor.overview(source='collector')['datasets']], ['collector'])
            self.assertEqual([d['source'] for d in monitor.overview(source='refresh')['datasets']], ['refresh'])
            with self.assertRaises(ValueError):
                monitor.overview(source='arbitrary')
