import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pool import local as key_pool
import reel_analyzer as analyzer
from google.genai.errors import ClientError


def quota(message):
    return ClientError(429, {'error': {'message': message, 'status': 'RESOURCE_EXHAUSTED'}})


class PoolTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for name, value in [('STATE_PATH', Path(temp.name)/'state.json'),
                            ('LIMITS_PATH', Path(temp.name)/'limits.json')]:
            patcher = patch.object(key_pool, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.dict('os.environ', {'GEMINI_API_KEYS': 'fake-a,fake-b',
                            'GEMINI_API_KEY_LABELS': 'a,b', 'GEMINI_MODELS': 'm1,m2'})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pool = key_pool.GeminiKeyPool()

    def test_braced_alias_key_mapping(self):
        with patch.dict('os.environ', {
            'GEMINI_API_KEYS': '{acct1:secret-a,acct2:secret-b}',
            'GEMINI_API_KEY_LABELS': '',
        }):
            self.assertEqual(key_pool.GeminiKeyPool._load_keys(), [
                ('acct1', 'secret-a'), ('acct2', 'secret-b'),
            ])

    def test_multiline_braced_alias_key_mapping(self):
        with patch.dict('os.environ', {
            'GEMINI_API_KEYS': '{acct1:secret-a,\nacct2:secret-b}',
            'GEMINI_API_KEY_LABELS': '',
        }):
            self.assertEqual(key_pool.GeminiKeyPool._load_keys(), [
                ('acct1', 'secret-a'), ('acct2', 'secret-b'),
            ])

    def test_empty_braced_mapping_allows_initial_shared_key_pull(self):
        with patch.dict('os.environ', {
            'GEMINI_API_KEYS': '{}', 'GEMINI_API_KEY': '',
        }):
            self.assertEqual(key_pool.GeminiKeyPool._load_keys(), [])

    def test_braced_mapping_rejects_duplicate_aliases(self):
        with patch.dict('os.environ', {
            'GEMINI_API_KEYS': '{acct1:secret-a,acct1:secret-b}',
        }):
            with self.assertRaises(RuntimeError):
                key_pool.GeminiKeyPool._load_keys()

    def test_stays_until_exhausted_then_models_then_keys(self):
        self.assertEqual(self.pool.acquire().combo_id, 'a:m1')
        self.assertEqual(self.pool.acquire().combo_id, 'a:m1')
        self.pool.mark_exhausted(self.pool.combos[0])
        self.assertEqual(self.pool.acquire().combo_id, 'a:m2')
        self.pool.mark_exhausted(self.pool.combos[1])
        self.assertEqual(self.pool.acquire().combo_id, 'b:m1')

    def test_minute_quota_recovers(self):
        combo = self.pool.acquire()
        with patch('pool.local.time.time', return_value=1000):
            self.pool.mark_quota_error(combo, quota('RequestsPerMinute retry in 12s'))
            self.assertFalse(self.pool._is_available(combo))
            self.assertLess(self.pool._combo_state(combo)['day_count'], 20)
        with patch('pool.local.time.time', return_value=1013):
            self.assertTrue(self.pool._is_available(combo))

    def test_daily_quota_persists_and_resets(self):
        combo = self.pool.acquire()
        self.pool.mark_quota_error(combo, quota('GenerateRequestsPerDayPerProject'))
        restored = key_pool.GeminiKeyPool()
        self.assertFalse(restored._is_available(restored.combos[0]))
        with patch('pool.local._today_str', return_value='2099-01-01'):
            self.assertTrue(restored._is_available(restored.combos[0]))

    def test_all_daily_exhausted_does_not_sleep(self):
        for combo in self.pool.combos:
            self.pool.mark_exhausted(combo)
        with patch('pool.local.time.sleep') as sleep:
            with self.assertRaises(key_pool.KeyPoolExhaustedError):
                self.pool.acquire_blocking()
            sleep.assert_not_called()

    def test_unknown_quota_is_temporary(self):
        combo = self.pool.acquire()
        self.pool.mark_quota_error(combo, quota('Resource exhausted'))
        self.assertEqual(self.pool._combo_state(combo)['day_count'], 1)

    def test_inline_fallback(self):
        client = Mock()
        client.models.generate_content.side_effect = [quota('PerDay'), SimpleNamespace(text='{"summary":"ok"}')]
        with patch.object(analyzer.genai, 'Client', return_value=client):
            self.assertEqual(analyzer._analyze_inline(self.pool, b'video', 'video/mp4')['summary'], 'ok')
        self.assertEqual([c.kwargs['model'] for c in client.models.generate_content.call_args_list], ['m1', 'm2'])

    def test_files_reuploads_on_new_key(self):
        self.pool.mark_exhausted(self.pool.combos[1])
        clients = [Mock(), Mock()]
        for i, client in enumerate(clients):
            client.files.upload.return_value = SimpleNamespace(name=f'files/{i}', uri=f'https://example.org/{i}', mime_type='video/mp4', state=SimpleNamespace(name='ACTIVE'))
        clients[0].models.generate_content.side_effect = quota('PerDay')
        clients[1].models.generate_content.return_value = SimpleNamespace(text='{"summary":"ok"}')
        with patch.object(analyzer.genai, 'Client', side_effect=clients) as factory:
            self.assertEqual(analyzer._analyze_via_files_api(self.pool, b'video', 'video/mp4')['summary'], 'ok')
        self.assertEqual([c.kwargs['api_key'] for c in factory.call_args_list], ['fake-a', 'fake-b'])
        for i, client in enumerate(clients):
            client.files.upload.assert_called_once()
            client.files.delete.assert_called_once_with(name=f'files/{i}')
            self.assertFalse(Path(client.files.upload.call_args.kwargs['file']).exists())

    def test_invalid_request_stops(self):
        client = Mock()
        call = Mock(side_effect=ClientError(400, {'error': {'message': 'bad schema'}}))
        with patch.object(analyzer.genai, 'Client', return_value=client):
            with self.assertRaises(ClientError):
                analyzer._call_with_pool(self.pool, call)
        self.assertEqual(call.call_count, 1)
        client.close.assert_called_once()

    def test_analyze_video_uses_pool(self):
        client = Mock()
        client.models.generate_content.side_effect = [quota('PerDay'), SimpleNamespace(text='{"summary":"ok"}')]
        with patch.object(analyzer.genai, 'Client', return_value=client):
            self.assertEqual(analyzer.analyze_video(self.pool, b'video')['summary'], 'ok')
        self.assertEqual([c.kwargs['model'] for c in client.models.generate_content.call_args_list], ['m1', 'm2'])

    def test_auth_error_skips_entire_key(self):
        client = Mock()
        call = Mock(side_effect=[ClientError(403, {'error': {'message': 'denied'}}), 'ok'])
        with patch.object(analyzer.genai, 'Client', return_value=client) as factory:
            self.assertEqual(analyzer._call_with_pool(self.pool, call), 'ok')
        self.assertEqual([c.kwargs['api_key'] for c in factory.call_args_list], ['fake-a', 'fake-b'])


if __name__ == '__main__':
    unittest.main()
