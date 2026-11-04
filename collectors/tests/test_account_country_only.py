import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.reels import instagram_reels_browser as browser


class AccountCountryOnlyTests(unittest.IsolatedAsyncioTestCase):
    def test_foreground_option_and_conflict(self):
        options = browser.build_parser().parse_args(['--foreground'])
        self.assertFalse(options.background)
        with self.assertRaises(SystemExit):
            browser.build_parser().parse_args(['--foreground', '--background'])

    async def test_country_limit_records_sanitized_response_and_stage(self):
        page, _ = self.make_page()
        async def read_country(*args):
            page.on.call_args.args[1](SimpleNamespace(
                url='https://www.instagram.com/graphql/query/?token=secret',
                status=429, request=SimpleNamespace(resource_type='xhr')))
            await asyncio.sleep(0)
            return {'status': 'country_found', 'country': '대한민국'}
        with patch.object(browser, '_collect_profile_snapshot_from_response', AsyncMock()), patch.object(
            browser, 'read_profile_account_country_result', side_effect=read_country
        ):
            with tempfile.TemporaryDirectory() as temporary:
                log = Path(temporary) / 'account_country_429.jsonl'
                result = await browser.lookup_account_country_only(page, '123', 'author', diagnostics_path=log)
                import json
                saved = json.loads(log.read_text(encoding='utf-8'))
                self.assertEqual(saved['username'], 'author')
                self.assertEqual(saved['stage'], 'account_about')
                self.assertNotIn('secret', log.read_text(encoding='utf-8'))
        self.assertEqual(result['limit_reason'], 'http_429')
        event = result['rate_limit_responses'][0]
        self.assertEqual(event['stage'], 'account_about')
        self.assertNotIn('secret', event['url'])
        self.assertTrue(event['observed_at'])

    async def test_country_pass_waits_ten_seconds_and_still_stops_on_restriction(self):
        async def expire_wait(awaitable, timeout):
            awaitable.close()
            raise TimeoutError

        for status, expected in [('country_not_shown', 2), ('rate_limited', 1)]:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                options = SimpleNamespace(data_dir=Path(temporary), account_country_limit=0, follower_interval_seconds=999)
                follower = SimpleNamespace(users={
                    '1': {'user_id': '1', 'username': 'first', 'account_country': '-'},
                    '2': {'user_id': '2', 'username': 'second', 'account_country': '-'},
                })
                context = SimpleNamespace(new_page=AsyncMock(return_value=object()))
                with patch.object(browser, 'lookup_account_country_only', AsyncMock(return_value={
                    'status': status, 'country': '', 'error': ''
                })) as lookup, patch.object(browser, 'read_account_countries', return_value={}), patch.object(
                    browser, 'materialize_korean_overrides', return_value=0
                ), patch.object(browser, 'safe_close', AsyncMock()), patch.object(
                    browser.asyncio, 'wait_for', side_effect=expire_wait
                ) as wait:
                    checked, found, stop = await browser.recheck_unknown_account_countries(
                        options, context, follower, asyncio.Event(), AsyncMock()
                    )
                self.assertEqual(checked, expected)
                self.assertEqual(lookup.await_count, expected)
                self.assertEqual(stop, status if status == 'rate_limited' else '')
                self.assertEqual(wait.call_count, 1 if expected == 2 else 0)
                if expected == 2:
                    self.assertEqual(wait.call_args.kwargs['timeout'], 10.0)

    def make_page(self):
        control = SimpleNamespace(wait_for=AsyncMock())
        locator = Mock()
        locator.first = control
        locator.filter.return_value = locator
        page = SimpleNamespace(
            url="https://www.instagram.com/author/",
            on=Mock(), remove_listener=Mock(),
            goto=AsyncMock(return_value=None),
            evaluate=AsyncMock(return_value="Profile"),
            get_by_role=Mock(return_value=locator),
        )
        return page, control

    async def test_country_does_not_wait_for_follower_metrics(self):
        page, control = self.make_page()
        async def read_country(*args):
            control.wait_for.assert_awaited_once_with(state="visible", timeout=10_000)
            return {"status": "country_found", "country": "대한민국"}
        with patch.object(browser, "request_web_follower_count", AsyncMock(side_effect=AssertionError("Follower lookup must not run"))), patch.object(browser, "read_profile_account_country_result", side_effect=read_country) as read:
            result = await browser.lookup_account_country_only(page, "123", "author")
        self.assertEqual(result["country"], "대한민국")
        read.assert_awaited_once_with(page, "author", "123")
        page.goto.assert_awaited_once_with("https://www.instagram.com/author/", wait_until="domcontentloaded", timeout=30_000)
        page.remove_listener.assert_called_once_with("response", page.on.call_args.args[1])

    async def test_missing_username_link_is_retryable_and_not_missing_country(self):
        page, control = self.make_page()
        control.wait_for.side_effect = TimeoutError()
        with patch.object(browser, "read_profile_account_country_result", AsyncMock()) as read:
            result = await browser.lookup_account_country_only(page, "123", "author")
        self.assertEqual(result["status"], "about_unavailable")
        read.assert_not_awaited()

    async def test_login_challenge_and_action_limit_stop_before_about(self):
        for url, body, expected in (
            ("https://www.instagram.com/accounts/login/", "", "login_required"),
            ("https://www.instagram.com/challenge/", "", "challenge_required"),
            ("https://www.instagram.com/author/", "Try Again Later", "rate_limited"),
            ("https://www.instagram.com/other/", "", "profile_unavailable"),
        ):
            with self.subTest(expected=expected):
                page, control = self.make_page()
                page.url = url
                page.evaluate.return_value = body
                with patch.object(browser, "read_profile_account_country_result", AsyncMock()) as read:
                    result = await browser.lookup_account_country_only(page, "123", "author")
                self.assertEqual(result["status"], expected)
                control.wait_for.assert_not_awaited()
                read.assert_not_awaited()

    async def test_response_identity_and_limits_rechecked_after_about(self):
        for path, status, observed_id, expected in (
            ("/graphql/query/", 200, "999", "identity_unverified"),
            ("/graphql/query/", 429, "123", "rate_limited"),
            ("/ajax/bulk-route-definitions/", 429, "123", "country_found"),
        ):
            with self.subTest(expected=expected):
                page, _ = self.make_page()
                async def observe(response, username, destination):
                    destination["userId"] = observed_id
                async def read_country(*args):
                    page.on.call_args.args[1](SimpleNamespace(url="https://www.instagram.com" + path, status=status))
                    await asyncio.sleep(0)
                    return {"status": "country_found", "country": "대한민국"}
                with patch.object(browser, "_collect_profile_snapshot_from_response", observe), patch.object(browser, "read_profile_account_country_result", side_effect=read_country):
                    result = await browser.lookup_account_country_only(page, "123", "author")
                self.assertEqual(result["status"], expected)
                if expected != "country_found":
                    self.assertEqual(result["country"], "")

    async def test_navigation_error_and_cancellation_remove_listener(self):
        for error in (RuntimeError("navigation failed"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                page, _ = self.make_page()
                page.goto.side_effect = error
                with self.assertRaises(type(error)):
                    await browser.lookup_account_country_only(page, "123", "author")
                page.remove_listener.assert_called_once()

    async def test_username_opens_panel_once_and_missing_country_is_not_an_error(self):
        browser._PROFILE_ACCOUNT_COUNTRIES.pop(('maru_nuna_', '123'), None)
        link = Mock()
        link.first = link
        link.filter.return_value = link
        link.count = AsyncMock(return_value=1)
        link.click = AsyncMock()
        close = Mock()
        close.last = close
        close.is_visible = AsyncMock(return_value=True)
        close.click = AsyncMock()
        heading = SimpleNamespace(wait_for=AsyncMock())
        country = SimpleNamespace(count=AsyncMock(return_value=0))
        joined = SimpleNamespace(wait_for=AsyncMock())
        panel = Mock()
        panel.last = panel
        panel.get_by_role.return_value = heading
        panel.get_by_text.side_effect = lambda pattern: country if 'Account based in' in pattern.pattern else joined
        def role(role_name, **kwargs):
            return {'link': link, 'heading': heading, 'dialog': panel, 'button': close}[role_name]
        page = SimpleNamespace(url='https://www.instagram.com/maru_nuna_/', get_by_role=Mock(side_effect=role))
        result = await browser.read_profile_account_country_result(page, 'maru_nuna_', '123')
        self.assertEqual(result['status'], 'country_not_shown')
        self.assertEqual(result['country'], '')
        self.assertEqual(result['entry_method'], 'username_link')
        link.click.assert_awaited_once()
        joined.wait_for.assert_awaited_once()
        close.click.assert_awaited_once()
        self.assertFalse(any(c.kwargs.get('name') and '옵션' in str(c.kwargs['name']) for c in page.get_by_role.call_args_list))
        # A failed/empty dialog must remain retryable instead of being counted
        # as a completed check with an undisclosed country.
        browser._PROFILE_ACCOUNT_COUNTRIES.pop(('maru_nuna_', '123'), None)
        joined.wait_for.side_effect = TimeoutError()
        failed = await browser.read_profile_account_country_result(page, 'maru_nuna_', '123')
        self.assertEqual(failed['status'], 'about_unavailable')
