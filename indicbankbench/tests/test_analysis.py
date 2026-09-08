"""Tests for the invariants that fail *silently*.

Deliberately narrow. The arithmetic in analysis.py (strict / flaky / mean / distribution) is
not tested here: it runs on every invocation and a break is obvious on sight — a strict rate
above the mean, a distribution that doesn't sum. Tests that restate readable arithmetic are
maintenance cost without safety.

What IS tested are three invariants whose failure produces a clean-looking, wrong number:

  1. a case that never scored still counts as a non-pass
  2. the resume fingerprint changes when the inputs change
     (a regression silently reuses results from a different temperature or an edited case)
  3. compare's mismatch warnings actually fire
     (a warning that stops firing is worse than none — you've stopped checking by hand)

Run:  python -m unittest discover -s indicbankbench/tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from indicbankbench.harness import analysis  # noqa: E402


def write_run(root, run_id, case_ids, verdicts, passes=3, **meta_overrides):
    """Build a synthetic results tree.

    `verdicts` maps case_id -> list (one entry per pass) of "PASS" / "FAIL" / None.
    **None means no score.json is written at all** — the crash case that motivated test 1.
    """
    base = Path(root) / run_id
    meta = {
        "run_id": run_id,
        "passes": passes,
        "n_cases": len(case_ids),
        "case_ids": list(case_ids),
        "case_meta": {c: {"axis": "1_happy_path", "domain": "testdomain", "tool": "t"} for c in case_ids},
        "case_bank_hash": "hash0",
        "temperature": 0.0,
        "candidate_model": "test-model",
        "candidate_base_url": "http://localhost/v1",
        "judge_model": "judge",
        "started_at": "2026-01-01 00:00:00",
    }
    meta.update(meta_overrides)
    base.mkdir(parents=True, exist_ok=True)
    (base / "run.json").write_text(json.dumps(meta))

    for case_id in case_ids:
        for i, verdict in enumerate(verdicts.get(case_id, [])):
            if verdict is None:
                continue  # crashed: no score.json, exactly like max_tool_iters
            d = base / "cases" / f"pass{i + 1}" / case_id
            d.mkdir(parents=True, exist_ok=True)
            (d / "score.json").write_text(json.dumps({
                "case_id": case_id,
                "verdict": verdict,
                "axis": "1_happy_path",
                "domain": "testdomain",
                "tool": "t",
                "fail_reason": None if verdict == "PASS" else "A1",
            }))
    return base


class NeverScoredCounts(unittest.TestCase):
    """Invariant 1 — a model must never be rewarded for failing loudly."""

    def test_case_that_never_scored_stays_in_the_denominator(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_run(tmp, "r", ["c1", "c2", "c3"], {
                "c1": ["PASS", "PASS", "PASS"],
                "c2": ["PASS", "PASS", "PASS"],
                "c3": [None, None, None],      # crashed every pass — writes nothing
            })
            s = analysis.summarize(analysis.load_run("r", results_root=tmp))

        # The regression this guards: c3 vanishing entirely -> 2/2 = 100%.
        self.assertEqual(s["n_cases"], 3, "crashed case dropped from the denominator")
        self.assertEqual(s["strict_pass"], 2)
        self.assertAlmostEqual(s["strict_rate"], 2 / 3)
        self.assertEqual(s["never"], 1)

    def test_missing_score_in_one_pass_is_a_fail_for_that_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_run(tmp, "r", ["c1"], {"c1": ["PASS", None, "PASS"]})
            s = analysis.summarize(analysis.load_run("r", results_root=tmp))

        # Must not be treated as 2/2 by averaging over what happens to exist.
        self.assertFalse(s["cases"]["c1"]["strict"])
        self.assertEqual(s["cases"]["c1"]["k"], 2)
        self.assertEqual(s["cases"]["c1"]["n"], 3)
        self.assertEqual(s["flaky"], 1)

    def test_never_scored_case_keeps_its_domain(self):
        """It must still group in the report rather than landing in an 'unknown' bucket."""
        with tempfile.TemporaryDirectory() as tmp:
            write_run(tmp, "r", ["c1"], {"c1": [None, None, None]})
            s = analysis.summarize(analysis.load_run("r", results_root=tmp))
        self.assertEqual(s["cases"]["c1"]["domain"], "testdomain")


class ResumeFingerprint(unittest.TestCase):
    """Invariant 2 — auto-resume is only safe while this discriminates."""

    def setUp(self):
        from indicbankbench.harness import cli
        self.cli = cli
        self.cfg = {"candidate_model": "m1", "temperature": 0.0, "judge_model": "j1"}

    def _case_file(self, tmp, body="{}"):
        p = Path(tmp) / "case.json"
        p.write_text(body)
        return p

    def test_same_inputs_give_the_same_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._case_file(tmp)
            self.assertEqual(
                self.cli._case_fingerprint(p, self.cfg),
                self.cli._case_fingerprint(p, self.cfg),
            )

    def test_fingerprint_changes_when_temperature_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._case_file(tmp)
            hot = {**self.cfg, "temperature": 0.7}
            self.assertNotEqual(self.cli._case_fingerprint(p, self.cfg),
                                self.cli._case_fingerprint(p, hot))

    def test_fingerprint_changes_when_the_case_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._case_file(tmp, '{"a": 1}')
            before = self.cli._case_fingerprint(p, self.cfg)
            p.write_text('{"a": 2}')
            self.assertNotEqual(before, self.cli._case_fingerprint(p, self.cfg))

    def test_fingerprint_changes_when_the_model_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._case_file(tmp)
            other = {**self.cfg, "candidate_model": "m2"}
            self.assertNotEqual(self.cli._case_fingerprint(p, self.cfg),
                                self.cli._case_fingerprint(p, other))


class CompareWarnings(unittest.TestCase):
    """Invariant 3 — the guard against clean-looking, meaningless tables."""

    def _two_runs(self, tmp, **b_overrides):
        cases = ["c1", "c2"]
        verdicts = {c: ["PASS"] * 3 for c in cases}
        write_run(tmp, "a", cases, verdicts)
        write_run(tmp, "b", cases, verdicts, **b_overrides)
        return analysis.compare(["a", "b"], results_root=tmp)

    def test_matched_configs_produce_no_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, warnings = self._two_runs(tmp)
        self.assertEqual(warnings, [], "false alarm — warnings must stay meaningful")

    def test_temperature_mismatch_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, warnings = self._two_runs(tmp, temperature=0.7)
        self.assertTrue(any("temperature" in w for w in warnings), warnings)

    def test_passes_mismatch_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, warnings = self._two_runs(tmp, passes=2)
        self.assertTrue(any("passes" in w for w in warnings), warnings)

    def test_case_bank_mismatch_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, warnings = self._two_runs(tmp, case_bank_hash="different")
        self.assertTrue(any("case_bank_hash" in w for w in warnings), warnings)

    def test_different_case_sets_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_run(tmp, "a", ["c1", "c2"], {c: ["PASS"] * 3 for c in ["c1", "c2"]})
            write_run(tmp, "b", ["c1", "c3"], {c: ["PASS"] * 3 for c in ["c1", "c3"]})
            _, warnings = analysis.compare(["a", "b"], results_root=tmp)
        self.assertTrue(any("case set" in w for w in warnings), warnings)

    def test_warning_survives_rendering(self):
        """A warning computed but not rendered is a warning that doesn't exist."""
        with tempfile.TemporaryDirectory() as tmp:
            summaries, warnings = self._two_runs(tmp, temperature=0.7)
            md = analysis.render_comparison(summaries, warnings)
        self.assertIn("WARNING", md.upper())
        self.assertIn("temperature", md)


if __name__ == "__main__":
    unittest.main()
