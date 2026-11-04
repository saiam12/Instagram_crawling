import unittest
import sys
import json
import tempfile
import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
import guide_diagram as guide
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from collectors.monitor.backend import gemini_recommend


SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 600"><rect width="1200" height="600" fill="white"/><text x="20" y="30">장면 1 · 제품 클로즈업</text></svg>'


class GuideTests(unittest.TestCase):
    def test_agent_uses_reserved_existing_key_and_logs_its_own_usage(self):
        pool = Mock()
        pool.acquire_blocking.return_value = SimpleNamespace(api_key='fake-key', model=guide.GUIDE_AGENT)
        response = Mock()
        response.json.return_value = {'status': 'completed', 'steps': [
            {'type': 'model_output', 'content': [{'type': 'text', 'text': SVG}]}],
            'usage': {'total_input_tokens': 10, 'total_output_tokens': 20, 'total_tokens': 35}}
        with patch.object(guide, 'SheetsKeyPool', return_value=pool) as factory, \
             patch.object(guide.requests, 'post', return_value=response) as post:
            result = guide.generate_guide({'concept': '제품 소개', 'scenes': []})
        factory.assert_called_once_with(models=[guide.GUIDE_AGENT])
        self.assertTrue(result['image'].startswith('data:image/png;base64,'))
        self.assertEqual(post.call_args.kwargs['headers']['x-goog-api-key'], 'fake-key')
        self.assertEqual(post.call_args.kwargs['json']['agent'], guide.GUIDE_AGENT)
        self.assertNotIn('store', post.call_args.kwargs['json'])
        self.assertEqual(post.call_args.kwargs['json']['agent_config']['model'], 'gemini-3.6-flash')
        self.assertEqual(post.call_args.kwargs['json']['agent_config']['max_total_tokens'], 12000)
        self.assertEqual(pool.finish.call_args.kwargs['response'].usage_metadata.total_token_count, 35)

    def test_direct_agent_call_does_not_select_a_gemini_model(self):
        pool = Mock()
        pool.acquire_blocking.return_value = SimpleNamespace(api_key='fake-key')
        response = Mock()
        response.json.return_value = {'status': 'completed', 'steps': [
            {'type': 'model_output', 'content': [{'type': 'text', 'text': SVG}]}]}
        with patch.object(guide, 'SheetsKeyPool', return_value=pool), \
             patch.object(guide.requests, 'post', return_value=response) as post:
            guide.generate_guide({'concept': '고정 테스트'}, model=None)
        self.assertNotIn('model', post.call_args.kwargs['json']['agent_config'])
        post.assert_called_once()

    def test_comments_are_removed_without_accepting_entities(self):
        self.assertNotIn('<!--', guide.safe_svg(SVG.replace('<rect', '<!-- Background --><rect')))
        with self.assertRaises(ValueError):
            guide.safe_svg('<!DOCTYPE svg [<!ENTITY test "x">]>' + SVG)

    def test_presentation_attributes_and_safe_inline_styles_are_retained(self):
        value = SVG.replace('<rect', '<rect class="background" data-note="test" style="stroke-dasharray:4 2;fill:white;font-style:italic"')
        result = guide.safe_svg(value)
        self.assertIn('stroke-dasharray="4 2"', result)
        self.assertIn('font-style="italic"', result)
        self.assertNotIn('class=', result)
        self.assertNotIn('data-note=', result)
        self.assertNotIn('style=', result.replace('font-style=', ''))

    def test_original_board_header_and_dimensions_are_preserved(self):
        board = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 1260"><text>영상 촬영 가이드 스토리보드</text>'
        for i in range(4):
            board += f'<g id="scene{i+1}"><rect x="30" y="135" width="555" height="535"/><text>SCENE {i+1}</text></g>'
        board += '</svg>'
        self.assertIn('스토리보드', guide.safe_svg(board))
        result = guide.guide_result(board, 'completed', None)
        self.assertEqual((result['width'], result['height']), (1200, 1260))

    def test_image_cache_reuses_png_and_invalidates_on_source_change_without_api(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(guide.requests, 'post') as post:
            path = Path(folder) / 'test.response.json'
            source = {'output_text': SVG, 'status': 'completed', 'model': None}
            path.write_text(json.dumps(source), encoding='utf-8')
            first = guide.cached_guide(path)
            with patch.object(guide, 'guide_result', side_effect=AssertionError('must use cache')):
                self.assertEqual(guide.cached_guide(path), first)
            source['output_text'] = SVG.replace('장면 1', '장면 2')
            path.write_text(json.dumps(source), encoding='utf-8')
            with patch.object(guide, 'guide_result', wraps=guide.guide_result) as render:
                guide.cached_guide(path)
                render.assert_called_once()
        post.assert_not_called()

    def test_response_is_saved_before_validation_and_credentials_are_redacted(self):
        pool = Mock(keys=[('test', 'fake-key')])
        pool.acquire_blocking.return_value = SimpleNamespace(api_key='fake-key')
        response = Mock()
        response.json.return_value = {'status': 'completed', 'steps': [
            {'type': 'model_output', 'content': [{'type': 'text', 'text': SVG.replace('<rect', '<rect onclick="fake-key"')}]}],
            'usage': {'total_tokens': 123}}
        with tempfile.TemporaryDirectory() as folder, patch.object(guide, 'SheetsKeyPool', return_value=pool), \
             patch.object(guide.requests, 'post', return_value=response):
            path = Path(folder) / 'response.json'
            with self.assertRaisesRegex(ValueError, 'onclick'):
                guide.generate_guide({}, model=None, response_path=path)
            self.assertNotIn('fake-key', path.read_text(encoding='utf-8'))
            self.assertIn('[redacted]', path.read_text(encoding='utf-8'))
        self.assertEqual(pool.finish.call_args.kwargs['response'].usage_metadata.total_token_count, 123)
        self.assertIsInstance(pool.finish.call_args.kwargs['error'], ValueError)

    def test_replay_writes_png_without_calling_api(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(guide.requests, 'post') as post:
            saved, output = Path(folder) / 'saved.json', Path(folder) / 'guide.png'
            saved.write_text(json.dumps({'output_text': SVG, 'status': 'completed'}), encoding='utf-8')
            self.assertEqual(guide.main(['--replay', str(saved), '--output', str(output)]), 0)
            self.assertTrue(output.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
        post.assert_not_called()

    def test_incomplete_run_returns_only_valid_svg_with_warning(self):
        pool = Mock()
        pool.acquire_blocking.return_value = SimpleNamespace(api_key='fake-key')
        response = Mock()
        response.json.return_value = {'status': 'incomplete', 'steps': [
            {'type': 'model_output', 'content': [{'type': 'text', 'text': SVG}]}]}
        with patch.object(guide, 'SheetsKeyPool', return_value=pool), \
             patch.object(guide.requests, 'post', return_value=response):
            self.assertIn('warning', guide.generate_guide({}, model=None))
            response.json.return_value['steps'][0]['content'][0]['text'] = SVG[:-6]
            with self.assertRaises(guide.ET.ParseError):
                guide.generate_guide({}, model=None)

    def test_timeout_is_not_retried_and_releases_reservation_as_unknown(self):
        pool = Mock()
        pool.acquire_blocking.return_value = SimpleNamespace(api_key='fake')
        with patch.object(guide, 'SheetsKeyPool', return_value=pool), \
             patch.object(guide.requests, 'post', side_effect=requests.Timeout) as post:
            with self.assertRaises(requests.Timeout):
                guide.generate_guide({'scenes': []})
        post.assert_called_once()
        self.assertIsNone(pool.finish.call_args.kwargs['response'])
        self.assertIsInstance(pool.finish.call_args.kwargs['error'], requests.Timeout)

    def test_diagram_rejects_active_or_external_content(self):
        for extra in ['<script>alert(1)</script>', '<image href="https://example.com/x"/>',
                      '<rect onclick="alert(1)"/>', '<rect fill="url(https://example.com/x)"/>']:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                guide.safe_svg(SVG.replace('</svg>', extra + '</svg>'))

    def test_direct_video_failure_is_safe_without_flash_fallback(self):
        import guide_videos
        from io import StringIO
        with patch.object(guide_videos, 'VideoPool'), \
             patch.object(guide_videos, 'generate_videos', side_effect=RuntimeError('fake-secret')), \
             patch.object(sys, 'stdin', StringIO(json.dumps({'brief':{},'references':[]}))), \
             patch.dict('os.environ', {'GEMINI_GUIDE_ENABLED':'true','GEMINI_POOL_MODE':'sheets'}):
            result = gemini_recommend.run()
        self.assertNotIn('plan',result)
        self.assertEqual(result['guide']['state'],'failed')
        self.assertNotIn('fake-secret',json.dumps(result))

    def test_web_adapter_sends_brief_and_scenes_directly_without_flash(self):
        import guide_videos
        from io import StringIO
        payload={'brief':{'product':'coat'},'references':[{'scene_details':[{'camera':'medium'}]}]}
        with patch.object(guide_videos, 'VideoPool') as pool, \
             patch.object(guide_videos, 'generate_videos', return_value={'state':'completed','kind':'videos','videos':[]}) as generate, \
             patch.object(sys, 'stdin', StringIO(json.dumps(payload))), \
             patch('google.genai.Client') as flash, \
             patch.dict('os.environ', {'GEMINI_GUIDE_ENABLED':'true','GEMINI_POOL_MODE':'sheets'}):
            result = gemini_recommend.run()
        flash.assert_not_called()
        self.assertEqual(generate.call_args.args,({},payload['brief'],payload['references']))
        pool.assert_called_once_with(models=[guide_videos.GUIDE_AGENT])
        self.assertNotIn('plan',result)
        self.assertEqual(result['guide']['kind'],'videos')

    def test_status_never_generates_or_reserves_keys(self):
        import guide_videos
        with patch.object(guide_videos, 'VideoPool') as pool, \
             patch.object(guide_videos, 'generate_videos') as generate, \
             patch.dict('os.environ', {'GEMINI_GUIDE_ENABLED':'true','GEMINI_POOL_MODE':'sheets'}):
            self.assertTrue(gemini_recommend.run(status=True)['ready'])
        generate.assert_not_called()
        pool.return_value.acquire_blocking.assert_not_called()
