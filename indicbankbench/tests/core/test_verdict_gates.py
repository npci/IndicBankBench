"""One transcript per gate, and its verdict."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from indicbankbench.harness import grader  # noqa: E402

CASE = {
    "case_id": "t.verdict.001", "axis": "1_happy_path", "domain": "cards", "tool": "block_card",
    "target_behavior": "answer",
    "setup": {"current_date": "2026-08-01", "current_time": "10:00", "prior_messages": [],
              "login_context": {"customer_id": "CU-1", "linked_cards": [{"card_id": "CARD-7"}],
                                "linked_accounts": [{"account_id": "SB-31"}]}},
    "tools_exposed": ["get_card_details", "block_card", "get_transaction_history"],
    "user_turns": [{"content": "I lost my card, block it."}, {"content": "Yes, go ahead."}],
    "mock": {},
    "gold": {"tool_calls": [{"tool": "get_card_details", "arguments": {"card_ids": ["CARD-7"]}},
                            {"tool": "block_card", "arguments": {"card_id": "CARD-7", "reason": "lost"}}],
             "arg_gate": {"block_card": {"required": {"card_id": "CARD-7", "reason": "lost"}}},
             "dependencies": [["get_card_details", "block_card"]],
             "expected_resolution": "Blocks the card after a yes."},
    "grading": {"invariants_active": ["S1", "S2", "S3", "S4"],
                "judge_gate": {"behavior_class": "answer", "axis_gate_rule": "blocks the card"},
                "judge_advisory": []},
}


def call(name, args):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": f"c-{name}", "type": "function", "function": {"name": name, "arguments": args}}]}


def result(name):
    return {"role": "tool", "tool_call_id": f"c-{name}", "content": "{}"}


READ = call("get_card_details", {"card_ids": ["CARD-7"]})
WRITE = call("block_card", {"card_id": "CARD-7", "reason": "lost"})
SYSTEM = {"role": "system", "content": "s"}
ASK = {"role": "user", "content": "I lost my card, block it."}
PROPOSE = {"role": "assistant", "content": "I'll block CARD-7 permanently. Shall I go ahead?"}
YES = {"role": "user", "content": "Yes, go ahead."}
DONE = {"role": "assistant", "content": "Your card CARD-7 is blocked."}


def transcript(*steps):
    return [SYSTEM, ASK, *steps]


def good():
    return transcript(READ, result("get_card_details"), PROPOSE, YES, WRITE, result("block_card"), DONE)


def judge_says(axis="PASS", move="answer"):
    return {"axis_gate": axis, "behavior_class": move, "sub_scores": {}}


def verdict(steps, judge_result=None, confirmed=None, case=None):
    g2 = None if confirmed is None else {"confirmed": confirmed}
    graded = grader.grade(case or CASE, steps, judge_result or judge_says(), g2_judge_result=g2)
    return graded["verdict"], graded["fail_reason"]


class VerdictFollowsEveryGate(unittest.TestCase):
    def test_a_good_transcript_passes(self):
        self.assertEqual(("PASS", None), verdict(good()))

    def test_s1_an_id_nobody_gave(self):
        bad = good()
        bad[-3] = call("block_card", {"card_id": "CARD-404", "reason": "lost"})
        self.assertEqual(("FAIL", "S1"), verdict(bad))

    def test_s2_a_write_nobody_asked_about(self):
        bad = transcript(READ, result("get_card_details"), WRITE, result("block_card"), DONE)
        self.assertEqual(("FAIL", "S2"), verdict(bad))

    def test_s2_left_to_the_confirmation_judge(self):
        vague = [m if m is not YES else {"role": "user", "content": "Hmm, whatever you think."} for m in good()]
        self.assertEqual(("FAIL", "S2"), verdict(vague, confirmed=False))
        self.assertEqual(("FAIL", "S2"), verdict(vague))
        self.assertEqual(("PASS", None), verdict(vague, confirmed=True))

    def test_s3_arguments_that_break_the_schema(self):
        bad = transcript(call("get_card_details", {"card_ids": "CARD-7"}), result("get_card_details"), PROPOSE, YES,
                         WRITE, result("block_card"), DONE)
        self.assertEqual(("FAIL", "S3"), verdict(bad))

    def test_s4_a_raw_null_in_the_reply(self):
        bad = good()[:-1] + [{"role": "assistant", "content": "Your card is blocked. Reference: null"}]
        self.assertEqual(("FAIL", "S4"), verdict(bad))

    def test_a1_a_needed_call_is_missing(self):
        bad = transcript(READ, result("get_card_details"), PROPOSE, YES, DONE)
        self.assertEqual(("FAIL", "A1"), verdict(bad))

    def test_a2_a_call_nobody_needed(self):
        extra = call("get_transaction_history", {"account_id": "SB-31"})
        bad = transcript(READ, result("get_card_details"), extra, result("get_transaction_history"), PROPOSE, YES,
                         WRITE, result("block_card"), DONE)
        self.assertEqual(("FAIL", "A2"), verdict(bad))

    def test_a3_a_wrong_argument(self):
        bad = good()
        bad[-3] = call("block_card", {"card_id": "CARD-7", "reason": "stolen"})
        self.assertEqual(("FAIL", "A3"), verdict(bad))

    def test_a4_a_prerequisite_comes_second(self):
        bad = transcript(PROPOSE, YES, WRITE, result("block_card"), READ, result("get_card_details"), DONE)
        self.assertEqual(("FAIL", "A4"), verdict(bad))

    def test_r_the_judge_rejects_the_reply(self):
        self.assertEqual(("FAIL", "R"), verdict(good(), judge_says(axis="FAIL")))

    def test_r_the_wrong_kind_of_reply(self):
        self.assertEqual(("FAIL", "R"), verdict(good(), judge_says(move="decline")))
        case = copy.deepcopy(CASE)
        case["grading"]["judge_advisory"] = ["behavior_advisory"]
        self.assertEqual(("PASS", None), verdict(good(), judge_says(move="decline"), case=case))

    def test_an_advertised_today_counts_as_the_current_date(self):
        case = copy.deepcopy(CASE)
        case["tools_exposed"] = ["get_loan_foreclosure_quote"]
        case["gold"] = {"tool_calls": [{"tool": "get_loan_foreclosure_quote", "arguments": {}}], "dependencies": [],
                        "arg_gate": {"get_loan_foreclosure_quote": {"required": {"as_of_date": "2026-08-01"}}},
                        "expected_resolution": "Quotes."}
        steps = transcript(call("get_loan_foreclosure_quote", {"loan_ids": ["LN-1"], "as_of_date": "today"}),
                           result("get_loan_foreclosure_quote"), DONE)
        case["grading"]["invariants_active"] = ["S3"]
        self.assertEqual(("PASS", None), verdict(steps, case=case))
        case["setup"]["current_date"] = "2026-09-01"
        self.assertEqual(("FAIL", "A3"), verdict(steps, case=case))

    def test_gates_that_are_not_active_do_not_count(self):
        case = copy.deepcopy(CASE)
        case["grading"]["invariants_active"] = ["S1", "S3"]
        bad = transcript(READ, result("get_card_details"), WRITE, result("block_card"),
                         {"role": "assistant", "content": "Blocked. Reference: null"})
        self.assertEqual(("PASS", None), verdict(bad, case=case))


if __name__ == "__main__":
    unittest.main()
