import json
import sys
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from collectors.monitor.backend import gemini_recommend, recommendations


class RecommendationErrorTests(unittest.TestCase):
    def test_collected_metrics_join_preserves_zero_private_values_and_latest_followers(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'data'/'collector';root.mkdir(parents=True)
            analysis=root/'reel_analysis.json'
            analysis.write_text(json.dumps([{'reel_id':'abcde','analysis':{'summary':'coat'}}]),encoding='utf-8')
            original=analysis.read_bytes()
            (root/'reels.json').write_text(json.dumps([
                {'url':'https://www.instagram.com/reel/abcde/','username':'author','view_count':900,'follower_count':50,'collected_at':'2026-11-01T00:00:00Z'},
                {'url':'https://www.instagram.com/reels/abcde/','username':'author','view_count':0,'like_count':'X','comment_count':0,'follower_count':1000,'collected_at':'2026-11-01T09:00:01+09:00'}]),encoding='utf-8')
            (root/'users.json').write_text(json.dumps([{'username':'author','follower_count':1500,'collected_at':'2026-11-01T00:01:00Z'}]),encoding='utf-8')
            with patch.object(recommendations,'DATASET_FILE',analysis),patch.object(recommendations,'PROJECT',Path(folder)):
                documents,_,_=recommendations.corpus()
            self.assertEqual(documents[0]['metrics'],{'view_count':0.,'comment_count':0.,'follower_count':1500.})
            self.assertEqual(documents[0]['private_metrics'],['like_count'])
            self.assertIn('share_count',documents[0]['missing_metrics'])
            self.assertEqual(analysis.read_bytes(),original)

    def test_follower_ranking_preserves_content_and_uses_account_scale(self):
        references=[{'reel_id':key,'score':score,'metrics':metrics} for key,score,metrics in
                    [('huge',1,{'follower_count':1000000}),('similar',.99,{'follower_count':1100}),('unknown',.98,{}),('unrelated',.2,{'follower_count':1000})]]
        ranked=recommendations.rank_followers(references,{'follower_count':1000})
        self.assertEqual(ranked[0]['reel_id'],'similar')
        self.assertNotEqual(ranked[0]['reel_id'],'unrelated')
        self.assertNotIn('follower_similarity',next(r for r in ranked if r['reel_id']=='unknown'))
        self.assertEqual([r['reference_id'] for r in ranked],['R1','R2','R3','R4'])
        self.assertEqual(recommendations.rank_followers(references,{}),references)

    def test_metrics_can_fall_back_to_existing_xlsx_export(self):
        from openpyxl import Workbook
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'data'/'collector';root.mkdir(parents=True)
            analysis=root/'reel_analysis.json'
            analysis.write_text(json.dumps([{'reel_id':'abcde','analysis':{'summary':'coat'}}]),encoding='utf-8')
            book=Workbook();sheet=book.active
            sheet.append(['url','username','collected_at','view_count','comment_count','follower_count'])
            sheet.append(['https://www.instagram.com/reel/abcde/','author','2026-11-01T00:00:00Z',1234,0,1000])
            book.save(root/'reels.xlsx');book.close()
            with patch.object(recommendations,'DATASET_FILE',analysis),patch.object(recommendations,'PROJECT',Path(folder)):
                documents,_,_=recommendations.corpus()
            self.assertEqual(documents[0]['metrics']['view_count'],1234)
            self.assertEqual(documents[0]['metrics']['comment_count'],0)
            self.assertTrue(documents[0]['metric_source'].endswith('reels.xlsx'))

    def test_optional_follower_input_is_validated(self):
        brief={'product':'coat','target':'men','purpose':'purchase'}
        self.assertEqual(recommendations.brief_values({**brief,'follower_count':'1,000'})['follower_count'],1000)
        self.assertEqual(recommendations.brief_values({**brief,'follower_count':0})['follower_count'],0)
        for invalid in (True,-1,'1.5','1e4',{},9007199254740992):
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):recommendations.brief_values({**brief,'follower_count':invalid})

    def test_country_filter_runs_before_embedding_and_excludes_unknown_from_foreign(self):
        service=recommendations.Recommendations()
        documents=[{'reel_id':key,'title':'coat','hashtags':'','analysis':{'summary':'coat'},'korean':country}
                   for key,country in [('korea',True),('abroad',False),('unset',None)]]
        payload={'dataset':recommendations.DATASET_ID,'brief':{'product':'coat','target':'men','purpose':'purchase'}}
        for selected,expected in [('all',['korea','abroad','unset']),('true',['korea']),('false',['abroad']),('unknown',['unset'])]:
            with self.subTest(selected=selected), patch.object(recommendations,'corpus',return_value=(documents,[],[])), patch.object(service,'worker',side_effect=ValueError('offline')) as worker:
                result=service.search({**payload,'country':selected})
                self.assertEqual([d['reel_id'] for d in worker.call_args.args[0]['documents']],expected)
                self.assertEqual(result['country'],selected)
                self.assertEqual(result['corpus_count'],len(expected))
                self.assertEqual(result['total_corpus_count'],3)
                self.assertTrue(all(r['reel_id'] in expected for r in result['references']))
        with self.assertRaises(ValueError):service.search({**payload,'country':'invalid'})

    def test_empty_country_selection_makes_no_embedding_request(self):
        service=recommendations.Recommendations()
        with patch.object(recommendations,'corpus',return_value=([{'korean':None}],[],[])),patch.object(service,'worker') as worker:
            result=service.search({'dataset':recommendations.DATASET_ID,'country':'true','brief':{'product':'coat','target':'men','purpose':'purchase'}})
        worker.assert_not_called()
        self.assertEqual(result['references'],[])

    def test_worker_accepts_direct_videos_without_a_common_plan(self):
        service = recommendations.Recommendations()
        result = {'provider':'antigravity-sheets','guide':{'kind':'videos','state':'completed','videos':[]}}
        response = SimpleNamespace(returncode=0,stdout=json.dumps(result))
        with patch.object(recommendations.subprocess,'run',return_value=response):
            self.assertEqual(service.worker({'brief':{},'references':[]}),result)

    def test_guide_cache_rejects_paths_outside_saved_images(self):
        service = recommendations.Recommendations()
        with patch.object(service, 'worker') as worker:
            for filename in ['../secret.png', 'test.png', '/tmp/file.png']:
                with self.subTest(filename=filename), self.assertRaises(ValueError):
                    service.guide(filename)
        worker.assert_not_called()

    def test_503_is_classified_without_exposing_provider_exception(self):
        result = gemini_recommend.failure_result(SimpleNamespace(code=503, message='fake-secret'))
        self.assertEqual(result['error_code'], 'gemini_unavailable')
        self.assertNotIn('fake-secret', json.dumps(result))

    def test_worker_and_job_preserve_safe_503_reason(self):
        service = recommendations.Recommendations()
        service.jobs['test'] = {'state': 'running'}
        response = SimpleNamespace(returncode=0, stdout=json.dumps({'error': 'fake-secret', 'error_code': 'gemini_unavailable'}))
        with patch.object(recommendations.subprocess, 'run', return_value=response):
            service._generate('test', {'brief': {}, 'references': []})
        self.assertIn('503', service.jobs['test']['error'])
        self.assertNotIn('fake-secret', service.jobs['test']['error'])
        self.assertFalse(service.running)

    def test_unknown_errors_remain_generic(self):
        service = recommendations.Recommendations()
        service.jobs['test'] = {'state': 'running'}
        with patch.object(service, 'worker', side_effect=ValueError('fake-secret')):
            service._generate('test', {'brief': {}, 'references': []})
        self.assertNotIn('fake-secret', service.jobs['test']['error'])
