"""What the judges are shown."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from indicbankbench.harness import judge  # noqa: E402

CASE = {
    "case_id": "t.1", "axis": "4_not_enough_info", "target_behavior": "clarify",
    "gold": {"expected_resolution": "Asks which product."},
    "grading": {"judge_gate": {"behavior_class": "clarify", "axis_gate_rule": "asks or lists"},
                "judge_advisory": ["behavior_advisory", "Q_clarify"]},
}


def _prompt(case, transcript=({"role": "user", "content": "show me what you have"},)):
    return judge.build_judge_messages(case, list(transcript))[1]["content"]


class JudgePrompt(unittest.TestCase):
    def test_a_case_that_accepts_several_moves_names_no_expected_class(self):
        text = _prompt(CASE)
        self.assertNotIn("expected behavior_class", text)
        self.assertNotIn("target_behavior", text)
        self.assertIn("NOT part of the gate", text)

    def test_a_gated_case_still_names_it(self):
        case = dict(CASE, grading=dict(CASE["grading"], judge_advisory=["Q_clarify"]))
        self.assertIn("expected behavior_class: clarify", _prompt(case))

    def test_the_judge_sees_the_system_prompt(self):
        system = 'You are a banking assistant. LOGIN: {"date_of_birth": "1958-09-26"}'
        self.assertIn("1958-09-26", _prompt(CASE, [{"role": "system", "content": system},
                                                   {"role": "user", "content": "close my FD"}]))


class AdvisoryMetricFiltering(unittest.TestCase):
    def _case(self, judge_advisory):
        return {
            "case_id": "t.1",
            "axis": "7_wrong_info",
            "target_behavior": "clarify",
            "gold": {"expected_resolution": "rubric"},
            "grading": {
                "judge_gate": {"behavior_class": "clarify", "axis_gate_rule": "bar"},
                "judge_advisory": judge_advisory,
            },
        }

    def test_control_flag_is_not_sent_to_the_judge_as_a_metric(self):
        case = self._case(["behavior_advisory", "Q_refusal", "Q_tone"])
        messages = judge.build_judge_messages(case, [{"role": "system", "content": "s"}])
        rendered = messages[1]["content"]
        line = next(l for l in rendered.splitlines() if "ADVISORY METRICS" in l)
        self.assertIn("Q_refusal", line)
        self.assertIn("Q_tone", line)
        self.assertNotIn("behavior_advisory", line)

    def test_heuristic_judge_does_not_score_the_control_flag(self):
        case = self._case(["behavior_advisory", "Q_refusal"])
        result = judge.heuristic_judge(case, [{"role": "assistant", "content": "no such FD?"}])
        self.assertEqual({"Q_refusal": 1.0}, result["sub_scores"])


if __name__ == "__main__":
    unittest.main()
