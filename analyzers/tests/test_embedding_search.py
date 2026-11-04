import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import embedding_search as embedding

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from collectors.monitor.backend import recommendations as rec


def vector(index=0):
    return [float(i == index) for i in range(embedding.DIMENSIONS)]


class EmbeddingTests(unittest.TestCase):
    def test_account_follower_count_does_not_reembed_the_search_query(self):
        brief={'product':'coat','target':'men','purpose':'purchase'}
        self.assertEqual(embedding.query_text({**brief,'follower_count':10}),embedding.query_text({**brief,'follower_count':1000000}))

    def test_batches_limit_both_document_count_and_long_text_volume(self):
        texts={str(i):'small' for i in range(19)}
        self.assertEqual([len(b) for b in embedding.batches(list(texts),texts)],[8,8,3])
        texts={str(i):'x'*6000 for i in range(5)}
        self.assertEqual([len(b) for b in embedding.batches(list(texts),texts)],[1]*5)
        with self.assertRaises(ValueError):
            list(embedding.batches(['large'],{'large':'x'*embedding.MINUTE_INPUT_BUDGET}))

    def test_rolling_budget_waits_without_changing_text_and_reuses_cache(self):
        clock=[1000.]
        calls=[]
        texts=[str(i)+'x'*6000 for i in range(5)]
        def generate(batch):
            calls.append((clock[0],list(batch)))
            return [vector() for _ in batch]
        def sleep(seconds): clock[0]+=seconds
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(embedding.time,'time',side_effect=lambda:clock[0]), \
             patch.object(embedding.time,'sleep',side_effect=sleep) as wait, \
             patch.object(embedding,'request_embeddings',side_effect=generate):
            path=Path(directory)/'vectors.sqlite3'
            _,stats=embedding.vectors(texts,path)
            self.assertEqual(stats,{'requested':5,'cached':0})
            self.assertEqual([text for _,batch in calls for text in batch],texts)
            self.assertGreaterEqual(calls[3][0]-calls[0][0],60)
            for start,_ in calls:
                total=sum(embedding.input_budget(batch) for stamp,batch in calls if start <= stamp < start+60)
                self.assertLessEqual(total,embedding.MINUTE_INPUT_BUDGET)
            self.assertTrue(wait.called)
            wait.reset_mock()
            _,stats=embedding.vectors(texts,path)
            self.assertEqual(stats,{'requested':0,'cached':5})
            wait.assert_not_called()

    def test_input_budget_is_shared_between_separate_cache_sessions(self):
        clock=[1000.]
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(embedding.time,'time',side_effect=lambda:clock[0]), \
             patch.object(embedding.time,'sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)) as wait, \
             patch.object(embedding,'request_embeddings',side_effect=lambda texts:[vector() for _ in texts]):
            path=Path(directory)/'vectors.sqlite3'
            embedding.vectors(['a'*7000,'b'*7000,'c'*7000],path)
            embedding.vectors(['d'*7000],path)
            self.assertTrue(wait.called)
            self.assertGreaterEqual(clock[0],1060)

    def test_cache_reuses_unchanged_text_and_only_requests_changes(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(embedding, 'request_embeddings', side_effect=lambda texts: [vector() for _ in texts]) as request:
            path = Path(directory) / 'vectors.sqlite3'
            _, stats = embedding.vectors(['document', 'query', 'document'], path)
            self.assertEqual(stats, {'requested': 2, 'cached': 0})
            _, stats = embedding.vectors(['document', 'query'], path)
            self.assertEqual(stats, {'requested': 0, 'cached': 2})
            _, stats = embedding.vectors(['document', 'changed query'], path)
            self.assertEqual(stats, {'requested': 1, 'cached': 1})
            self.assertEqual(request.call_count, 2)

    def test_batch_has_one_request_per_document_and_records_usage(self):
        pool = Mock()
        pool.acquire_blocking.return_value = SimpleNamespace(api_key='fake')
        response = Mock()
        response.json.return_value = {'embeddings': [{'values': vector()}, {'values': vector(1)}], 'usageMetadata': {'promptTokenCount': 8, 'totalTokenCount': 8}}
        with patch.object(embedding, 'SheetsKeyPool', return_value=pool), patch.object(embedding.requests, 'post', return_value=response) as post:
            values = embedding.request_embeddings(['one', 'two'])
        requests = post.call_args.kwargs['json']['requests']
        self.assertEqual([r['content']['parts'][0]['text'] for r in requests], ['one', 'two'])
        self.assertEqual(requests[0]['embedContentConfig'], {'outputDimensionality': 768})
        self.assertEqual(values, [vector(), vector(1)])
        self.assertEqual(pool.finish.call_args.kwargs['response'].usage_metadata.total_token_count, 8)

    def test_invalid_vectors_are_rejected(self):
        for value in ([], [0.] * 768, [float('nan')] * 768, [True] * 768, [1e308] * 768):
            with self.assertRaises(ValueError):
                embedding.normalize(value)

    def test_completed_batch_survives_later_api_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'vectors.sqlite3'
            with patch.object(embedding, 'BATCH_SIZE', 1), patch.object(embedding, 'request_embeddings', side_effect=[[vector()], ValueError('quota')]):
                with self.assertRaisesRegex(ValueError, 'quota'):
                    embedding.vectors(['first', 'second'], path)
            with patch.object(embedding, 'request_embeddings', return_value=[vector(1)]) as request:
                _, stats = embedding.vectors(['first', 'second'], path)
            request.assert_called_once_with(['second'])
            self.assertEqual(stats, {'requested': 1, 'cached': 1})

    def test_failed_response_is_finished_as_failure(self):
        pool = Mock()
        pool.acquire_blocking.return_value = SimpleNamespace(api_key='fake')
        response = Mock()
        response.json.return_value = {'embeddings': []}
        with patch.object(embedding, 'SheetsKeyPool', return_value=pool), patch.object(embedding.requests, 'post', return_value=response):
            with self.assertRaisesRegex(ValueError, 'count mismatch'):
                embedding.request_embeddings(['one'])
        self.assertIsInstance(pool.finish.call_args.kwargs['error'], ValueError)

    def test_semantic_match_without_shared_words_can_be_retrieved(self):
        documents = [{'reel_id': 'abcde', 'title': '전신 코디', 'analysis': {'summary': '겨울 패션'}, 'hashtags': ''}]
        with patch.object(embedding, 'vectors', return_value=([vector(), vector()], {'requested': 0, 'cached': 2})):
            result = embedding.semantic_search(documents, {'product': '아우터'})
        refs = rec.hybrid_retrieve(documents, {'product': 'unmatchedword'}, result['semantic'])
        self.assertEqual(refs[0]['cosine_similarity'], 1.)
        self.assertEqual(refs[0]['bm25_score'], 0)

    def test_api_failure_falls_back_without_exposing_error_details(self):
        document = {'reel_id': 'abcde', 'title': '아우터', 'analysis': {'summary': '아우터'}, 'hashtags': ''}
        service = rec.Recommendations()
        with patch.object(rec, 'corpus', return_value=([document], [], [])), patch.object(service, 'worker', side_effect=ValueError('secret-key')):
            result = service.search({'dataset': rec.DATASET_ID, 'brief': {'product': '아우터', 'target': '남성', 'purpose': '구매'}})
        self.assertEqual(result['search_method'], 'bm25')
        self.assertTrue(result['references'])
        self.assertNotIn('secret-key', str(result))


if __name__ == '__main__':
    unittest.main()
