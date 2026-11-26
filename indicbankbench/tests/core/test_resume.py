"""Run resume fingerprints."""
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

class ResumeFingerprint(unittest.TestCase):

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
