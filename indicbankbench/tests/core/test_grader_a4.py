"""A4 — declared prerequisites must precede dependent calls."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from indicbankbench.harness import grader  # noqa: E402

GOLD = {"dependencies": [["get_deposit_loan_rates", "calculate_fd_maturity"]]}


def _call(name):
    return {"function": {"name": name, "arguments": {}}}


def _transcript(*assistant_turns):
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    for calls in assistant_turns:
        msgs.append({"role": "assistant", "tool_calls": [_call(n) for n in calls]})
        msgs.append({"role": "tool", "content": "{}"})
    return msgs


class PrerequisiteOrder(unittest.TestCase):

    def test_same_turn_fails_and_says_same_turn(self):
        t = _transcript(["get_deposit_loan_rates", "calculate_fd_maturity"])
        result = grader._check_a4(t, GOLD, 1)  # noqa: SLF001
        self.assertFalse(result["ok"])
        self.assertIn("same turn", result["details"][0])
        self.assertIn("calculate_fd_maturity", result["details"][0])
        self.assertIn("get_deposit_loan_rates", result["details"][0])


    def test_reversed_order_fails_and_says_before(self):
        t = _transcript(["calculate_fd_maturity"], ["get_deposit_loan_rates"])
        result = grader._check_a4(t, GOLD, 1)  # noqa: SLF001
        self.assertFalse(result["ok"])
        self.assertIn("was called before its prerequisite", result["details"][0])


if __name__ == "__main__":
    unittest.main()
