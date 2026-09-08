"""Tests for mock execution."""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from indicbankbench.harness import mock_executor, tools  # noqa: E402

CARD = "CARD-TKN-TEST01"


def _case():
    return {
        "case_id": "test.merge.001",
        "tools_exposed": ["get_card_details", "set_card_controls"],
        "mock": {
            "get_card_details": {
                "kind": "record_set_filtered",
                "records": [{
                    "card_id": CARD, "card_type": "credit", "status": "active",
                    "controls": {
                        "atm": {"domestic": {"enabled": True, "daily_limit": 20000},
                                "international": {"enabled": True, "daily_limit": 15000}},
                        "online": {"domestic": {"enabled": True, "daily_limit": 50000},
                                   "international": {"enabled": True, "daily_limit": 25000}},
                    },
                }],
            },
            "set_card_controls": {
                "kind": "outcome_merged",
                "merge": {"state_from": "get_card_details", "match_on": "card_id", "field": "controls"},
                "output": {"status": "success", "message": "Card controls updated.", "reference_id": "REF-1"},
            },
        },
    }


class OutcomeMerged(unittest.TestCase):
    def setUp(self):
        self.case = _case()
        _, defs = tools.load_tool_definitions()
        self.raw, _ = tools.resolve_tools_exposed(self.case["tools_exposed"], defs)

    def _call(self, args, state):
        return json.loads(mock_executor.execute("set_card_controls", args, self.case, self.raw, state))

    def test_only_the_fields_sent_change(self):
        out = self._call({"card_id": CARD, "online": {"international": {"enabled": False}}}, {})
        c = out["controls"]
        self.assertFalse(c["online"]["international"]["enabled"])
        self.assertEqual(25000, c["online"]["international"]["daily_limit"], "omitted field must keep its value")
        self.assertTrue(c["atm"]["domestic"]["enabled"], "untouched channel must be untouched")

    def test_an_over_broad_write_is_visible_in_the_reply(self):
        out = self._call({"card_id": CARD, "online": {"international": {"enabled": False},
                                                      "domestic": {"enabled": False}}}, {})
        self.assertFalse(out["controls"]["online"]["domestic"]["enabled"])

    def test_a_second_call_sees_the_first(self):
        state = {}
        self._call({"card_id": CARD, "atm": {"international": {"enabled": False}}}, state)
        out = self._call({"card_id": CARD, "atm": {"international": {"daily_limit": 5000}}}, state)
        self.assertEqual({"enabled": False, "daily_limit": 5000}, out["controls"]["atm"]["international"])

    def test_a_read_after_a_write_agrees_with_it(self):
        state = {}
        self._call({"card_id": CARD, "atm": {"international": {"enabled": False}}}, state)
        read = json.loads(mock_executor.execute("get_card_details", {"card_ids": [CARD]},
                                                self.case, self.raw, state))
        self.assertFalse(read["cards"][0]["controls"]["atm"]["international"]["enabled"])

    def test_state_is_per_run_and_the_case_is_never_mutated(self):
        before = copy.deepcopy(self.case)
        self._call({"card_id": CARD, "atm": {"international": {"enabled": False}}}, {})
        fresh = json.loads(mock_executor.execute("get_card_details", {"card_ids": [CARD]},
                                                 self.case, self.raw, {}))
        self.assertTrue(fresh["cards"][0]["controls"]["atm"]["international"]["enabled"],
                        "a new run must start from the authored state")
        self.assertEqual(before, self.case, "the shared case object must not be mutated")

    def test_misconfiguration_raises_rather_than_guessing(self):
        for broken, label in (
            ({"state_from": "get_card_details", "match_on": "card_id"}, "missing field"),
            ({"state_from": "nope", "match_on": "card_id", "field": "controls"}, "bad state_from"),
            ({"state_from": "get_card_details", "match_on": "card_id", "field": "nope"}, "bad field"),
        ):
            case = _case()
            case["mock"]["set_card_controls"]["merge"] = broken
            with self.subTest(label):
                with self.assertRaises(mock_executor.MockExecutionError):
                    mock_executor.execute("set_card_controls", {"card_id": CARD}, case, self.raw, {})

    def test_a_forbidden_combination_is_refused(self):
        self.case["mock"]["set_card_controls"]["reject_when"] = {
            "payload_has_all_of": ["enabled", "daily_limit"],
            "output": {"status": "failed", "message": "Send them as sequential calls."},
        }
        refused = self._call({"card_id": CARD, "atm": {"international": {"enabled": False, "daily_limit": 5000}}}, {})
        self.assertEqual("failed", refused["status"])
        self.assertNotIn("controls", refused, "a refused call must not report a new state")
        for ok in ({"enabled": False}, {"daily_limit": 5000}):
            out = self._call({"card_id": CARD, "atm": {"international": ok}}, {})
            self.assertEqual("success", out["status"], f"{ok} alone must still be accepted")

    def test_an_unknown_entity_raises(self):
        with self.assertRaises(mock_executor.MockExecutionError):
            self._call({"card_id": "CARD-TKN-NOSUCH", "atm": {"domestic": {"enabled": False}}}, {})


if __name__ == "__main__":
    unittest.main()
