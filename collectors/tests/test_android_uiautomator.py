from src.reels import android_metric_worker as _remediation_android_metric_worker
import importlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.reels import android_reel_metrics as android


class PersistentAndroidTests(unittest.TestCase):
    def backend(self):
        self.assertIsNotNone(importlib.util.find_spec('src.reels.android_uiautomator'), 'Persistent backend is missing')
        return importlib.import_module('src.reels.android_uiautomator')

    def test_default_enricher_uses_persistent_driver(self):
        enricher = android.AndroidReelMetricsEnricher(adb_path=Path('adb.exe'), device_id='chosen-device')
        self.assertEqual(type(enricher.driver).__name__, 'PersistentAndroidUiDriver')

    def test_session_preserves_login_and_sets_verified_idle_timeout(self):
        m = self.backend()
        lease = Mock()
        lease.acquire.return_value = True
        def reply(url, method, path, payload=None, **kwargs):
            return {'sessionId':'test-session'} if path == '/session' else {}
        with patch.object(m, 'ensure_server'), patch.object(m, '_process_lock', return_value=lease), patch.object(m, 'http_request', side_effect=reply) as request:
            client = m.UiAutomatorSession(Path('adb.exe'), 'chosen-device')
            client.start()
            client.start()
            create = next(c for c in request.call_args_list if c.args[2] == '/session')
            caps = create.args[3]['capabilities']['alwaysMatch']
            self.assertTrue(caps['appium:noReset'])
            self.assertFalse(caps['appium:fullReset'])
            self.assertFalse(caps['appium:autoLaunch'])
            self.assertEqual(caps['appium:udid'], 'chosen-device')
            settings = next(c for c in request.call_args_list if c.args[2].endswith('/appium/settings'))
            self.assertEqual(settings.args[3]['settings']['waitForIdleTimeout'], 500)
            self.assertEqual(sum(c.args[2] == '/session' for c in request.call_args_list), 1)
            client.close()
            lease.release.assert_called_once()

    def test_busy_device_does_not_create_competing_session(self):
        m = self.backend()
        lease = Mock()
        lease.acquire.return_value = False
        with patch.object(m, '_process_lock', return_value=lease), patch.object(m, 'http_request') as request:
            client = m.UiAutomatorSession(Path('adb.exe'), 'chosen-device')
            with self.assertRaisesRegex(android.AndroidMetricsError, 'device busy'):
                client.start()
            request.assert_not_called()

    def test_source_failure_discards_session_for_reconnect(self):
        m = self.backend()
        client = m.UiAutomatorSession(Path('adb.exe'), 'chosen-device')
        client.session_id = 'expired'
        client._lease = Mock()
        lease = client._lease
        with patch.object(m, 'http_request', side_effect=android.AndroidMetricsError('invalid session id')):
            with self.assertRaises(android.AndroidMetricsError):
                client.source(timeout_seconds=1)
        self.assertFalse(client.session_id)
        self.assertIs(client._lease, lease)
        lease.release.assert_not_called()

    def test_source_timeout_does_not_shorten_session_start_budget(self):
        m = self.backend()
        client = m.UiAutomatorSession(Path('adb.exe'), 'chosen-device')
        def started(**kwargs):
            client.session_id = 'ready'
        with patch.object(client, 'start', side_effect=started) as start, patch.object(m, 'http_request', return_value='<hierarchy />'):
            client.source(timeout_seconds=.5)
        start.assert_called_once_with()

    def test_timed_out_start_closes_only_its_late_created_session(self):
        m = self.backend()
        client = m.UiAutomatorSession(Path('adb.exe'), 'chosen-device')
        lease = Mock()
        lease.acquire.return_value = True
        transport_error = android.AndroidMetricsError('connection lost')
        transport_error.__cause__ = TimeoutError('socket timed out')
        def reply(url, method, path, payload=None, **kwargs):
            if method == 'POST' and path == '/session':
                self.assertEqual(kwargs['timeout'], 180)
                raise transport_error
            if path == '/appium/sessions':
                return [{'id':'other', 'capabilities':{'sessionName':'unrelated'}},
                        {'id':'ours', 'capabilities':{'sessionName':client.session_name}}]
            return None
        with patch.object(m, 'ensure_server'), patch.object(m, '_process_lock', return_value=lease), patch.object(m, 'http_request', side_effect=reply) as request:
            with self.assertRaises(android.AndroidMetricsError):
                client.start()
        deletes = [c.args[2] for c in request.call_args_list if c.args[1]=='DELETE']
        self.assertEqual(deletes, ['/session/ours'])
        lease.release.assert_not_called()
        client.close()
        lease.release.assert_called_once()

    def test_start_failure_keeps_lease_until_final_close(self):
        m = self.backend()
        lease = Mock()
        lease.acquire.return_value = True
        client = m.UiAutomatorSession(Path('adb.exe'), 'chosen-device')
        with patch.object(m, '_process_lock', return_value=lease), patch.object(m, 'ensure_server', side_effect=android.AndroidMetricsError('server unavailable')):
            with self.assertRaises(android.AndroidMetricsError):
                client.start()
        lease.release.assert_not_called()
        client.close()
        lease.release.assert_called_once()

    def test_appium_element_tags_are_normalized_for_existing_parser(self):
        m = self.backend()
        driver = m.PersistentAndroidUiDriver(Path('adb.exe'), 'chosen-device')
        driver._client = Mock()
        driver._client.source.return_value = '<hierarchy><android.widget.TextView text="author" resource-id="com.instagram.android:id/clips_author_username" bounds="[0,0][10,10]" /></hierarchy>'
        nodes = android.parse_ui_xml(driver.dump_ui(timeout_seconds=2))
        self.assertEqual(nodes[0].text, 'author')
        self.assertEqual(nodes[0].class_name, 'android.widget.TextView')

    def test_invalid_source_is_retryable_and_not_used_as_metrics(self):
        m = self.backend()
        driver = m.PersistentAndroidUiDriver(Path('adb.exe'), 'chosen-device')
        driver._client = Mock()
        driver._client.source.return_value = '<hierarchy />'
        with self.assertRaisesRegex(android.AndroidMetricsError, 'empty or unreadable screen'):
            driver.dump_ui()
        driver._client.reset_session.assert_called()

    def test_ui_reset_retains_device_ownership_until_final_close(self):
        m = self.backend()
        client = m.UiAutomatorSession(Path('adb.exe'), 'chosen-device')
        client.session_id = 'active'
        client._lease = Mock()
        lease = client._lease
        with patch.object(m, 'http_request'):
            client.reset_session()
            lease.release.assert_not_called()
            client.close()
        lease.release.assert_called_once()

    def test_link_confirmation_reuses_only_immediate_reel_snapshot(self):
        m = self.backend()
        driver = m.PersistentAndroidUiDriver(Path('adb.exe'), 'chosen-device')
        driver._client = Mock()
        reel = '<hierarchy><node resource-id="com.instagram.android:id/direct_share_button" bounds="[0,0][10,10]" /></hierarchy>'
        menu = '<hierarchy><node text="Copy link" bounds="[20,20][30,30]" /></hierarchy>'
        driver._client.source.side_effect = [reel, menu, reel]
        driver.dump_ui()
        url = 'https://www.instagram.com/reel/ACTUAL123/'
        with patch.object(android.AdbAndroidUiDriver, 'tap_bounds', return_value=True), patch.object(android, '_read_windows_clipboard_text', side_effect=['before', url]), patch.object(android, '_set_windows_clipboard_text'), patch.object(android.time, 'sleep'):
            self.assertEqual(driver.copy_current_reel_url(), url)
        self.assertEqual(driver._client.source.call_count, 3)

    def test_ready_server_does_not_require_local_installation(self):
        m = self.backend()
        with patch.object(m, 'http_request', return_value={'ready':True}), patch.object(m.subprocess, 'Popen') as spawn:
            m.ensure_server(Path('adb.exe'), 'http://127.0.0.1:4723')
            spawn.assert_not_called()

    def test_missing_runtime_has_actionable_setup_message(self):
        m = self.backend()
        with tempfile.TemporaryDirectory() as directory, patch.object(m, 'RUNTIME_DIR', Path(directory)), patch.object(m, 'http_request', side_effect=android.AndroidMetricsError('offline')):
            with self.assertRaisesRegex(android.AndroidMetricsError, 'setup-android-ui.ps1'):
                m.ensure_server(Path('adb.exe'), 'http://127.0.0.1:4723')

    def test_owned_enricher_driver_is_closed_on_reset(self):
        enricher = android.AndroidReelMetricsEnricher()
        driver = Mock(device_id='chosen-device')
        enricher._driver = driver
        enricher.reset_connection()
        driver.close.assert_called_once()
        self.assertEqual(enricher._device_id, 'chosen-device')

    def test_readiness_failure_does_not_consume_durable_job(self):
        from src.reels import instagram_reels_browser as browser
        with tempfile.TemporaryDirectory() as directory:
            browser.enqueue_android_metric_job(directory, {'url':'https://www.instagram.com/reels/CODE/', 'collected_at':'2026-10-27T00:00:00Z'})
            enricher = Mock()
            enricher._ready.return_value = 'UIAutomator runtime missing. Run setup-android-ui.ps1.'
            with patch.object(_remediation_android_metric_worker, 'AndroidReelMetricsEnricher', return_value=enricher):
                self.assertEqual(browser.run_android_metric_worker(directory, attach_existing=False), 1)
            enricher.enrich.assert_not_called()
            self.assertEqual(browser.android_metric_queue_counts(directory)['pending'], 1)
