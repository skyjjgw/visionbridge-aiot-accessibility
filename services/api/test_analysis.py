from __future__ import annotations

import unittest

from services.api.analysis import ExternalAnalysisClient, local_quality_analysis


class AnalysisContractTest(unittest.TestCase):
    def test_local_quality_flags_invalid_and_duplicate_inputs(self) -> None:
        decision = local_quality_analysis(
            {
                "source": "edge",
                "category": "unknown-model-class",
                "description": "短",
                "lat": 180,
                "lng": 220,
                "confidence": 0.2,
                "duplicateRisk": 0.9,
            }
        )
        self.assertFalse(decision.valid)
        self.assertTrue(decision.needs_manual_review)
        self.assertIn("invalid_coordinates", decision.quality_flags)
        self.assertIn("probable_duplicate", decision.quality_flags)

    def test_advantech_dify_dispatch_result_is_structured(self) -> None:
        raw, run_id = ExternalAnalysisClient._extract_decision(
            {
                "data": {
                    "id": "run-1",
                    "outputs": {
                        "dispatchResult": '{"action":"dispatch_now","priority":"high","summary":"需要复核"}'
                    },
                }
            }
        )
        self.assertEqual(run_id, "run-1")
        self.assertEqual(raw["priority"], "high")


if __name__ == "__main__":
    unittest.main()
