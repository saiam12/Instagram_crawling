import asyncio
import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch

from src.reels import instagram_reels_browser as browser
from src.reels.android_reel_metrics import AndroidMetricResult,AndroidReelMetricsEnricher,merge_android_metrics
from tests.test_android_reel_metrics import FakeDriver,REEL_XML


class MetricExtractionAuditTests(unittest.TestCase):
    def test_verified_visible_share_button_without_number_is_zero(self):
        xml=REEL_XML.replace('resource-id="com.instagram.android:id/share_count"','resource-id="other"').replace('text="11.3K"','text=""')
        xml=xml.replace('</hierarchy>','<node content-desc="Share" resource-id="share_button"/></hierarchy>')
        with patch('src.reels.android_reel_metrics.time.sleep'):
            for recollection in (False,True):
                result=AndroidReelMetricsEnricher(driver=FakeDriver([xml]),ui_delay_seconds=.1).enrich(
                    'https://www.instagram.com/reels/TARGET/',expected_username='creator',recollection=recollection)
                self.assertEqual(result.metrics['share_count'],0)

    def test_explicit_zero_and_exact_text_are_preserved(self):
        from src.reels.android_reel_metrics import extract_recollection_metrics
        xml='<hierarchy><node text="0" resource-id="share_count"/><node text="19" resource-id="save_count"/></hierarchy>'
        metrics,compact=extract_recollection_metrics(xml)
        self.assertEqual(metrics,{'share_count':0,'saved_count':19})
        self.assertFalse(compact)

    def test_compact_counts_do_not_replace_exact_numbers(self):
        result=AndroidMetricResult(metrics={'share_count':33000,'saved_count':1200},compact_fields=('share_count','saved_count'),recollection=True)
        merged=merge_android_metrics({'share_count':33552,'saved_count':1234},result)
        self.assertEqual((merged['share_count'],merged['saved_count']),(33552,1234))

    def test_unreadable_likes_are_not_marked_private(self):
        merged=merge_android_metrics({'like_count':''},AndroidMetricResult(metrics={'share_count':10},recollection=True,like_count_private=False))
        self.assertEqual(merged['like_count'],'')

    def test_completion_marks_x_only_for_explicit_private_likes(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths=browser._android_metric_queue_paths(Path(temporary))
            paths['working'].mkdir(parents=True)
            for private in (False,True):
                working=paths['working']/f'{private}.json'
                job={'job_id':str(private),'recollection':True,'target':{'url':'https://www.instagram.com/reels/TARGET/','collected_at':'2026-10-22T00:00:00Z'}}
                working.write_text(json.dumps(job))
                result=AndroidMetricResult(metrics={'share_count':10},recollection=True,like_count_private=private)
                browser._write_android_metric_completion(paths,working,job,result)
                saved=json.loads((paths['completed']/working.name).read_text())
                self.assertEqual(saved['metrics'].get('like_count'),'X' if private else None)

    def test_publisher_cannot_bypass_identity_gate(self):
        from src.reels.fashion_beauty_collection import publish_dataset_outputs
        from src.reels.fashion_beauty_scheduler import DatasetConfig
        from src.reels.reel_store import write_csv_records
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); data=root/'.datasets/fashion'; history=data/'.collector/reels_history_active.csv'
            row={'url':'https://www.instagram.com/reels/TARGET/','username':'creator','collected_at':'2026-10-22T00:00:00Z','collection_number':'1','share_count':'33552'}
            write_csv_records(history,[row],list(row))
            completed=data/'.collector/android_metric_queue/completed';completed.mkdir(parents=True)
            (completed/'bad.json').write_text(json.dumps({'target':{'url':row['url'],'collected_at':row['collected_at']},'metrics':{'share_count':0},'status':'collected'}))
            publish_dataset_outputs(DatasetConfig('fashion',data,()))
            with (root/'reels.csv').open(encoding='utf-8-sig',newline='') as stream:
                self.assertEqual(next(csv.DictReader(stream))['share_count'],'33552')


