import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
import reel_analyzer
from pool import sheets as sheets_pool
from pool.sheets import SheetsKeyPool, SheetsPoolError, create_pool

MODEL = 'gemini-3.6-flash'


class SheetsPoolTests(unittest.TestCase):
    def setUp(self):
        p = patch.dict('os.environ', {
            'GEMINI_API_KEYS': 'secret-a,secret-b', 'GEMINI_API_KEY_LABELS': 'a,b',
            'GEMINI_MODELS': MODEL, 'GEMINI_POOL_MODE': 'sheets',
            'GEMINI_POOL_URL': 'https://script.google.com/macros/s/test/exec',
            'GEMINI_POOL_TOKEN': 't' * 40, 'GEMINI_POOL_USER': 'tester',
            'GEMINI_API_KEY_OWNERS': '{a:alice,b:bob}',
        })
        p.start(); self.addCleanup(p.stop)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.pool = SheetsKeyPool()
        self.pool.pending_dir = Path(self.temp.name)

    def selection(self, label='a', request_id='request:0'):
        return {'ok': True, 'selected': {'key_label': label, 'model': MODEL, 'request_id': request_id},
                'sampled': [{'key_label': 'a', 'model': MODEL, 'day_count': 1},
                            {'key_label': 'b', 'model': MODEL, 'day_count': 2}]}

    def test_reselects_and_finishes_each_reel(self):
        client = Mock()
        client.models.generate_content.return_value = SimpleNamespace(text='{"summary":"ok"}')
        with patch.object(self.pool, '_post', side_effect=[self.selection(), {'ok': True},
                          self.selection('b', 'request2:0'), {'ok': True}]) as post, \
             patch.object(reel_analyzer.genai, 'Client', return_value=client) as factory:
            reel_analyzer.analyze_video(self.pool, b'first')
            reel_analyzer.analyze_video(self.pool, b'second')
        self.assertEqual([c.args[0] for c in post.call_args_list], ['acquire','finish','acquire','finish'])
        self.assertNotEqual(post.call_args_list[0].kwargs['request_id'], post.call_args_list[2].kwargs['request_id'])
        self.assertEqual([c.kwargs['api_key'] for c in factory.call_args_list], ['secret-a','secret-b'])
        self.assertNotIn('secret-', repr(post.call_args_list))

    def test_exception_releases_selection(self):
        with patch.object(self.pool, '_post', side_effect=[self.selection(), {'ok': True}]) as post, \
             patch.object(reel_analyzer.genai, 'Client', side_effect=ValueError('bad config')):
            with self.assertRaises(ValueError):
                reel_analyzer._call_with_pool(self.pool, Mock())
        self.assertEqual(post.call_args.args, ('finish',))
        self.assertEqual(post.call_args.kwargs['status'], '결과 미확인')
        self.assertIsNone(self.pool._active)

    def test_transport_retry_uses_same_request_id(self):
        good = Mock(); good.json.return_value = self.selection()
        with patch('pool.sheets.requests.post', side_effect=[requests.Timeout(), good]) as post, \
             patch('pool.sheets.time.sleep'):
            self.pool.acquire_blocking()
        self.assertEqual(post.call_args_list[0].kwargs['json'], post.call_args_list[1].kwargs['json'])
        self.assertNotIn('secret-', json.dumps(post.call_args.kwargs['json']))

    def test_sync_merges_shared_keys_into_env(self):
        env_path = Path(self.temp.name) / '.env'
        env_path.write_text('KEEP=value\nGEMINI_API_KEYS="{a:stale-a}"\nTAIL=value\n', encoding='utf-8')
        self.pool.keys = [('a', 'stale-a')]
        with patch.object(self.pool, '_post', return_value={
            'ok': True, 'keys': [
                {'key_label': 'a', 'api_key': 'shared-a', 'owner': 'alice'},
                {'key_label': 'b', 'api_key': 'secret-b', 'owner': 'bob'},
            ],
            'vault_added': 0, 'vault_updated': 0,
            'owner_renamed': 0,
            'added': 3, 'updated': 0, 'enabled': 0, 'stopped': 0,
        }) as post, patch.object(sheets_pool, 'ENV_PATH', env_path):
            result = self.pool.sync_keys()
        post.assert_called_once_with('sync', keys=[
            {'key_label': 'a', 'api_key': 'stale-a', 'owner': 'alice'},
        ])
        self.assertEqual((result['local_added'], result['local_updated']), (1, 1))
        content = env_path.read_text(encoding='utf-8')
        self.assertIn('GEMINI_API_KEYS="{a:shared-a,\nb:secret-b}"', content)
        self.assertIn('GEMINI_API_KEY_OWNERS="{a:alice,\nb:bob}"', content)
        self.assertIn('KEEP=value', content)
        self.assertIn('TAIL=value', content)

    def test_missing_key_owner_falls_back_to_current_user(self):
        self.pool.key_owners = {'a': 'alice'}
        self.pool.keys = [('a', 'secret-a'), ('b', 'secret-b')]
        with patch.object(self.pool, '_post', return_value={
            'ok': True, 'keys': [
                {'key_label': 'a', 'api_key': 'secret-a', 'owner': 'alice'},
                {'key_label': 'b', 'api_key': 'secret-b', 'owner': 'tester'},
            ],
        }) as post, patch.object(sheets_pool, 'ENV_PATH', Path(self.temp.name) / '.env'):
            self.pool.sync_keys()
        post.assert_called_once_with('sync', keys=[
            {'key_label': 'a', 'api_key': 'secret-a', 'owner': 'alice'},
            {'key_label': 'b', 'api_key': 'secret-b', 'owner': 'tester'},
        ])

    def test_failed_finish_is_retried_before_new_acquire(self):
        self.pool._active = 'request:0'
        with patch.object(self.pool, '_post', side_effect=SheetsPoolError('offline')):
            self.pool.finish(self.pool.combos[0], response=SimpleNamespace())
        self.assertEqual(len(list(self.pool.pending_dir.glob('*.json'))), 1)
        with patch.object(self.pool, '_post', side_effect=[{'ok': True}, self.selection()]) as post:
            self.pool.acquire_blocking()
        self.assertEqual([c.args[0] for c in post.call_args_list], ['finish','acquire'])
        self.assertFalse(list(self.pool.pending_dir.glob('*.json')))

    def test_offline_never_falls_back(self):
        with patch.object(self.pool, '_post', side_effect=SheetsPoolError('offline')):
            with self.assertRaises(SheetsPoolError): self.pool.acquire_blocking()

    def test_missing_endpoint_blocks_start(self):
        with patch.dict('os.environ', {'GEMINI_POOL_URL': ''}):
            with self.assertRaises(SheetsPoolError): create_pool()

    def test_lite_rejected(self):
        with patch.dict('os.environ', {'GEMINI_MODELS': 'gemini-3.5-flash-lite'}):
            with self.assertRaises(SheetsPoolError): SheetsKeyPool()

    def test_3_8_rejected(self):
        with patch.dict('os.environ', {'GEMINI_MODELS': 'gemini-3.8-flash'}):
            with self.assertRaises(SheetsPoolError): SheetsKeyPool()

    def test_daily_error_recorded_on_finish(self):
        from google.genai.errors import ClientError
        self.pool._active = 'request:0'
        error = ClientError(429, {'error': {'message': 'RequestsPerDay'}})
        self.pool.mark_quota_error(self.pool.combos[0], error)
        with patch.object(self.pool, '_post', return_value={'ok': True}) as post:
            self.pool.finish(self.pool.combos[0], error=error)
        self.assertEqual(post.call_args.kwargs['limit'], '일일')

    def test_results_are_appended_to_one_json_file(self):
        output_dir = Path(self.temp.name) / 'output'
        output_file = output_dir / 'reel_analyses.json'
        with patch.object(reel_analyzer, 'OUTPUT_DIR', str(output_dir)), \
             patch.object(reel_analyzer, 'OUTPUT_FILE', str(output_file)):
            first = reel_analyzer.save_result('first', {'summary': 'one'})
            second = reel_analyzer.save_result('second', {'summary': 'two'})
        self.assertEqual(first, second)
        records = json.loads(output_file.read_text(encoding='utf-8'))
        self.assertEqual([r['reel_id'] for r in records], ['first', 'second'])
        self.assertEqual(records[1]['analysis']['summary'], 'two')

    def test_parallel_results_are_all_saved(self):
        output_dir = Path(self.temp.name) / 'output'
        output_file = output_dir / 'reel_analyses.json'
        with patch.object(reel_analyzer, 'OUTPUT_DIR', str(output_dir)), \
             patch.object(reel_analyzer, 'OUTPUT_FILE', str(output_file)):
            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(lambda i: reel_analyzer.save_result(str(i), {'summary': str(i)}), range(10)))
        records = json.loads(output_file.read_text(encoding='utf-8'))
        self.assertEqual({record['reel_id'] for record in records}, {str(i) for i in range(10)})


if __name__ == '__main__': unittest.main()
