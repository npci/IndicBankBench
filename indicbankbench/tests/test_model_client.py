"""Tests for isolation of provider-specific candidate request options."""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from indicbankbench.harness.model_client import ModelClient  # noqa: E402


class CandidateExtraBodyIsolation(unittest.TestCase):
    messages = [{"role": "user", "content": "Test request"}]

    def _request(self, profile_name, extra_raw):
        client = ModelClient()
        transport = mock.Mock()
        message = SimpleNamespace(content="Test response", tool_calls=None)
        transport.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=message)]
        )
        profile = {
            "model": "test-model",
            "temperature": 0.7,
            "max_tokens": 1024,
            "top_p": 1.0,
            "seed": None,
        }
        with (
            mock.patch.dict(os.environ, {"CANDIDATE_EXTRA_BODY": extra_raw}),
            mock.patch.object(client, "_client_for", return_value=(transport, profile)),
        ):
            text = client.raw_text(profile_name, self.messages)
        self.assertEqual("Test response", text)
        transport.chat.completions.create.assert_called_once()
        return transport.chat.completions.create.call_args.kwargs

    def test_candidate_profiles_receive_extra_body(self):
        for name in ("candidate_default", "candidate_custom"):
            with self.subTest(profile=name):
                request = self._request(name, '{"enable_thinking": false}')
                self.assertEqual({"enable_thinking": False}, request["extra_body"])

    def test_judge_and_report_profiles_do_not_receive_candidate_options(self):
        for name in ("judge_default", "judge_confirmation", "report_default"):
            with self.subTest(profile=name):
                request = self._request(name, '{"enable_thinking": false}')
                self.assertNotIn("extra_body", request)

    def test_invalid_candidate_json_is_ignored(self):
        request = self._request("candidate_default", "not valid JSON")
        self.assertNotIn("extra_body", request)

    def test_empty_candidate_options_are_omitted(self):
        request = self._request("candidate_default", "")
        self.assertNotIn("extra_body", request)


if __name__ == "__main__":
    unittest.main()
