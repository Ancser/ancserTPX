import unittest

from backend.api.routes import (
    ML_RR_VALUES,
    _normalize_rr_ratio,
)


class FixedRrRangeTests(unittest.TestCase):
    def test_public_fixed_rr_range_is_one_through_six(self):
        self.assertEqual(ML_RR_VALUES, tuple(range(1, 7)))
        self.assertEqual(_normalize_rr_ratio(0), 1)
        self.assertEqual(_normalize_rr_ratio(7), 6)


if __name__ == "__main__":
    unittest.main()
