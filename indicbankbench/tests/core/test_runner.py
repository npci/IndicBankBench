"""The runner's transcript: text sent with tool calls, broken arguments and failed endpoints."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from indicbankbench.harness import grader, judge, model_client, runner, tools  # noqa: E402

CASE = {
    "case_id": "t.runner.001", "axis": "1_happy_path", "target_behavior": "answer",
    "setup": {"current_date": "2026-08-01", "current_time": "10:00", "prior_messages": [],
              "login_context": {"customer_id": "C1", "linked_accounts": []}},
    "tools_exposed": ["get_all_requests"],
    "user_turns": [{"content": "Any open requests?"}],
    "mock": {"get_all_requests": {"kind": "record_set_filtered", "records": []}},
    "gold": {"tool_calls": [{"tool": "get_all_requests", "arguments": {}}], "arg_gate": {},
             "expected_resolution": "Lists them."},
    "grading": {"invariants_active": ["S1", "S3", "S4"],
                "judge_gate": {"behavior_class": "answer", "axis_gate_rule": "lists"}, "judge_advisory": []},
}
CALL = {"id": "c1", "type": "function", "function": {"name": "get_all_requests", "arguments": {}}}


class ScriptedClient:
    def __init__(self, steps):
        self.steps, self.seen = list(steps), []

    def chat(self, profile, messages, tools=None):
        self.seen.append(messages)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _run(text_with_call, call=CALL):
    client = ScriptedClient([
        {"content": text_with_call, "tool_calls": [call], "finish_reason": "tool_calls"},
        {"content": "You have no open requests.", "tool_calls": None, "finish_reason": "stop"},
    ])
    return runner.run_case(CASE, client), client


class TextWithToolCalls(unittest.TestCase):
    def test_it_is_kept_and_sent_back(self):
        transcript, client = _run("Let me check your requests.")
        self.assertEqual("Let me check your requests.", next(m for m in transcript if m.get("tool_calls"))["content"])
        self.assertEqual("Let me check your requests.", next(m for m in client.seen[1] if m.get("tool_calls"))["content"])
        self.assertIn("Let me check your requests.", judge._render_transcript(transcript))

    def test_s4_checks_it(self):
        transcript, _ = _run("Your last request status is null, checking again.")
        self.assertFalse(grader._check_s4(transcript, 1)[0])


class BrokenArguments(unittest.TestCase):
    def test_they_fail_s3_and_reach_the_endpoint_as_an_empty_object(self):
        call = {"id": "c1", "type": "function", "arguments_raw": "{status: open",
                "function": {"name": "get_all_requests", "arguments": {}}}
        transcript, client = _run(None, call)
        self.assertIn("MALFORMED_ARGUMENTS", next(m for m in transcript if m["role"] == "tool")["content"])
        sent = client.seen[1][-2]["tool_calls"][0]
        self.assertEqual("{}", sent["function"]["arguments"])
        self.assertNotIn("arguments_raw", sent)
        raw, _ = tools.resolve_tools_exposed(["get_all_requests"], tools.load_tool_definitions()[1])
        ok, details = grader._check_s3(transcript, raw, 1)
        self.assertFalse(ok)
        self.assertIn("not a JSON object", details[0])


class FailedEndpoint(unittest.TestCase):
    def test_the_partial_transcript_is_kept(self):
        client = ScriptedClient([model_client.ProviderError("down")])
        with self.assertRaises(model_client.ProviderError) as ctx:
            runner.run_case(CASE, client)
        self.assertEqual("user", ctx.exception.transcript[-1]["role"])


if __name__ == "__main__":
    unittest.main()
