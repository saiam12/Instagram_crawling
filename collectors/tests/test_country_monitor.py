import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'monitor' / 'backend'))
from classification import korean_value, matches_korean
from src.reels.korean_classification import apply_account_countries, apply_korean_overrides, classify_korean, classification_fields, normalize_account_country, save_account_country, save_korean_override


class CountryMonitorTests(unittest.TestCase):
    def test_country_is_shared_by_user_id_across_reels_and_rounds(self):
        rows=[{'user_id':'1','username':'old_name','account_country':'','collected_at':'2026-10-20T00:00:00Z'},
              {'user_id':'1','username':'new_name','account_country':'대한민국','collected_at':'2026-10-21T00:00:00Z'},
              {'user_id':'2','username':'old_name','account_country':''},
              {'username':'new_name','account_country':''}]
        with tempfile.TemporaryDirectory() as directory:
            normalized=apply_account_countries(rows,directory)
        self.assertEqual([row['account_country'] for row in normalized],['대한민국','대한민국','',''])
        self.assertEqual(rows[0]['account_country'],'')

    def test_latest_nonblank_country_wins_without_erasing_known_country(self):
        rows=[{'user_id':'1','account_country':'대한민국','collected_at':'2026-10-22T00:00:00Z'},
              {'user_id':'1','account_country':'일본','collected_at':'2026-10-20T00:00:00Z'},
              {'user_id':'1','account_country':'공유되지 않음','collected_at':'2026-10-23T00:00:00Z'}]
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual([row['account_country'] for row in apply_account_countries(rows,directory)],['대한민국']*3)
            save_account_country(directory,'1','renamed_user','미국')
            self.assertEqual([row['account_country'] for row in apply_account_countries(rows,directory)],['미국']*3)

    def test_confirmed_country_replaces_default_false_but_preserves_explicit_override(self):
        rows=[{'url':'https://www.instagram.com/reels/TARGET/','user_id':'1','username':'creator',
               'account_country':'','korean':'false','korean_source':'manual'},
              {'url':'https://www.instagram.com/reels/TARGET/','user_id':'1','username':'creator',
               'account_country':'대한민국','korean':'true','korean_source':'account_country'}]
        with tempfile.TemporaryDirectory() as directory:
            normalized=apply_korean_overrides(rows,directory)
            self.assertEqual([(row['korean'],row['korean_source']) for row in normalized],[('true','account_country')]*2)
            save_korean_override(directory,rows[0]['url'],False)
            normalized=apply_korean_overrides(rows,directory)
            self.assertTrue(all(row['korean']=='false' and row['korean_source']=='manual' for row in normalized))
            self.assertTrue(all(row['account_country']=='대한민국' for row in normalized))

    def test_country_evidence_change_invalidates_publication(self):
        from src.reels.fashion_beauty_collection import dataset_publication_signature
        from src.reels.fashion_beauty_scheduler import DatasetConfig
        with tempfile.TemporaryDirectory() as directory:
            dataset=DatasetConfig('fashion',Path(directory),())
            before=dataset_publication_signature(dataset)
            save_account_country(directory,'1','creator','대한민국')
            self.assertNotEqual(before,dataset_publication_signature(dataset))

    def test_undisclosed_country_is_blank_and_unknown(self):
        for value in ('공유되지 않음', 'Not shared', '-', ''):
            self.assertEqual(normalize_account_country(value), '')
            self.assertIsNone(classify_korean(account_country=value))
            self.assertEqual(classification_fields({'account_country': value})['account_country'], '')

    def test_monitor_filters_use_korean_without_country_inference(self):
        for raw, expected, selected in [('true', True, 'true'), ('false', False, 'false'), ('-', None, 'unknown')]:
            row = {'korean': raw, 'account_country': '대한민국', 'title': '한글'}
            self.assertIs(korean_value(row), expected)
            self.assertTrue(matches_korean(row, selected))
            self.assertFalse(matches_korean(row, 'false' if selected != 'false' else 'true'))
