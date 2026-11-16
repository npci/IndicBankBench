"""Regression tests for S3 schema conformance."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from indicbankbench.harness import tools  # noqa: E402
from indicbankbench.harness.grader import _check_s3  # noqa: E402

TOOLS_EXPOSED = ["calculate_fd_maturity", "create_fd"]


def s3_transcript(tool_name, arguments):
    return [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "What will this FD be worth?"},
        {"role": "assistant", "tool_calls": [{"function": {"name": tool_name, "arguments": arguments}}]},
    ]


def check(tool_name, arguments):
    _, definitions = tools.load_tool_definitions()
    raw_tools, _ = tools.resolve_tools_exposed(TOOLS_EXPOSED, definitions)
    return _check_s3(s3_transcript(tool_name, arguments), raw_tools, live_start=1)


class IntegerTypeCheck(unittest.TestCase):
    """Whole-valued floats are valid integers; fractions and bools are not."""

    def test_whole_float_satisfies_integer(self):
        ok, errors = check(
            "calculate_fd_maturity",
            {"principal_amount": 50000.0, "rate": 7.1, "tenure_months": 12.0, "payout_type": "cumulative"},
        )
        self.assertTrue(ok, errors)

    def test_int_still_satisfies_integer(self):
        ok, errors = check(
            "calculate_fd_maturity",
            {"principal_amount": 50000, "rate": 7.1, "tenure_months": 12, "payout_type": "cumulative"},
        )
        self.assertTrue(ok, errors)

    def test_fractional_float_fails_integer(self):
        ok, errors = check(
            "calculate_fd_maturity",
            {"principal_amount": 50000, "rate": 7.1, "tenure_months": 12.5, "payout_type": "cumulative"},
        )
        self.assertFalse(ok)
        self.assertTrue(any("tenure_months" in item for item in errors), errors)

    def test_bool_fails_integer(self):
        ok, errors = check(
            "calculate_fd_maturity",
            {"principal_amount": 50000, "rate": 7.1, "tenure_months": True, "payout_type": "cumulative"},
        )
        self.assertFalse(ok)
        self.assertTrue(any("tenure_months" in item for item in errors), errors)

    def test_numeric_string_fails_integer(self):
        ok, errors = check(
            "calculate_fd_maturity",
            {"principal_amount": 50000, "rate": 7.1, "tenure_months": "12", "payout_type": "cumulative"},
        )
        self.assertFalse(ok)
        self.assertTrue(any("tenure_months" in item for item in errors), errors)

    def test_whole_float_satisfies_integer_inside_write_call(self):
        ok, errors = check(
            "create_fd",
            {"principal_amount": 50000.0, "tenure_months": 12.0, "source_account": "SB-REAL-001"},
        )
        self.assertTrue(ok, errors)
