import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.reels import instagram_reels_browser as browser
from src.reels.android_reel_metrics import AndroidReelMetricsEnricher
from tests.test_android_reel_metrics import FakeDriver, REEL_XML


class IdentityDriver(FakeDriver):
    def __init__(self, screens, copied='https://www.instagram.com/reels/TARGET/'):
        super().__init__(screens)
        self.copied = copied

    def copy_current_reel_url(self):
        return self.copied


class AndroidIdentityTests(unittest.TestCase):
    def collect(self, driver, username='creator'):
        with patch('src.reels.android_reel_metrics.time.sleep'):
            return AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=.1).enrich(
                'https://www.instagram.com/reels/TARGET/', expected_username=username)

    def test_waits_for_target_author_instead_of_accepting_first_reel(self):
        driver = IdentityDriver([REEL_XML.replace('creator', 'wrong'), REEL_XML])
        self.assertEqual(self.collect(driver).status, 'collected')
        self.assertGreaterEqual(driver.dump_count, 2)

    def test_wrong_author_same_author_other_reel_and_unverified_link_are_rejected(self):
        for screens, copied in (([REEL_XML.replace('creator', 'wrong')], 'https://www.instagram.com/reels/TARGET/'),
                                ([REEL_XML], 'https://www.instagram.com/reels/OTHER/'),
                                ([REEL_XML], '')):
            result = self.collect(IdentityDriver(screens, copied))
            self.assertEqual(result.status, 'unavailable')
            self.assertFalse(result.metrics)

    def test_multiple_cards_are_rejected_even_if_one_author_matches(self):
        mixed = REEL_XML.replace('</hierarchy>', '') + REEL_XML.replace('creator', 'wrong').replace('<hierarchy>', '')
        self.assertFalse(self.collect(IdentityDriver([mixed])).metrics)


class ViewObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_locked_reel_history_does_not_abort_profile_view_lookup(self):
        row = {"url": "https://www.instagram.com/reels/TARGET/", "collected_at": "2026-10-22T00:00:00Z",
               "username": "creator", "view_count": ""}
        flush = AsyncMock(side_effect=[PermissionError("CSV locked"), PermissionError("CSV locked")])
        store = SimpleNamespace(rows=[row], lock=asyncio.Lock(), dirty=False, flush=flush)
        enricher = browser.ProfileReelViewEnricher(store)
        enricher.track(row)
        profile = {"status": "success", "followerCount": 1234}
        with patch.object(browser, "read_profile_account_country", AsyncMock(return_value="")), patch.object(
            browser, "read_profile_reel_view_counts", AsyncMock(return_value={"TARGET": 100})
        ):
            await enricher(object(), {"username": "creator"}, profile)
        self.assertEqual(profile["status"], "success")
        self.assertEqual(row["follower_count"], 1234)
        self.assertEqual(row["view_count"], 100)
        self.assertTrue(store.dirty)
        self.assertEqual(flush.await_count, 2)

    async def test_later_snapshot_prevents_retroactive_profile_fill(self):
        first = {'url':'https://www.instagram.com/reels/TARGET/', 'collected_at':'2026-10-22T00:00:00Z', 'username':'creator', 'view_count':''}
        second = {**first, 'collected_at':'2026-10-22T04:00:00Z'}
        store = SimpleNamespace(rows=[first,second], lock=asyncio.Lock(), dirty=False, flush=AsyncMock())
        enrich = browser.ProfileReelViewEnricher(store)
        enrich.track(first)
        enrich.track(second)
        with patch.object(browser, 'read_profile_account_country', AsyncMock(return_value='')), patch.object(
            browser, 'read_profile_reel_view_counts', AsyncMock(return_value={'TARGET':100})):
            await enrich(object(), {'username':'creator'}, {'status':'success'})
        self.assertEqual(first['view_count'], '')
        self.assertEqual(second['view_count'], 100)
        self.assertTrue(second['view_count_observed_at'])
        self.assertEqual(second['view_count_source'], 'instagram_web_profile')

    def test_observation_time_inversion_has_no_view_delta(self):
        from src.reels.reel_records import calculate_reel_derived_fields
        previous = {'view_count':200, 'follower_count':10, 'view_count_observed_at':'2026-10-22T08:00:00Z'}
        current = {'view_count':100, 'follower_count':10, 'view_count_observed_at':'2026-10-22T04:00:00Z'}
        derived = calculate_reel_derived_fields(current, previous)
        self.assertEqual(derived['view_count_change'], '')
        self.assertEqual(derived['reaction_rate_change'], '')

    def test_completed_jobs_require_observed_identity_and_reject_late_old_views(self):
        from src.reels.reel_store import write_csv_records
        for verified, kind in ((False, ''), (True, ''), (True, 'profile_view_count'), ('preview', '')):
            with self.subTest(verified=verified, kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                paths = browser._android_metric_queue_paths(root)
                paths['completed'].mkdir(parents=True)
                first = {'url':'https://www.instagram.com/reels/TARGET/', 'collected_at':'2026-10-22T00:00:00Z', 'username':'creator', 'view_count':'', 'share_count':''}
                second = {**first, 'collected_at':'2026-10-22T04:00:00Z'}
                first['title'] = second['title'] = 'Winter outfit with a long caption'
                other_reel = {**first, 'url':'https://www.instagram.com/reels/OTHER/'}
                history = root/'.collector/reels_history_active.csv'
                write_csv_records(history, [first,second,other_reel], list(first))
                job = {'target':{'url':first['url'], 'collected_at':first['collected_at']}, 'completed_at':'2026-10-22T08:00:00Z',
                       'kind':kind, 'status':'collected', 'metrics': {'view_count':100} if kind else {'share_count':50}}
                if verified == 'preview':
                    job.update(observed_caption='Winter outfit …', observed_username='creator')
                elif verified:
                    job.update(observed_reel_url=first['url'], observed_username='creator')
                (paths['completed']/'job.json').write_text(json.dumps(job),encoding='utf-8')
                outcome = browser.apply_completed_android_metric_jobs(root, export_outputs=False)
                self.assertEqual(outcome['pending'], 0)
                _, rows = browser.read_csv_objects(history)
                self.assertEqual(rows[0]['view_count'], '')
                self.assertEqual(rows[0]['share_count'], '50' if verified and not kind else '')
                self.assertEqual(rows[2]['share_count'], '')
                if not verified:
                    self.assertTrue((paths['root']/'identity_unverified/job.json').exists())
