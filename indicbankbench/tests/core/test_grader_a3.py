"""A3: argument comparison and forbidden values."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from indicbankbench.harness.grader import _check_call_against_gate, _contains_forbidden_id  # noqa: E402


class Arguments(unittest.TestCase):
    def test_list_order_is_ignored(self):
        gate = {"required": {"account_ids": ["A", "B"]}}
        self.assertEqual([], _check_call_against_gate("t", {"account_ids": ["B", "A"]}, gate))
        self.assertTrue(_check_call_against_gate("t", {"account_ids": ["A"]}, gate))
        self.assertTrue(_check_call_against_gate("t", {"account_ids": ["A", "A", "B"]}, gate))

    def test_an_omitted_argument_takes_its_default(self):
        gate = {"required": {"account_id": "SAV-1", "leaves": 25}}
        self.assertEqual([], _check_call_against_gate("t", {"account_id": "SAV-1"}, gate, {"leaves": 25}))
        self.assertTrue(_check_call_against_gate("t", {"account_id": "SAV-1"}, gate, {}))
        null = {"account_id": "SAV-1", "leaves": None}
        self.assertEqual([], _check_call_against_gate("t", null, gate, {"leaves": 25}))


class ForbiddenValues(unittest.TestCase):
    def test_strings_match_exactly_and_numbers_by_value(self):
        self.assertTrue(_contains_forbidden_id(["CARD-2", "CARD-1"], {"CARD-1"}))
        self.assertFalse(_contains_forbidden_id("CARD-10", {"CARD-1"}))
        self.assertTrue(_contains_forbidden_id(400000.0, {400000}))

    def test_nested_values_are_found(self):
        payload = {"international": {"enabled": True, "limits": {"daily_limit": 500000}}}
        self.assertTrue(_contains_forbidden_id(payload, {500000}))
        gate = {"forbidden_ids": [500000]}
        self.assertIn("forbidden", _check_call_against_gate("set_card_controls", payload, gate)[0])


if __name__ == "__main__":
    unittest.main()
