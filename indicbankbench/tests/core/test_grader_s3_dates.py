"""S3: date-typed arguments must be ISO dates (YYYY-MM-DD, or an ISO-8601 timestamp)."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from indicbankbench.harness import mock_executor, tools  # noqa: E402
from indicbankbench.harness.grader import _check_a1_a2_a3, _check_s3  # noqa: E402


def _s3(value):
    _, definitions = tools.load_tool_definitions()
    raw, _ = tools.resolve_tools_exposed(["get_transaction_history"], definitions)
    transcript = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "Show my transactions."},
        {"role": "assistant", "tool_calls": [{"function": {
            "name": "get_transaction_history", "arguments": {"account_id": "A1", "from_date": value}}}]},
    ]
    return _check_s3(transcript, raw, live_start=1)


class DateTypeCheck(unittest.TestCase):
    def test_iso_dates_and_timestamps_pass(self):
        for value in ("2026-02-15", "2026-02-15T10:30", "2026-02-15T10:30:00", "2026-02-15T10:30:00Z",
                      "2026-02-15T10:30:00.5+05:30"):
            with self.subTest(value=value):
                self.assertEqual((True, []), _s3(value))

    def test_non_iso_or_impossible_dates_fail(self):
        for value in ("15-02-2026", "2026/02/15", "Feb 15 2026", "20260215", "2026-02-30", "2026-2-5", 20260215):
            with self.subTest(value=value):
                ok, errors = _s3(value)
                self.assertFalse(ok)
                self.assertIn("is not of type date", errors[0])


QUOTE = "get_loan_foreclosure_quote"
TODAY = "2026-09-04"


def _quote_transcript(args):
    return [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "Can you tell me the payoff for LN-1 as of today?"},
        {"role": "assistant", "tool_calls": [{"function": {"name": QUOTE, "arguments": args}}]},
    ]


def _raw(names):
    _, definitions = tools.load_tool_definitions()
    return tools.resolve_tools_exposed(names, definitions)[0]


class AdvertisedTodayDefault(unittest.TestCase):
    """as_of_date advertises "today" as its default; sending it is not a malformed date."""

    def test_s3_accepts_the_advertised_default(self):
        ok, errors = _check_s3(_quote_transcript({"loan_ids": ["LN-1"], "as_of_date": "today"}),
                               _raw([QUOTE]), live_start=1)
        self.assertTrue(ok, errors)

    def test_the_mock_executes_it(self):
        case = {"case_id": "test.today.001", "tools_exposed": [QUOTE], "setup": {"current_date": TODAY},
                "mock": {QUOTE: {"kind": "record_set_filtered", "records": [
                    {"loan_id": "LN-1", "as_of_date": TODAY, "total_payoff": 103},
                    {"loan_id": "LN-2", "as_of_date": "2026-01-01", "total_payoff": 90}]}}}
        out = json.loads(mock_executor.execute(QUOTE, {"loan_ids": ["LN-1", "LN-2"], "as_of_date": "today"},
                                               case, _raw([QUOTE]), {}))
        self.assertEqual(["LN-1"], [q["loan_id"] for q in out["quotes"]])

    def _a3(self, gate, args):
        gold = {"tool_calls": [{"tool": QUOTE, "arguments": {}}], "arg_gate": {QUOTE: gate}}
        return _check_a1_a2_a3(_quote_transcript(args), gold, 1, _raw([QUOTE]), current_date=TODAY)

    def test_a3_reads_today_as_the_current_date(self):
        gate = {"required": {}, "optional_ok": {"as_of_date": {"allowed": [TODAY, "2026-09-05"]}}}
        _, _, a3, details = self._a3(gate, {"loan_ids": ["LN-1"], "as_of_date": "today"})
        self.assertTrue(a3, details["a3_errors"])


if __name__ == "__main__":
    unittest.main()
