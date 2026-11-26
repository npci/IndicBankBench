import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from indicbankbench.harness import cli, runner  # noqa: E402


class ToolLimitFailure(unittest.TestCase):
    case = {
        "case_id": "case.1",
        "axis": "axis",
        "domain": "domain",
        "tool": "tool",
        "target_behavior": "answer",
    }
    transcript = [{"role": "assistant", "content": None, "tool_calls": []}]

    def _run_limited_case(self, graded):
        error = runner.MaxToolIterationsError("tool limit", self.transcript)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "case.1"
            with (
                mock.patch.object(cli, "_make_candidate_client", return_value=(object(), "candidate")),
                mock.patch.object(cli.runner, "run_case", side_effect=error),
                mock.patch.object(cli.grader, "grade", return_value=graded),
                mock.patch.object(cli, "_get_judge_result") as judge_call,
            ):
                result = cli._run_one(
                    self.case, "live", "live", lambda: object(), out_dir, case_fingerprint="fingerprint"
                )
            saved = json.loads((out_dir / "score.json").read_text())
        return result, saved, judge_call

    def test_limit_without_deterministic_failure_is_scored_as_failed(self):
        graded = {"case_id": "case.1", "verdict": "INCOMPLETE", "fail_reason": "awaiting judge"}
        result, saved, judge_call = self._run_limited_case(graded)

        self.assertEqual("FAIL", result["verdict"])
        self.assertEqual("NO_FINAL_ANSWER", result["fail_reason"])
        self.assertEqual("max_tool_iters", result["termination_reason"])
        self.assertEqual("fingerprint", saved["_fingerprint"])
        judge_call.assert_not_called()

    def test_limit_preserves_a_deterministic_failure(self):
        graded = {"case_id": "case.1", "verdict": "FAIL", "fail_reason": "S3"}
        result, saved, _ = self._run_limited_case(graded)

        self.assertEqual("S3", result["fail_reason"])
        self.assertEqual("max_tool_iters", saved["termination_reason"])


if __name__ == "__main__":
    unittest.main()
