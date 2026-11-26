"""What a run keeps after a failure."""
import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from indicbankbench.harness import analysis, cli  # noqa: E402
from indicbankbench.tests.core.test_analysis import write_run  # noqa: E402
from indicbankbench.tests.core.test_verdict_gates import CASE  # noqa: E402

CASE_ID = "t.integrity.001"
PLAIN_CASE = dict(CASE, case_id=CASE_ID, tools_exposed=[], user_turns=[{"content": "Hello."}],
                  gold={"tool_calls": [], "arg_gate": {}, "expected_resolution": "Greets."},
                  grading=dict(CASE["grading"], invariants_active=["S1", "S3", "S4"]))
CFG = {"candidate_model": "m", "temperature": 0.0, "judge_model": "j"}
GOOD_JUDGE = json.dumps({"case_id": CASE_ID, "behavior_class": "answer", "axis_gate": "PASS",
                         "gate_reason": "r", "sub_scores": {}, "rationale": "n"})


class Client:
    def __init__(self, answers=("Hello! How can I help?",), judge_fails=False):
        self.answers, self.judge_fails, self.asked = list(answers), judge_fails, 0

    def chat(self, profile, messages, tools=None):
        self.asked += 1
        if not self.answers:
            raise RuntimeError("endpoint down")
        return {"role": "assistant", "content": self.answers.pop(0), "tool_calls": None, "finish_reason": "stop"}

    def raw_text(self, profile, messages):
        if self.judge_fails:
            raise RuntimeError("judge down")
        return GOOD_JUDGE


class KeptOnDisk(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.case_path = self.root / "case.json"
        self.case_path.write_text(json.dumps(PLAIN_CASE))
        self.pass_dir = self.root / "pass1"
        self.pass_dir.mkdir()
        self.score = self.pass_dir / CASE_ID / "score.json"

    def run_once(self, client, cfg=CFG, fresh=False):
        return cli._run_case_for_pass(self.case_path, lambda: client, self.pass_dir, dict(cfg), fresh, None)

    def test_a_judge_that_fails_does_not_lose_the_answer(self):
        _, how = self.run_once(Client(judge_fails=True))
        self.assertEqual("error", how)
        saved = json.loads((self.pass_dir / CASE_ID / "transcript.json").read_text())
        self.assertIn("How can I help", saved[-1]["content"])
        self.assertFalse(self.score.exists())

    def test_the_kept_answer_is_judged_without_asking_the_candidate_again(self):
        self.run_once(Client(judge_fails=True))
        again = Client(answers=())
        result, how = self.run_once(again)
        self.assertEqual(("ran", "PASS", 0), (how, result["verdict"], again.asked))
        self.assertFalse((self.pass_dir / CASE_ID / cli.PENDING_JUDGE).exists())

    def test_a_failed_run_under_other_settings_leaves_no_answer_to_judge(self):
        self.run_once(Client(judge_fails=True))
        self.run_once(Client(answers=()), dict(CFG, candidate_model="another"))
        again = Client()
        result, _ = self.run_once(again)
        self.assertEqual(("PASS", 1), (result["verdict"], again.asked))

    def test_a_fresh_run_ignores_the_kept_answer(self):
        self.run_once(Client(judge_fails=True))
        again = Client()
        self.run_once(again, fresh=True)
        self.assertEqual(1, again.asked)

    def test_a_failed_re_run_leaves_no_old_score_behind(self):
        self.run_once(Client())
        self.assertTrue(self.score.exists())
        _, how = self.run_once(Client(answers=()), dict(CFG, candidate_model="another"))
        self.assertEqual("error", how)
        self.assertFalse(self.score.exists())


class ReportSaysWhatIsMissing(unittest.TestCase):
    def summary(self, verdicts):
        with tempfile.TemporaryDirectory() as tmp:
            write_run(tmp, "r", ["c1", "c2"], verdicts)
            return analysis.summarize(analysis.load_run("r", results_root=tmp))

    def test_unscored_trials_are_counted_and_headlined(self):
        summary = self.summary({"c1": ["PASS", "PASS", "PASS"], "c2": ["PASS", None, None]})
        self.assertEqual(2, summary["unscored"])
        self.assertIn("**Incomplete run:** 2 of 6 trials have no score", analysis.render_run_report(summary))

    def test_compare_warns_about_an_incomplete_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            for run_id, last in (("a", "PASS"), ("b", None)):
                write_run(tmp, run_id, ["c1"], {"c1": ["PASS", "PASS", last]})
            _, warnings = analysis.compare(["a", "b"], results_root=tmp)
        self.assertEqual(1, len([w for w in warnings if w.startswith("b: 1 of 3 trials")]))
        self.assertFalse([w for w in warnings if w.startswith("a:")])


class JudgeEndpoint(unittest.TestCase):
    def run_with(self, judge_url):
        cfg = {"candidate_base_url": "http://localhost/v1", "judge_base_url": judge_url}
        args = argparse.Namespace(cases=[str(self.case)], prompt=str(self.case) + ".missing")
        err = io.StringIO()
        with mock.patch.object(cli, "_run_config", return_value=cfg), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as stop:
                cli.cmd_run(args)
        return stop.exception.code, err.getvalue()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.case = Path(tmp.name) / "case.json"
        self.case.write_text(json.dumps(PLAIN_CASE))

    def test_a_missing_judge_url_stops_the_run(self):
        code, text = self.run_with(None)
        self.assertEqual(2, code)
        self.assertIn("JUDGE_BASE_URL is not set", text)

    def test_configured_endpoints_complete_an_offline_run(self):
        cfg = dict(CFG, candidate_base_url="http://localhost/v1", judge_base_url="http://localhost/v1")
        args = argparse.Namespace(cases=[str(self.case)], prompt=None, candidate_label=None,
                                  run_id="test", passes=1, concurrency=1, fresh=False)
        client = Client()
        results = self.case.parent / "results"
        with mock.patch.object(cli, "_run_config", return_value=cfg), \
                mock.patch.object(cli, "_lazy_client", return_value=lambda: client), \
                mock.patch.object(cli, "RESULTS_ROOT", results), \
                mock.patch.object(cli.analysis, "RESULTS_ROOT", results), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            summary = cli.cmd_run(args)
        self.assertEqual(1, summary["strict_pass"])
        self.assertTrue((results / "test" / "REPORT.md").exists())


if __name__ == "__main__":
    unittest.main()
