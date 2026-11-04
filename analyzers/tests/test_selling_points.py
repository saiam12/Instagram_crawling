import unittest

from reel_analyzer import MarketingAnalysis


class SellingPointTests(unittest.TestCase):
    def test_discards_points_below_evidence_threshold(self):
        def point(confidence):
            return {
                "point": "여러 코디 비교",
                "evidence": "세 가지 코디가 연속으로 등장",
                "spoken_evidence": None,
                "visual_evidence": "세 가지 코디가 연속으로 등장",
                "on_screen_text_evidence": None,
                "start_second": 2.0,
                "end_second": 9.5,
                "appeal_type": "product_variety",
                "evidence_confidence": confidence,
            }

        analysis = MarketingAnalysis(
            strengths=[],
            weaknesses=[],
            notable_elements=[],
            selling_points=[point(0.49), point(0.5)],
        )

        self.assertEqual(len(analysis.selling_points), 1)
        self.assertEqual(analysis.selling_points[0].evidence_confidence, 0.5)


if __name__ == "__main__":
    unittest.main()