class DurationAuditTests(unittest.IsolatedAsyncioTestCase):
    def test_duration_is_read_from_the_same_reels_dash_manifest(self):
        for manifest,expected in (("<MPD mediaPresentationDuration='PT32.733334S'/>",32.733334),
                                  ('<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT1M2.5S"/>',62.5),
                                  ('<MPD mediaPresentationDuration="PT0S"/>',None),
                                  ('<MPD mediaPresentationDuration="PTNaNS"/>',None),
                                  ('broken xml',None)):
            self.assertEqual(browser.metadata_from_media({'code':'TARGET','video_dash_manifest':manifest})['videoDurationSeconds'],expected)
        self.assertEqual(browser.metadata_from_media({'video_duration':12.5,'video_dash_manifest':"<MPD mediaPresentationDuration='PT99S'/>"})['videoDurationSeconds'],12.5)

    async def test_duration_only_missing_uses_profile_lookup_without_overwriting_views(self):
        row={'url':'https://www.instagram.com/reels/TARGET/','username':'creator','user_id':'1',
             'collected_at':'2026-10-22T00:00:00Z','view_count':100,'video_duration_seconds':''}
        store=SimpleNamespace(rows=[row],lock=asyncio.Lock(),dirty=False,flush=AsyncMock())
        enricher=browser.ProfileReelViewEnricher(store);enricher.track(row)
        async def lookup(page,username,targets,**kwargs):
            self.assertEqual(targets,{'TARGET'})
            self.assertEqual(kwargs['required_duration_shortcodes'],{'TARGET'})
            kwargs['initial_metadata']['TARGET']={'username':'creator','userId':'1','videoDurationSeconds':12.5}
            return {'TARGET':200}
        profile={'status':'success'}
        with patch.object(browser,'read_profile_reel_view_counts',side_effect=lookup) as read,patch.object(
            browser,'read_profile_account_country',AsyncMock(return_value='')):
            await enricher(object(),{'username':'creator','userId':'1'},profile)
        read.assert_awaited_once()
        self.assertEqual(row['video_duration_seconds'],12.5)
        self.assertEqual(row['view_count'],100)
        self.assertFalse(profile['profileReelDurationMissing'])

    async def test_view_success_does_not_end_lookup_before_required_duration(self):
        metadata={'TARGET':{'viewCount':100,'viewSourceField':'play_count','username':'creator'}}
        async def embedded(page,destination,**kwargs):
            if embedded_mock.await_count >= 2:
                destination['TARGET']['videoDurationSeconds']=12.5
        page=SimpleNamespace(url='https://www.instagram.com/creator/reels/',on=Mock(),remove_listener=Mock(),
                             goto=AsyncMock(),mouse=SimpleNamespace(wheel=AsyncMock()))
        with patch.object(browser,'collect_embedded_reel_metadata',side_effect=embedded) as embedded_mock,patch.object(
            browser,'read_exact_profile_grid_counts',AsyncMock(return_value={'TARGET':100})):
            counts=await browser.read_profile_reel_view_counts(page,'creator',{'TARGET'},initial_metadata=metadata,
                                                               required_duration_shortcodes={'TARGET'})
        self.assertEqual(counts,{'TARGET':100})
        self.assertEqual(metadata['TARGET']['videoDurationSeconds'],12.5)

    async def test_duration_only_response_can_finish_without_a_view_count(self):
        page=SimpleNamespace(url='https://www.instagram.com/creator/reels/',on=Mock(),remove_listener=Mock(),goto=AsyncMock())
        metadata={'TARGET':{'username':'creator','videoDurationSeconds':12.5}}
        with patch.object(browser,'collect_embedded_reel_metadata',AsyncMock()):
            counts=await browser.read_profile_reel_view_counts(page,'creator',{'TARGET'},initial_metadata=metadata,
                required_duration_shortcodes={'TARGET'},required_view_shortcodes=set())
        self.assertEqual(counts,{})
        page.goto.assert_not_awaited()

    async def test_duration_tail_retry_uses_target_detail_response(self):
        row={'url':'https://www.instagram.com/reels/TARGET/','username':'creator','user_id':'1',
             'collected_at':'2026-10-22T00:00:00Z','view_count':100,'video_duration_seconds':'','korean_source':'manual'}
        store=SimpleNamespace(rows=[row],lock=asyncio.Lock(),dirty=False,flush=AsyncMock())
        enricher=browser.ProfileReelViewEnricher(store);enricher.track(row)
        detail=SimpleNamespace(url=row['url'],on=Mock(),close=AsyncMock())
        page=SimpleNamespace(context=SimpleNamespace(new_page=AsyncMock(return_value=detail)))
        profile={'status':'success'}
        with patch.object(browser,'read_profile_reel_view_counts',AsyncMock()) as grid,patch.object(
            browser,'read_reel_detail_metadata',AsyncMock(return_value={'username':'creator','userId':'1','videoDurationSeconds':12.5})) as read:
            await enricher(page,{'username':'creator','userId':'1','durationRetry':'1'},profile)
        grid.assert_not_awaited()
        self.assertEqual(read.await_args.args[1],'TARGET')
        self.assertEqual(read.await_args.kwargs['required_fields'],('videoDurationSeconds',))
        self.assertEqual(row['video_duration_seconds'],12.5)
        self.assertEqual(row['view_count'],100)
        detail.close.assert_awaited_once()

    async def test_missing_duration_retries_once_and_access_failure_never_retries(self):
        from src.reels.instagram_follower_enricher import FollowerEnricher
        for status,expected in (('success',2),('rate_limited',1)):
            with tempfile.TemporaryDirectory() as temporary:
                lookup=AsyncMock(return_value={'status':status,'followerCount':100,'sourceField':'passive_profile_response.follower_count',
                                              'profileReelViewCounts':[100],'profileReelDurationMissing':True})
                enricher=FollowerEnricher(data_dir=temporary,lookup_impl=lookup)
                await enricher.track_user(user_id='1',username='creator')
                await enricher.drain()
                self.assertEqual(lookup.await_count,expected)
                if expected==2:
                    self.assertEqual(lookup.await_args.args[0]['durationRetry'],'1')

    async def test_duration_detail_429_remains_terminal_after_timeout(self):
        row={'url':'https://www.instagram.com/reels/TARGET/','username':'creator','user_id':'1',
             'collected_at':'2026-10-22T00:00:00Z','view_count':100,'video_duration_seconds':''}
        store=SimpleNamespace(rows=[row],lock=asyncio.Lock(),dirty=False,flush=AsyncMock())
        enricher=browser.ProfileReelViewEnricher(store);enricher.track(row)
        handlers={}
        detail=SimpleNamespace(url=row['url'],on=Mock(side_effect=lambda event,handler:handlers.update({event:handler})),close=AsyncMock())
        page=SimpleNamespace(context=SimpleNamespace(new_page=AsyncMock(return_value=detail)))
        profile={'status':'success'}
        original_timeout=asyncio.timeout
        def shortened_timeout(seconds):
            self.assertEqual(seconds,10)
            return original_timeout(.02)
        async def read(*_args,**_kwargs):
            handlers['response'](SimpleNamespace(status=429,url='https://www.instagram.com/graphql/query/'))
            await asyncio.Event().wait()
        with patch.object(browser,'read_reel_detail_metadata',side_effect=read),patch.object(
            browser.asyncio,'timeout',side_effect=shortened_timeout):
            await enricher.retry_detail_durations(page,'creator','1',profile)
        self.assertEqual(profile['status'],'rate_limited')
        self.assertEqual(row['view_count'],100)
        self.assertEqual(row['video_duration_seconds'],'')
        detail.close.assert_awaited_once()

    async def test_duration_detail_access_denial_stops_before_country_lookup(self):
        for path,status in (('accounts/login','login_required'),('challenge','challenge_required')):
            with self.subTest(status=status):
                row={'url':'https://www.instagram.com/reels/TARGET/','username':'creator','user_id':'1',
                     'collected_at':'2026-10-22T00:00:00Z','view_count':100,'video_duration_seconds':''}
                store=SimpleNamespace(rows=[row],lock=asyncio.Lock(),dirty=False,flush=AsyncMock())
                enricher=browser.ProfileReelViewEnricher(store);enricher.track(row)
                detail=SimpleNamespace(url=f'https://www.instagram.com/{path}/',on=Mock(),close=AsyncMock())
                page=SimpleNamespace(context=SimpleNamespace(new_page=AsyncMock(return_value=detail)))
                profile={'status':'success'}
                with patch.object(browser,'read_profile_reel_view_counts',AsyncMock()) as grid,patch.object(
                    browser,'read_reel_detail_metadata',AsyncMock(return_value={})),patch.object(
                    browser,'read_profile_account_country',AsyncMock(return_value='')) as country:
                    await enricher(page,{'username':'creator','userId':'1','durationRetry':'1'},profile)
                self.assertEqual(profile['status'],status)
                country.assert_not_awaited()
                grid.assert_not_awaited()
                self.assertEqual(row['view_count'],100)
                self.assertEqual(row['video_duration_seconds'],'')
                detail.close.assert_awaited_once()

    async def test_duration_requires_matching_author_and_saved_user_id(self):
        for observed in ({}, {'userId':'1'}, {'username':'creator'},
                         {'username':'other','userId':'1'}, {'username':'creator','userId':'2'}):
            with self.subTest(observed=observed):
                row={'url':'https://www.instagram.com/reels/TARGET/','username':'creator','user_id':'1',
                     'collected_at':'2026-10-22T00:00:00Z','view_count':100,'video_duration_seconds':''}
                store=SimpleNamespace(rows=[row],lock=asyncio.Lock(),dirty=False,flush=AsyncMock())
                enricher=browser.ProfileReelViewEnricher(store);enricher.track(row)
                profile={'_reel_metadata':{'TARGET':{**observed,'videoDurationSeconds':12.5}}}
                await enricher.enrich_profile_durations('creator','1',profile)
                self.assertEqual(row['video_duration_seconds'],'')
                self.assertEqual(row['view_count'],100)
                self.assertTrue(profile['profileReelDurationMissing'])
                store.flush.assert_not_awaited()

    async def test_duration_lookup_does_not_complete_before_author_is_observed(self):
        metadata={'TARGET':{'videoDurationSeconds':12.5,'userId':'1'}}
        async def embedded(_page,destination,**_kwargs):
            if embedded_mock.await_count >= 2:
                destination['TARGET']['username']='creator'
        page=SimpleNamespace(url='https://www.instagram.com/creator/reels/',on=Mock(),remove_listener=Mock(),goto=AsyncMock())
        with patch.object(browser,'collect_embedded_reel_metadata',side_effect=embedded) as embedded_mock,patch.object(
            browser,'read_exact_profile_grid_counts',AsyncMock(return_value={})):
            counts=await browser.read_profile_reel_view_counts(page,'creator',{'TARGET'},initial_metadata=metadata,
                required_duration_shortcodes={'TARGET'},required_view_shortcodes=set())
        self.assertEqual(counts,{})
        self.assertEqual(metadata['TARGET'].get('username'),'creator')
        self.assertEqual(embedded_mock.await_count,2)

    async def test_duration_detail_redirect_remains_terminal_after_timeout(self):
        for path,status in (('accounts/login','login_required'),('challenge','challenge_required')):
            with self.subTest(status=status):
                row={'url':'https://www.instagram.com/reels/TARGET/','username':'creator','user_id':'1',
                     'collected_at':'2026-10-22T00:00:00Z','view_count':100,'video_duration_seconds':''}
                store=SimpleNamespace(rows=[row],lock=asyncio.Lock(),dirty=False,flush=AsyncMock())
                enricher=browser.ProfileReelViewEnricher(store);enricher.track(row)
                detail=SimpleNamespace(url=row['url'],on=Mock(),close=AsyncMock())
                page=SimpleNamespace(context=SimpleNamespace(new_page=AsyncMock(return_value=detail)))
                profile={'status':'success'}
                original_timeout=asyncio.timeout
                async def read(*_args,**_kwargs):
                    detail.url=f'https://www.instagram.com/{path}/'
                    await asyncio.Event().wait()
                with patch.object(browser,'read_reel_detail_metadata',side_effect=read),patch.object(
                    browser.asyncio,'timeout',side_effect=lambda _seconds:original_timeout(.02)):
                    await enricher.retry_detail_durations(page,'creator','1',profile)
                self.assertEqual(profile['status'],status)
                self.assertEqual(profile['profileReelDurationRetryStatus'],'timeout')
                detail.close.assert_awaited_once()

    async def test_duration_detail_429_cancels_before_another_navigation(self):
        row={'url':'https://www.instagram.com/reels/TARGET/','username':'creator','user_id':'1',
             'collected_at':'2026-10-22T00:00:00Z','view_count':100,'video_duration_seconds':''}
        store=SimpleNamespace(rows=[row],lock=asyncio.Lock(),dirty=False,flush=AsyncMock())
        enricher=browser.ProfileReelViewEnricher(store);enricher.track(row)
        handlers=[]
        detail=SimpleNamespace(url=row['url'],on=lambda _event,handler:handlers.append(handler),
            remove_listener=lambda _event,handler:handlers.remove(handler),close=AsyncMock(),
            goto=AsyncMock(return_value=SimpleNamespace(status=200)))
        sent=False
        async def settle(_milliseconds):
            nonlocal sent
            if not sent:
                sent=True
                response=SimpleNamespace(status=429,url='https://www.instagram.com/graphql/query/',headers={},
                    request=SimpleNamespace(resource_type='xhr'),text=AsyncMock(return_value='{}'))
                for handler in list(handlers):
                    handler(response)
            await asyncio.sleep(0)
        detail.wait_for_timeout=settle
        page=SimpleNamespace(context=SimpleNamespace(new_page=AsyncMock(return_value=detail)))
        profile={'status':'success'}
        with patch.object(browser,'collect_embedded_reel_metadata',AsyncMock()),patch.object(
            browser,'EXACT_REEL_DETAIL_TIMEOUT_MILLISECONDS',0):
            await enricher.retry_detail_durations(page,'creator','1',profile)
        self.assertEqual(profile['status'],'rate_limited')
        detail.goto.assert_awaited_once()
        detail.close.assert_awaited_once()

    async def test_duration_detail_ignores_auxiliary_429_responses(self):
        for url in ('https://example.com/metrics/','https://www.instagram.com/ajax/bulk-route-definitions/'):
            with self.subTest(url=url):
                row={'url':'https://www.instagram.com/reels/TARGET/','username':'creator','user_id':'1',
                     'collected_at':'2026-10-22T00:00:00Z','view_count':100,'video_duration_seconds':''}
                store=SimpleNamespace(rows=[row],lock=asyncio.Lock(),dirty=False,flush=AsyncMock())
                enricher=browser.ProfileReelViewEnricher(store);enricher.track(row)
                handlers={}
                detail=SimpleNamespace(url=row['url'],on=Mock(side_effect=lambda event,handler:handlers.update({event:handler})),close=AsyncMock())
                page=SimpleNamespace(context=SimpleNamespace(new_page=AsyncMock(return_value=detail)))
                profile={'status':'success'}
                async def read(*_args,**_kwargs):
                    handlers['response'](SimpleNamespace(status=429,url=url))
                    await asyncio.sleep(0)
                    return {'username':'creator','userId':'1','videoDurationSeconds':12.5}
                with patch.object(browser,'read_reel_detail_metadata',side_effect=read):
                    await enricher.retry_detail_durations(page,'creator','1',profile)
                self.assertEqual(profile['status'],'success')
                self.assertEqual(row['video_duration_seconds'],12.5)
                self.assertEqual(row['view_count'],100)
                detail.close.assert_awaited_once()

    async def test_duration_detail_parent_cancellation_propagates_and_closes_page(self):
        for restricted in (False,True):
            with self.subTest(restricted=restricted):
                row={'url':'https://www.instagram.com/reels/TARGET/','username':'creator','user_id':'1',
                     'collected_at':'2026-10-22T00:00:00Z','view_count':100,'video_duration_seconds':''}
                store=SimpleNamespace(rows=[row],lock=asyncio.Lock(),dirty=False,flush=AsyncMock())
                enricher=browser.ProfileReelViewEnricher(store);enricher.track(row)
                handlers={}
                detail=SimpleNamespace(url=row['url'],on=Mock(side_effect=lambda event,handler:handlers.update({event:handler})),close=AsyncMock())
                page=SimpleNamespace(context=SimpleNamespace(new_page=AsyncMock(return_value=detail)))
                started=asyncio.Event()
                async def read(*_args,**_kwargs):
                    started.set()
                    await asyncio.Event().wait()
                with patch.object(browser,'read_reel_detail_metadata',side_effect=read):
                    task=asyncio.create_task(enricher.retry_detail_durations(page,'creator','1',{'status':'success'}))
                    await asyncio.wait_for(started.wait(),.3)
                    if restricted:
                        handlers['response'](SimpleNamespace(status=429,url='https://www.instagram.com/graphql/query/'))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                detail.close.assert_awaited_once()

    async def test_profile_tail_callback_sees_metadata_that_arrives_after_view_callback(self):
        ready=asyncio.Event()
        handlers={}
        page=SimpleNamespace(url='https://www.instagram.com/creator/reels/',on=Mock(side_effect=lambda event,handler:handlers.update({event:handler})),
                             remove_listener=Mock(),evaluate=AsyncMock(return_value={}),wait_for_timeout=AsyncMock())
        async def navigate(*args,**kwargs):
            handlers['response'](object())
            return SimpleNamespace(status=200)
        page.goto=AsyncMock(side_effect=navigate)
        async def profile_response(response,username,destination):
            destination.update(userId='1',followerCount=100)
        async def media_response(response,destination):
            await ready.wait()
            destination['TARGET']={'videoDurationSeconds':12.5,'username':'creator','userId':'1'}
        async def first_callback(profile):
            self.assertNotIn('TARGET',profile['_reel_metadata'])
            ready.set()
        tail=AsyncMock()
        with patch.object(browser,'_collect_profile_snapshot_from_response',side_effect=profile_response),patch.object(
            browser,'_collect_reel_metadata_from_response',side_effect=media_response),patch.object(browser,'read_profile_account_country',AsyncMock(return_value='')):
            result=await browser.request_web_follower_count(page,'creator',reels=True,on_profile=first_callback,on_metadata_complete=tail)
        self.assertEqual(result['status'],'success')
        tail.assert_awaited_once()
        self.assertEqual(tail.await_args.args[0]['_reel_metadata']['TARGET']['videoDurationSeconds'],12.5)

    def test_positive_nested_duration_overrides_zero_placeholder(self):
        meta=browser.metadata_from_media({'video_duration':0,'clips_metadata':{'video_duration':12.5}})
        self.assertEqual(meta['videoDurationSeconds'],12.5)
        self.assertEqual(browser.merge_reel_metadata({'videoDurationSeconds':0},{'videoDurationSeconds':12.5})['videoDurationSeconds'],12.5)

    def test_anonymous_refresh_fills_missing_but_preserves_known_duration(self):
        for old,expected in (('',12.5),('13.75',13.75)):
            result=browser.build_anonymous_refresh_record({'video_duration_seconds':old}, {'video_duration_seconds':12.5})
            self.assertEqual(result['video_duration_seconds'],expected)

    async def test_duration_waits_for_inflight_response_and_understands_csv_numbers(self):
        for old,expected in (('',12.5),(0,12.5),('13.75','13.75')):
            row={'url':'https://www.instagram.com/reels/TARGET/','username':'creator','user_id':'1','collected_at':'2026-10-22T00:00:00Z','video_duration_seconds':old}
            store=SimpleNamespace(rows=[row],lock=asyncio.Lock(),dirty=False,flush=AsyncMock())
            enricher=browser.ProfileReelViewEnricher(store);enricher.track(row)
            metadata={}
            async def response():
                await asyncio.sleep(.001)
                metadata['TARGET']={'username':'creator','userId':'1','videoDurationSeconds':12.5}
            task=asyncio.create_task(response())
            await enricher.enrich_profile_durations('creator','1',{'_reel_metadata':metadata,'_reel_response_tasks':{task}})
            self.assertEqual(row['video_duration_seconds'],expected)
