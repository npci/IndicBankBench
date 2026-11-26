"""Regression tests for S1 ID provenance."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from indicbankbench.harness import mock_executor, tools  # noqa: E402
from indicbankbench.harness.grader import _check_s1, _is_id_key  # noqa: E402


LOGIN = {"customer_id": "CUST-1", "card_id": "CARD-REAL-001"}
CASE = {
    "tools_exposed": ["block_card"],
    "mock": {
        "block_card": {
            "kind": "outcome_fixed",
            "output": {"status": "success", "message": "Card blocked.", "reference_id": "REF-77"},
        }
    },
}


def block_card_transcript(card_id):
    _, definitions = tools.load_tool_definitions()
    raw_tools, _ = tools.resolve_tools_exposed(CASE["tools_exposed"], definitions)
    arguments = {"card_id": card_id, "reason": "lost"}
    output = mock_executor.execute("block_card", arguments, CASE, raw_tools, state={})
    return [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "Please block my card."},
        {"role": "assistant", "tool_calls": [{"function": {"name": "block_card", "arguments": arguments}}]},
        {"role": "tool", "content": output},
    ]


class S1Provenance(unittest.TestCase):
    def test_self_echo_does_not_validate_a_fabricated_id(self):
        transcript = block_card_transcript("CARD-9999-INVENTED")
        ok, violations = _check_s1(transcript, LOGIN, live_start=1)
        self.assertFalse(ok)
        self.assertTrue(any("CARD-9999-INVENTED" in item for item in violations))

    def test_login_context_id_is_valid(self):
        ok, violations = _check_s1(block_card_transcript("CARD-REAL-001"), LOGIN, live_start=1)
        self.assertTrue(ok, violations)

    def test_customer_stated_id_is_valid(self):
        transcript = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "What is the status of request REQ-888?"},
            {"role": "assistant", "tool_calls": [{"function": {"name": "get_request_status", "arguments": {"request_id": "REQ-888"}}}]},
        ]
        ok, violations = _check_s1(transcript, LOGIN, live_start=1)
        self.assertTrue(ok, violations)

    def test_earlier_tool_output_id_is_valid(self):
        transcript = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "Block my card."},
            {"role": "assistant", "tool_calls": [{"function": {"name": "get_cards", "arguments": {}}}]},
            {"role": "tool", "content": json.dumps({"cards": [{"card_id": "CARD-FROM-READ"}]})},
            {"role": "assistant", "tool_calls": [{"function": {"name": "block_card", "arguments": {"card_id": "CARD-FROM-READ", "reason": "lost"}}}]},
        ]
        ok, violations = _check_s1(transcript, LOGIN, live_start=1)
        self.assertTrue(ok, violations)

    def test_live_assistant_prose_is_not_a_valid_source(self):
        transcript = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "Check my request."},
            {"role": "assistant", "content": "Looking up REQ-777."},
            {"role": "assistant", "tool_calls": [{"function": {"name": "get_request_status", "arguments": {"request_id": "REQ-777"}}}]},
        ]
        ok, _ = _check_s1(transcript, LOGIN, live_start=1)
        self.assertFalse(ok)


LOGIN_FD = {"customer_id": "CUST-101", "linked_accounts": [{"account_id": "SB-REAL-001"}]}


def create_fd_transcript(source_account):
    return [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "Please book an FD of Rs 50,000 from SB-REAL-001."},
        {"role": "assistant", "content": "I will book it. Confirm?"},
        {"role": "user", "content": "Yes, go ahead."},
        {"role": "assistant", "tool_calls": [{"function": {"name": "create_fd", "arguments": {
            "principal_amount": 50000,
            "tenure_months": 12,
            "source_account": source_account,
            "payout_type": "cumulative",
        }}}]},
    ]


def close_deposit_transcript(payout_account):
    return [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "Close deposit FD-REAL-001 and credit SB-REAL-001."},
        {"role": "assistant", "tool_calls": [{"function": {"name": "close_deposit", "arguments": {
            "deposit_id": "FD-REAL-001",
            "payout_account": payout_account,
        }}}]},
    ]


class S1NonIdAccountParameters(unittest.TestCase):
    """Identifiers named *_account are still identifiers and must be provenance-checked."""

    def test_fabricated_source_account_fails_s1(self):
        ok, violations = _check_s1(create_fd_transcript("SB-FAKE-999999"), LOGIN_FD, live_start=1)
        self.assertFalse(ok)
        self.assertTrue(any("source_account" in item for item in violations), violations)

    def test_login_context_source_account_is_valid(self):
        ok, violations = _check_s1(create_fd_transcript("SB-REAL-001"), LOGIN_FD, live_start=1)
        self.assertTrue(ok, violations)

    def test_fabricated_payout_account_fails_s1(self):
        ok, violations = _check_s1(
            close_deposit_transcript("SB-FAKE-999999"),
            {"deposit_id": "FD-REAL-001", "linked_accounts": [{"account_id": "SB-REAL-001"}]},
            live_start=1,
        )
        self.assertFalse(ok)
        self.assertTrue(any("payout_account" in item for item in violations), violations)

    def test_the_advertised_default_is_not_an_invented_id(self):
        raw, _ = tools.resolve_tools_exposed(["close_deposit"], tools.load_tool_definitions()[1])
        default = next(i["default"] for i in raw["close_deposit"]["inputs"] if i["name"] == "payout_account")
        transcript = close_deposit_transcript(default)
        self.assertTrue(_check_s1(transcript, {"deposit_id": "FD-REAL-001"}, 1, raw)[0])
        self.assertFalse(_check_s1(transcript, {"deposit_id": "FD-REAL-001"}, 1)[0])


def transfer_transcript(beneficiary_account):
    return [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "Send Rs 15,000 every month to their account 5021004471 here at the bank."},
        {"role": "assistant", "content": "I will set that up. Confirm?"},
        {"role": "user", "content": "Yes, please set it up."},
        {"role": "assistant", "tool_calls": [{"function": {
            "name": "schedule_recurring_transfer",
            "arguments": {
                "from_account": "SB-REAL-001",
                "beneficiary_account": beneficiary_account,
                "amount": 15000,
                "day_of_month": 2,
                "duration_months": 12,
            },
        }}]},
    ]


class S1NumericIdsInProse(unittest.TestCase):
    """A bare account number in the customer's own words is disclosed, not guessed."""

    def test_customer_stated_numeric_account_is_valid(self):
        ok, violations = _check_s1(transfer_transcript("5021004471"), LOGIN_FD, live_start=1)
        self.assertTrue(ok, violations)

    def test_fabricated_numeric_account_fails_s1(self):
        ok, violations = _check_s1(transfer_transcript("5099999999"), LOGIN_FD, live_start=1)
        self.assertFalse(ok)
        self.assertTrue(any("beneficiary_account" in item for item in violations), violations)


class IdentifierClassification(unittest.TestCase):
    def test_identifier_names_are_recognized_without_a_catalog_snapshot(self):
        for name in ("account_id", "card_ids", "source_account", "beneficiary_account"):
            with self.subTest(name=name):
                self.assertTrue(_is_id_key(name))
        for name in ("amount", "tenure_months", "reason"):
            with self.subTest(name=name):
                self.assertFalse(_is_id_key(name))
