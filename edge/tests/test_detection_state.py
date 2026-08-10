import importlib.util
import pathlib
import sys
import unittest


MODULE_PATH = pathlib.Path(__file__).parents[1] / "pi-runtime" / "detection_state.py"
SPEC = importlib.util.spec_from_file_location("detection_state", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
OccupancyState = MODULE.OccupancyState


class OccupancyStateTest(unittest.TestCase):
    def state(self):
        return OccupancyState(
            window_frames=5,
            required_hits=3,
            min_confirm_duration_sec=2,
            clear_miss_frames=3,
            clear_duration_sec=2,
            evidence_interval_sec=5,
            cooldown_sec=10,
            spatial_dedup_meters=15,
        )

    def test_requires_n_of_m_and_duration(self):
        state = self.state()
        detection = {"score": 0.8, "class_name": "car"}
        self.assertEqual(state.update(now=0, detection=detection).action, "none")
        self.assertEqual(state.update(now=1, detection=None).action, "none")
        self.assertEqual(state.update(now=2, detection=detection).action, "none")
        self.assertEqual(state.update(now=3, detection=detection).action, "confirmed")

    def test_periodic_evidence_and_stable_clear(self):
        state = self.state()
        detection = {"score": 0.8, "class_name": "car"}
        for second in (0, 1, 2):
            decision = state.update(now=second, detection=detection)
        self.assertEqual(decision.action, "confirmed")
        self.assertEqual(state.update(now=6, detection=detection).action, "none")
        self.assertEqual(state.update(now=7, detection=detection).action, "evidence")
        self.assertEqual(state.update(now=8, detection=None).action, "none")
        self.assertEqual(state.update(now=9, detection=None).action, "none")
        self.assertEqual(state.update(now=10, detection=None).action, "cleared")

    def test_spatial_cooldown_allows_new_location(self):
        state = self.state()
        detection = {"score": 0.9, "class_name": "car"}
        location_a = (28.0, 121.0)
        for second in (0, 1, 2):
            state.update(now=second, detection=detection, location=location_a)
        for second in (3, 4, 5):
            state.update(now=second, detection=None, location=location_a)
        for second in (6, 7, 8):
            same = state.update(now=second, detection=detection, location=location_a)
        self.assertEqual(same.action, "none")
        location_b = (28.001, 121.0)
        actions = []
        for second in (9, 10, 11):
            moved = state.update(now=second, detection=detection, location=location_b)
            actions.append(moved.action)
        self.assertIn("confirmed", actions)


if __name__ == "__main__":
    unittest.main()
