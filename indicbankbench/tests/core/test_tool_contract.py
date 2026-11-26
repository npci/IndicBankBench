"""Contracts shipped with the harness; no downloaded cases required."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from indicbankbench.harness import judge, tools

class Disclosure(unittest.TestCase):

    def test_every_declared_prerequisite_is_disclosed(self):
        raw, _ = tools.load_tool_definitions()
        model = tools.load_model_schemas()
        undisclosed = []
        for tool in raw["tools"]:
            description = model[tool["name"]]["function"]["description"]
            for pre in tool.get("call_before") or []:
                if pre.get("tool") and pre["tool"] not in description:
                    undisclosed.append(f"{tool['name']} requires {pre['tool']}, unmentioned in its description")
        self.assertEqual([], undisclosed, "\n  " + "\n  ".join(undisclosed))


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
