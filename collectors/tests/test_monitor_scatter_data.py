import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "monitor" / "backend"), str(Path(__file__).resolve().parents[1])]
from analysis_data import read_analysis


class ScatterDataTests(unittest.TestCase):
    def test_projects_first_exposure_and_earliest_appeal_without_inventing_legacy_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reel_analysis.json"
            path.write_text(json.dumps([
                {"reel_id": "MODERN", "analysis": {
                    "hook": {"hook_type": "상품 선노출"},
                    "product": {"exists": True, "first_exposure": {
                        "start_second": 0, "visual_evidence": "첫 프레임의 재킷",
                    }},
                    "marketing_analysis": {"selling_points": [
                        {"start_second": 4}, {"start_second": None}, {"start_second": 1.5},
                    ]},
                    "scene_details": [{"layout_type": "product_shot", "on_screen_text": "재킷"}],
                }},
                {"reel_id": "LEGACY", "analysis": {"product": {"exists": True}}},
                {"reel_id": "ABSENT", "analysis": {"product": {"exists": False}}},
            ]), encoding="utf-8")
            rows = {row["reel_id"]: row for row in read_analysis(path)["rows"]}
            modern = rows["MODERN"]
            self.assertEqual(modern["hook_type"], "상품 선노출")
            self.assertEqual(modern["first_product_exposure_seconds"], 0)
            self.assertEqual(modern["first_product_exposure_evidence"], "첫 프레임의 재킷")
            self.assertEqual(modern["first_appeal_seconds"], 1.5)
            self.assertEqual(modern["scenes"][0]["features"]["화면 구성"], "product_shot")
            self.assertIsNone(rows["LEGACY"]["first_product_exposure_seconds"])
            self.assertIsNone(rows["LEGACY"]["first_appeal_seconds"])
            self.assertIs(rows["ABSENT"]["product_visible"], False)

    def test_content_type_aliases_share_category_without_rewriting_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'reel_analysis.json'
            records = [{'reel_id': f'REEL0{i}', 'analysis': {'content_type': value}}
                       for i, value in enumerate(['lookbook', 'fashion_lookbook', 'daily_lookbook',
                                                  'outfit_lookbook', 'streetwear_lookbook',
                                                  'product_demo_lookbook', 'new_format'])]
            path.write_text(json.dumps(records), encoding='utf-8')
            original = path.read_bytes()
            rows = read_analysis(path)['rows']
            self.assertEqual(sum(row['content_type'] == 'lookbook' for row in rows), 5)
            self.assertEqual({row['content_type'] for row in rows}, {'lookbook', 'product_demo_lookbook', 'new_format'})
            self.assertEqual({row['content_type_raw'] for row in rows}, {item['analysis']['content_type'] for item in records})
            self.assertEqual(path.read_bytes(), original)

    def test_metrics_join_latest_matching_reel_and_preserve_missing_and_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            analysis = root / "reel_analysis.json"
            analysis.write_text(json.dumps([
                {"reel_id": key, "analysis": {"scene_details": [{"end_second": 12.5}]}}
                for key in ["REEL01", "REEL02", "REEL03"]
            ]), encoding="utf-8")
            dataset = root / ".datasets" / "fashion"
            dataset.mkdir(parents=True)
            (dataset / "reels.json").write_text(json.dumps([
                {"url": "https://www.instagram.com/reel/REEL01/", "collected_at": "2026-10-29", "view_count": 99, "like_count": 10},
                {"url": "https://www.instagram.com/reels/REEL01/", "username":"author", "collected_at": "2026-10-30", "view_count": "1,234", "like_count": 0,
                 "comment_count":0,"share_count":10,"repost_count":2,"saved_count":3,"follower_count":400,"video_duration_seconds":15},
                {"url": "https://www.instagram.com/reel/REEL02/", "collected_at": "2026-10-30", "view_count": None, "like_count": "비공개"},
            ]), encoding="utf-8")
            (root/'users.json').write_text(json.dumps([{'username':'author','follower_count':900,'collected_at':'2026-10-31'}]),encoding='utf-8')
            rows = {row["reel_id"]: row for row in read_analysis(analysis)["rows"]}
            self.assertEqual((rows["REEL01"]["view_count"], rows["REEL01"]["like_count"]), (1234, 0))
            self.assertEqual(rows["REEL01"]["duration"], 12.5)
            self.assertEqual((rows['REEL01']['comment_count'],rows['REEL01']['share_count'],rows['REEL01']['saved_count'],rows['REEL01']['repost_count'],rows['REEL01']['follower_count']),(0,10,3,2,900))
            self.assertEqual(rows['REEL01']['video_duration_seconds'],15)
            self.assertEqual(rows['REEL01']['duration_source'],'collector')
            for key in ["REEL02", "REEL03"]:
                self.assertIsNone(rows[key]["view_count"])
                self.assertIsNone(rows[key]["like_count"])

    def test_missing_metrics_file_keeps_analysis_and_explains_missing_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reel_analysis.json"
            path.write_text('[{"reel_id":"REEL01","analysis":{"summary":"ok"}}]', encoding="utf-8")
            result = read_analysis(path)
            self.assertEqual(len(result["rows"]), 1)
            self.assertIsNone(result["rows"][0]["view_count"])
            self.assertTrue(result["warnings"])
