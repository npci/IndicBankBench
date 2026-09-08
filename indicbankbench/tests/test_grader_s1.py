"""Regression tests for S1 ID provenance."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from indicbankbench.harness import mock_executor, tools  # noqa: E402
from indicbankbench.harness.grader import _check_s1  # noqa: E402


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
