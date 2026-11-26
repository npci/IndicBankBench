"""Date-range filtering in record-set mocks."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from indicbankbench.harness import mock_executor, tools  # noqa: E402

_, DEFS = tools.load_tool_definitions()

TXNS = [
    {"date": "2026-06-30T09:00:00", "amount": 500, "type": "debit", "channel": "upi",
     "reference_id": "T5", "status": "success", "running_balance": 1000},
    {"date": "2026-06-28T16:45:00", "amount": 400, "type": "debit", "channel": "upi",
     "reference_id": "T4", "status": "success", "running_balance": 1500},
    {"date": "2026-06-15", "amount": 300, "type": "credit", "channel": "neft",
     "reference_id": "T3", "status": "success", "running_balance": 1900},
    {"date": "2026-06-01T00:00:00", "amount": 200, "type": "debit", "channel": "card",
     "reference_id": "T2", "status": "success", "running_balance": 1600},
    {"date": "2026-05-20T12:00:00", "amount": 100, "type": "debit", "channel": "upi",
     "reference_id": "T1", "status": "success", "running_balance": 1800},
]


def _rows(args, records=TXNS):
    case = {"case_id": "test.date_filter.001", "tools_exposed": ["get_transaction_history"],
            "mock": {"get_transaction_history": {"kind": "record_set_filtered", "records": records}}}
    raw, _ = tools.resolve_tools_exposed(case["tools_exposed"], DEFS)
    out = json.loads(mock_executor.execute("get_transaction_history", dict(args), case, raw, {}))
    return out


def _refs(args, records=TXNS):
    return [r["reference_id"] for r in _rows(args, records)["transactions"]]


class DateRange(unittest.TestCase):
    def test_from_date_is_an_inclusive_lower_bound(self):
        self.assertEqual(["T5", "T4", "T3"], _refs({"account_id": "A1", "from_date": "2026-06-15"}))

    def test_to_date_is_an_inclusive_upper_bound(self):
        self.assertEqual(["T3", "T2", "T1"], _refs({"account_id": "A1", "to_date": "2026-06-15"}))

    def test_timestamp_record_on_the_end_date_is_included(self):
        # "2026-06-28T16:45:00" is later than "2026-06-28" as text; only the date part counts.
        self.assertIn("T4", _refs({"account_id": "A1", "to_date": "2026-06-28"}))

    def test_equality_filters_still_apply(self):
        self.assertEqual(["T5", "T4"], _refs({"account_id": "A1", "type": "debit", "from_date": "2026-06-16"}))

    def test_paging_applies_after_the_range(self):
        self.assertEqual(["T4"], _refs({"account_id": "A1", "from_date": "2026-06-15", "offset": 1, "limit": 1}))


class RecordsWithoutTheField(unittest.TestCase):
    """A record without a date cannot be placed in the range, so neither bound drops it."""

    MIXED = [dict(TXNS[0]), {k: v for k, v in TXNS[2].items() if k != "date"}]

    def test_from_date_keeps_a_record_without_a_date(self):
        self.assertEqual(["T5", "T3"], _refs({"account_id": "A1", "from_date": "2026-01-01"}, self.MIXED))


if __name__ == "__main__":
    unittest.main()
