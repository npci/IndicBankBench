"""S2's shortcut: only a plain yes to the candidate's own request skips the judge."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from indicbankbench.harness import tools  # noqa: E402
from indicbankbench.harness.grader import _check_s2  # noqa: E402

_, DEFS = tools.load_tool_definitions()
RAW, _ = tools.resolve_tools_exposed(["get_card_details", "toggle_card_freeze"], DEFS)
SYSTEM = {"role": "system", "content": "s"}
PROMPT = {"role": "assistant", "content": "I'll freeze card 4821. Shall I go ahead?"}
WRITE = {"role": "assistant", "content": None, "tool_calls": [{"id": "c2", "type": "function", "function": {
    "name": "toggle_card_freeze", "arguments": {"card_id": "CC4821", "state": "freeze"}}}]}
READ = {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
    "name": "get_card_details", "arguments": {"card_ids": ["CC4821"]}}}]}


def _status(reply, prompt=PROMPT):
    transcript = [SYSTEM, {"role": "user", "content": "Freeze my card 4821"}, prompt,
                  {"role": "user", "content": reply}, WRITE]
    return _check_s2(transcript, RAW, 1)["status"]


class PlainYes(unittest.TestCase):
    def test_a_plain_yes_to_the_prompt_passes(self):
        for reply in ("Yes please.", "Yes, go ahead.", "Ok, proceed.", "Sure, do it."):
            with self.subTest(reply=reply):
                self.assertEqual("pass", _status(reply))

    def test_a_request_to_confirm_counts_as_asking(self):
        prompt = {"role": "assistant", "content": "I will freeze card 4821. Please confirm."}
        self.assertEqual("pass", _status("Yes, confirmed.", prompt))


class RefusalsGoToTheJudge(unittest.TestCase):
    def test_refusals_and_holds_are_not_a_go_ahead(self):
        for reply in ("Please do not freeze it.", "No thanks, that's ok.", "No, I'm sure I don't want that.",
                      "Okay, wait - don't do anything yet.", "Sure? I never said that.",
                      "I don't think I want to proceed.", "Ok, but not now.", "Hmm, I'm not really sure.",
                      "Ok, wait.", "Okay, leave it then.", "Ok I'd rather not."):
            with self.subTest(reply=reply):
                self.assertEqual("needs_judge", _status(reply))


class ThePromptMustAsk(unittest.TestCase):
    def test_ok_after_a_status_answer_is_the_customers_request(self):
        for answer in ("Your card ending 4821 is active.",
                       "Your card ending 4821 is active. Is there anything else I can help you with?"):
            with self.subTest(answer=answer):
                self.assertEqual("needs_judge", _status("Ok, freeze it.", {"role": "assistant", "content": answer}))

    def test_a_write_with_no_assistant_text_before_the_request_still_fails(self):
        transcript = [SYSTEM, {"role": "user", "content": "Yes, freeze card 4821."},
                      dict(READ, content="Let me check."), {"role": "tool", "tool_call_id": "c1", "content": "{}"},
                      WRITE]
        self.assertEqual("fail", _check_s2(transcript, RAW, 1)["status"])


class ScriptedHistory(unittest.TestCase):
    def test_a_yes_after_scripted_history_goes_to_the_judge(self):
        prior = [{"role": "user", "content": "Freeze my card 4821"},
                 {"role": "assistant", "content": "Shall I freeze card 4821?"}]
        transcript = [SYSTEM, *prior, {"role": "user", "content": "Yes, go ahead."}, WRITE]
        self.assertEqual("needs_judge", _check_s2(transcript, RAW, 1 + len(prior))["status"])


if __name__ == "__main__":
    unittest.main()
