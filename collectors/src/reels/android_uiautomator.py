"""Persistent Android UI reads; ADB still owns navigation and device checks."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener
from xml.etree import ElementTree

from .android_reel_metrics import AdbAndroidUiDriver, AndroidMetricsError, default_adb_path

COLLECTORS = Path(__file__).resolve().parents[2]
RUNTIME_DIR = COLLECTORS / 'scripts' / 'android' / 'tools' / 'android-ui'
STATE_DIR = COLLECTORS / 'runtime' / 'android-ui'


def server_url() -> str:
    value = os.environ.get('INSTAGRAM_APPIUM_URL', 'http://127.0.0.1:4723').strip().rstrip('/')
    parsed = urlparse(value)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AndroidMetricsError('INSTAGRAM_APPIUM_URL must be an HTTP(S) server URL without credentials or query parameters.')
    return value


def http_request(url: str, method: str, path: str, payload=None, *, timeout: float = 15):
    body = json.dumps(payload).encode('utf-8') if payload is not None else None
    request = Request(url + path, data=body, method=method, headers={'Content-Type': 'application/json'})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=max(.1, timeout)) as response:
            result = json.load(response)
    except HTTPError as error:
        try:
            detail = json.loads(error.read()).get('value', {})
            message = detail.get('message', str(error)) if isinstance(detail, dict) else str(error)
        except (ValueError, OSError):
            message = str(error)
        raise AndroidMetricsError(f'UIAutomator HTTP {error.code}: {message[:400]}') from error
    except (URLError, OSError, ValueError) as error:
        raise AndroidMetricsError(f'UIAutomator connection unavailable: {error}') from error
    value = result.get('value')
    if isinstance(value, dict) and value.get('error'):
        raise AndroidMetricsError(f"UIAutomator {value['error']}: {str(value.get('message', ''))[:400]}")
    return value


def _process_lock(path: Path, *, wait_seconds: float = 0):
    # Imported lazily to avoid the browser -> Android driver import cycle.
    from .process_lock import AtomicProcessLock
    return AtomicProcessLock(path, wait_seconds=wait_seconds)


def ensure_server(adb_path: Path, url: str | None = None) -> str:
    url = url or server_url()
    def ready():
        try:
            status = http_request(url, 'GET', '/status', timeout=2)
            return isinstance(status, dict) and bool(status.get('ready'))
        except AndroidMetricsError:
            return False
    if ready():
        return url
    parsed = urlparse(url)
    if parsed.hostname not in {'localhost', '127.0.0.1', '::1'} or parsed.scheme != 'http':
        raise AndroidMetricsError('Configured Appium server is unavailable. Start it before collection.')
    script = RUNTIME_DIR / 'node_modules' / 'appium' / 'build' / 'lib' / 'main.js'
    if not script.is_file():
        raise AndroidMetricsError('Android UI runtime is not installed. Run .\\scripts\\setup\\setup-android-ui.ps1 from collectors first.')
    node = shutil.which('node')
    if not node:
        raise AndroidMetricsError('Node.js 24+ is required. Install Node.js, then run scripts/setup/setup-android-ui.ps1.')
    environment = os.environ.copy()
    environment.pop('APPIUM_HOME', None)  # Use the pinned project drivers.
    resolved_adb = Path(adb_path).resolve()
    if resolved_adb.parent.name == 'platform-tools':
        environment['ANDROID_HOME'] = str(resolved_adb.parent.parent)
    java_home = environment.get('JAVA_HOME') or environment.get('ANDROID_STUDIO_JAVA_HOME')
    if not java_home:
        bundled_java = Path(environment.get('PROGRAMFILES', r'C:\Program Files')) / 'Android' / 'Android Studio' / 'jbr'
        if (bundled_java / 'bin' / 'java.exe').is_file():
            java_home = str(bundled_java)
    if java_home:
        environment['JAVA_HOME'] = os.path.expandvars(java_home.strip('"'))
    elif not shutil.which('java'):
        raise AndroidMetricsError('Java is required. Set JAVA_HOME to the Android Studio jbr folder.')
    port = parsed.port or 80
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = _process_lock(STATE_DIR / f'server-{port}.lock.json', wait_seconds=90)
    if not lock.acquire():
        raise AndroidMetricsError('UIAutomator server startup is busy; retry after the other collector finishes starting.')
    try:
        if ready():
            return url
        log_path = STATE_DIR / f'appium-{port}.log'
        command = [node, str(script), '--address', parsed.hostname, '--port', str(port), '--log-no-colors',
                   '--allow-insecure=*:session_discovery']
        if parsed.path and parsed.path != '/':
            command += ['--base-path', parsed.path]
        with log_path.open('ab') as log:
            process = subprocess.Popen(command, cwd=RUNTIME_DIR, env=environment, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        state = {'pid': process.pid, 'server_url': url, 'node': node, 'script': str(script)}
        (STATE_DIR / f'server-{port}.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and process.poll() is None:
            if ready():
                return url
            time.sleep(.25)
        raise AndroidMetricsError(f'UIAutomator server did not start. See {log_path}')
    finally:
        lock.release()


class UiAutomatorSession:
    def __init__(self, adb_path: Path, device_id: str, *, url: str | None = None):
        self.adb_path, self.device_id = adb_path, device_id
        self.url = url or server_url()
        self.session_id = ''
        self.session_name = 'instagram-' + uuid.uuid4().hex
        self._lease = None

    def start(self):
        if self.session_id:
            return
        if self._lease is None:
            key = hashlib.sha256(self.device_id.encode()).hexdigest()[:16]
            lease = _process_lock(STATE_DIR / f'device-{key}.lock.json')
            if not lease.acquire():
                raise AndroidMetricsError(f'Android device busy: another collector owns {self.device_id}.')
            self._lease = lease
        try:
            ensure_server(self.adb_path, self.url)
            capabilities = {'platformName': 'Android', 'appium:automationName': 'UiAutomator2',
                            'appium:udid': self.device_id, 'appium:appPackage': 'com.instagram.android',
                            'appium:noReset': True, 'appium:fullReset': False, 'appium:autoLaunch': False,
                            'appium:dontStopAppOnReset': True, 'appium:shouldTerminateApp': False,
                            'appium:skipUnlock': True, 'appium:skipLogcatCapture': True,
                            'appium:sessionName': self.session_name,
                            'appium:newCommandTimeout': 300}
            try:
                result = http_request(self.url, 'POST', '/session',
                                      {'capabilities': {'alwaysMatch': capabilities, 'firstMatch': [{}]}}, timeout=180)
            except AndroidMetricsError as error:
                if error.__cause__ is not None and not isinstance(error.__cause__, HTTPError):
                    self._find_late_startup()
                raise
            self.session_id = str(result['sessionId'])
            http_request(self.url, 'POST', f'/session/{self.session_id}/appium/settings',
                         {'settings': {'waitForIdleTimeout': 500, 'waitForSelectorTimeout': 0}})
        except Exception as error:
            self.reset_session()
            if isinstance(error, AndroidMetricsError):
                raise
            raise AndroidMetricsError(f'UIAutomator session could not start: {error}') from error

    def ensure_ready(self):
        if self.session_id:
            try:
                http_request(self.url, 'GET', f'/session/{self.session_id}/appium/settings', timeout=5)
            except AndroidMetricsError:
                self.reset_session()
        self.start()

    def source(self, *, timeout_seconds: float = 15) -> str:
        self.start()
        try:
            value = http_request(self.url, 'GET', f'/session/{self.session_id}/source', timeout=timeout_seconds)
            if not isinstance(value, str):
                raise AndroidMetricsError('UIAutomator returned no screen source.')
            return value
        except AndroidMetricsError:
            self.reset_session()
            raise

    def _find_late_startup(self):
        # A disconnected POST does not cancel Appium startup. Retain ownership
        # while locating this request; startup cleanup closes only its session.
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                sessions = http_request(self.url, 'GET', '/appium/sessions', timeout=2)
                for session in sessions or []:
                    caps = session.get('capabilities', {})
                    if caps.get('sessionName', caps.get('appium:sessionName')) == self.session_name:
                        self.session_id = str(session['id'])
                        return
            except AndroidMetricsError:
                pass
            time.sleep(.5)

    def reset_session(self):
        """Drop stale instrumentation without letting another worker navigate."""
        session_id, self.session_id = self.session_id, ''
        try:
            if session_id:
                http_request(self.url, 'DELETE', f'/session/{session_id}', timeout=30)
        except AndroidMetricsError:
            pass

    def close(self):
        try:
            self.reset_session()
        finally:
            if self._lease is not None:
                self._lease.release()
                self._lease = None


class PersistentAndroidUiDriver(AdbAndroidUiDriver):
    def __init__(self, adb_path=None, device_id=None, *, diagnostics=None):
        super().__init__(adb_path, device_id, diagnostics=diagnostics)
        self._client = UiAutomatorSession(self.adb_path, self.device_id)
        self._last_xml = ''
        self._reuse_xml_once = None

    @property
    def requires_ready_check(self):
        return not self._client.session_id

    def ensure_ready(self):
        super().ensure_ready()
        self._client.ensure_ready()

    def dump_ui(self, *, timeout_seconds=None):
        if self._reuse_xml_once is not None:
            value, self._reuse_xml_once = self._reuse_xml_once, None
            return value
        started = time.monotonic()
        try:
            source = self._client.source(timeout_seconds=min(15, timeout_seconds or 15))
            root = ElementTree.fromstring(source)
            if root.tag != 'hierarchy' or not list(root):
                raise ValueError('No Android UI nodes')
            for node in root.iter():
                if node.tag != 'hierarchy':
                    node.attrib.setdefault('class', node.tag)
                    node.tag = 'node'
            self._last_xml = ElementTree.tostring(root, encoding='unicode')
            if self.diagnostics is not None:
                self.diagnostics.emit('PERSISTENT_UI_READ', duration_ms=round((time.monotonic()-started)*1000, 1))
            return self._last_xml
        except (AndroidMetricsError, ElementTree.ParseError, ValueError) as error:
            self.reset_ui_automation()
            raise AndroidMetricsError(f'UIAutomator returned an empty or unreadable screen. Persistent session: {error}') from error

    def copy_current_reel_url(self):
        self._reuse_xml_once = self._last_xml or None
        try:
            return super().copy_current_reel_url()
        finally:
            self._reuse_xml_once = None

    def _invalidate_snapshot(self):
        self._last_xml = ''
        self._reuse_xml_once = None

    def open_instagram_url(self, url):
        self._invalidate_snapshot()
        return super().open_instagram_url(url)

    def tap_bounds(self, bounds):
        self._invalidate_snapshot()
        return super().tap_bounds(bounds)

    def tap_screen_center(self):
        self._invalidate_snapshot()
        return super().tap_screen_center()

    def press_back(self):
        self._invalidate_snapshot()
        return super().press_back()

    def reset_ui_automation(self):
        self._invalidate_snapshot()
        self._client.reset_session()
        return True

    def reset_connection(self):
        self.reset_ui_automation()

    def close(self):
        self._invalidate_snapshot()
        self._client.close()


if __name__ == '__main__':
    try:
        print(f'Android UI server ready: {ensure_server(default_adb_path())}')
    except AndroidMetricsError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
