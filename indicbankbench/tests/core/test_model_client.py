"""Request options, provider errors and tool arguments."""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from indicbankbench.harness import model_client  # noqa: E402
from indicbankbench.harness.model_client import ModelClient  # noqa: E402


class CandidateExtraBodyIsolation(unittest.TestCase):
    messages = [{"role": "user", "content": "Test request"}]

    def _request(self, profile_name, extra_raw="", judge_raw=""):
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
        env = {"CANDIDATE_EXTRA_BODY": extra_raw, "JUDGE_EXTRA_BODY": judge_raw}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(client, "_client_for", return_value=(transport, profile)):
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

    def test_non_object_candidate_json_is_ignored(self):
        for extra_raw in ('["unexpected"]', '"unexpected"', '42', 'true', 'null'):
            with self.subTest(extra_raw=extra_raw):
                request = self._request("candidate_default", extra_raw)
                self.assertNotIn("extra_body", request)

    def test_empty_candidate_options_are_omitted(self):
        request = self._request("candidate_default", "")
        self.assertNotIn("extra_body", request)


    def test_judge_and_report_get_judge_options(self):
        for profile in ("judge_default", "report_default"):
            with self.subTest(profile=profile):
                sent = self._request(profile, '{"reasoning": {"effort": "high"}}',
                                     '{"enable_thinking": false}')
                self.assertEqual({"enable_thinking": False}, sent["extra_body"])

    def test_candidate_never_gets_judge_options(self):
        sent = self._request("candidate_default", judge_raw='{"enable_thinking": false}')
        self.assertNotIn("extra_body", sent)


def _reply(content, finish_reason="stop", tool_calls=None, error=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls, reasoning=None, reasoning_details=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish_reason)], error=error)


def _chat(responses):
    transport = mock.Mock()
    transport.chat.completions.create.side_effect = list(responses)
    client = ModelClient()
    profile = {"model": "m", "temperature": 0.0, "max_tokens": 10, "top_p": 1.0, "seed": None,
               "max_retries": 2}
    with mock.patch.object(client, "_client_for", return_value=(transport, profile)), \
            mock.patch.object(model_client.time, "sleep"), \
            mock.patch.dict(os.environ, {"CANDIDATE_EXTRA_BODY": "", "JUDGE_EXTRA_BODY": ""}):
        result = client.chat("candidate_default", [{"role": "user", "content": "hi"}])
    return result, transport.chat.completions.create.call_count


GATEWAY_TEXT = "HTTP 503: Service temporarily unavailable."


class ProviderErrorText(unittest.TestCase):
    """A gateway error delivered as the reply's text is an outage, not the model's answer."""

    def test_error_text_is_retried(self):
        result, calls = _chat([_reply(GATEWAY_TEXT), _reply("Your card is frozen.")])
        self.assertEqual(2, calls)
        self.assertEqual("Your card is frozen.", result["content"])

    def test_a_reply_that_mentions_an_error_is_the_models_answer(self):
        for text in ("Sorry, the bank's system returned an error (HTTP 502). Please try again later.",
                     "I'm sorry, the service is unavailable right now. Please try again later.",
                     "HTTP 502 " + "x" * 400):
            with self.subTest(text=text[:40]):
                result, calls = _chat([_reply(text)])
                self.assertEqual((1, text), (calls, result["content"]))


class ProviderErrors(unittest.TestCase):
    def test_an_error_reply_is_retried(self):
        result, calls = _chat([_reply("", finish_reason="error"), _reply("fine")])
        self.assertEqual((2, "fine"), (calls, result["content"]))

    def test_a_finished_reply_with_an_error_object_is_kept(self):
        result, calls = _chat([_reply("Here it is.", error={"code": 502}), _reply("other")])
        self.assertEqual((1, "Here it is."), (calls, result["content"]))

    def test_an_empty_reply_is_kept(self):
        result, calls = _chat([_reply("", finish_reason="length"), _reply("other")])
        self.assertEqual((1, ""), (calls, result["content"]))

    def test_persistent_error_text_raises(self):
        with self.assertRaises(model_client.ProviderError):
            _chat([_reply(GATEWAY_TEXT)] * 3)


class BrokenToolArguments(unittest.TestCase):
    def test_null_arguments_mean_none(self):
        call = SimpleNamespace(id="c1", function=SimpleNamespace(name="get_all_requests", arguments="null"))
        result, _ = _chat([_reply(None, finish_reason="tool_calls", tool_calls=[call])])
        self.assertEqual({}, result["tool_calls"][0]["function"]["arguments"])
        self.assertNotIn("arguments_raw", result["tool_calls"][0])

    def test_the_raw_text_is_kept(self):
        call = SimpleNamespace(id="c1", function=SimpleNamespace(name="get_all_requests", arguments='{"status": "open"'))
        result, _ = _chat([_reply(None, finish_reason="tool_calls", tool_calls=[call])])
        self.assertEqual('{"status": "open"', result["tool_calls"][0]["arguments_raw"])
        self.assertEqual({}, result["tool_calls"][0]["function"]["arguments"])


if __name__ == "__main__":
    unittest.main()
