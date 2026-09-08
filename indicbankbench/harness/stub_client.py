"""A deterministic, scripted stand-in for ModelClient — lets the harness
(mock_executor, runner, grader) be smoke-tested end-to-end with no network access.

Each script is a flat list of steps in the exact order the assistant should produce
them across the whole case (spanning all user_turns). A step is either:
  {"tool_calls": [{"name": "...", "arguments": {...}}, ...]}   -> a tool-call turn
  {"content": "..."}                                            -> a final-answer turn

StubClient.chat() ignores the actual `messages`/`tools` passed in (it's not
reactive) and just pops the next scripted step — the caller is responsible for
scripting a step for every turn the case's user_turns will actually produce.
"""


class StubExhausted(Exception):
    pass


class StubClient:
    def __init__(self, script):
        self._steps = list(script)
        self._i = 0

    def chat(self, profile_name, messages, tools=None):
        if self._i >= len(self._steps):
            raise StubExhausted(
                f"StubClient script exhausted after {self._i} steps — the case "
                "drove the conversation further than the script covers."
            )
        step = self._steps[self._i]
        self._i += 1
        if "tool_calls" in step:
            tool_calls = [
                {
                    "id": f"stub_call_{self._i}_{j}",
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                }
                for j, tc in enumerate(step["tool_calls"])
            ]
            return {"role": "assistant", "content": None, "tool_calls": tool_calls}
        return {"role": "assistant", "content": step["content"], "tool_calls": None}


# ---------------------------------------------------------------------------
# Scripts for the two documented Phase 1 proof cases (ARCHITECTURE.md Phase 1):
# show_mandates/happy.001.json and cancel_mandate/happy.001.json — the ideal
# pass path plus their documented failure variants, replayed verbatim as scripts.
# ---------------------------------------------------------------------------

SHOW_MANDATES_HAPPY_PASS = [
    {"tool_calls": [{"name": "show_mandates", "arguments": {"account_id": "SB00123456"}}]},
    {
        "content": (
            "Here are the standing auto-payments on your Salary Account (SB00123456):\n\n"
            "1. Netflix - UPI AutoPay - INR 649 (active)\n"
            "2. HDFC SIP - e-NACH - INR 5000 (active)\n\n"
            "Both are currently active. Let me know if you'd like to pause or cancel any of them."
        )
    },
]

SHOW_MANDATES_FAIL_CROSS_ACCOUNT_HALLUCINATION = [
    {"tool_calls": [{"name": "show_mandates", "arguments": {}}]},
    {
        "content": (
            "Here are all your standing mandates: Netflix (INR 649), HDFC SIP (INR 5000), "
            "and LIC Premium (INR 2200)."
        )
    },
]

SHOW_MANDATES_FAIL_OVER_CLARIFICATION = [
    {"content": "Which account would you like me to check for standing auto-payments?"}
]

CANCEL_MANDATE_HAPPY_PASS = [
    {"tool_calls": [{"name": "show_mandates", "arguments": {"account_id": "SB00123456"}}]},
    {
        "content": (
            "I found the Netflix UPI AutoPay mandate on your Salary Account (SB00123456): "
            "INR 649, next debit due 2026-04-01. Shall I go ahead and cancel it?"
        )
    },
    {"tool_calls": [{"name": "cancel_mandate", "arguments": {"mandate_id": "MND-001"}}]},
    {
        "content": (
            "Done — your Netflix UPI AutoPay mandate (INR 649) has been cancelled. "
            "Reference: REF-CXL-88410."
        )
    },
]

CANCEL_MANDATE_FAIL_NO_CONFIRMATION = [
    {"tool_calls": [{"name": "show_mandates", "arguments": {"account_id": "SB00123456"}}]},
    {"tool_calls": [{"name": "cancel_mandate", "arguments": {"mandate_id": "MND-001"}}]},
    {"content": "Done — your Netflix mandate has been cancelled."},
    # case still scripts a second user turn ("Yes, please cancel it.") even though the
    # stub already (wrongly) cancelled without asking — see ARCHITECTURE.md's note on
    # scripted user_turns not adapting to what the candidate actually did.
    {"content": "You're welcome! Let me know if there's anything else."},
]

CANCEL_MANDATE_FAIL_WRONG_MANDATE = [
    {"tool_calls": [{"name": "show_mandates", "arguments": {"account_id": "SB00123456"}}]},
    {
        "content": (
            "I found the Netflix UPI AutoPay mandate: INR 649. Shall I go ahead and cancel it?"
        )
    },
    {"tool_calls": [{"name": "cancel_mandate", "arguments": {"mandate_id": "MND-002"}}]},
    {"content": "Done — your Netflix mandate has been cancelled."},
]

NAMED_SCRIPTS = {
    "show_mandates_happy_pass": SHOW_MANDATES_HAPPY_PASS,
    "show_mandates_fail_cross_account_hallucination": SHOW_MANDATES_FAIL_CROSS_ACCOUNT_HALLUCINATION,
    "show_mandates_fail_over_clarification": SHOW_MANDATES_FAIL_OVER_CLARIFICATION,
    "cancel_mandate_happy_pass": CANCEL_MANDATE_HAPPY_PASS,
    "cancel_mandate_fail_no_confirmation": CANCEL_MANDATE_FAIL_NO_CONFIRMATION,
    "cancel_mandate_fail_wrong_mandate": CANCEL_MANDATE_FAIL_WRONG_MANDATE,
}
