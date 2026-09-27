import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from transition_policy.contracts import MeasureStatus, PolicyMeasure


class PolicyContractTests(unittest.TestCase):
    def test_measure_can_be_open_ended(self):
        value = PolicyMeasure("m-7", "transport", date(2026, 1, 1), None, (), MeasureStatus.ACTIVE)
        self.assertIsNone(value.effective_until)

    def test_measure_retains_prerequisites(self):
        value = PolicyMeasure("m-8", "industry", date(2027, 1, 1), None, ("m-7",), MeasureStatus.PROPOSED)
        self.assertEqual(value.prerequisite_ids, ("m-7",))


if __name__ == "__main__":
    unittest.main()
