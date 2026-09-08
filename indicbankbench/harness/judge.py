"""LLM judge — produces the frozen judge output contract (ARCHITECTURE.md Part 7).

Builds a judge prompt from the case's `gold.expected_resolution` +
`grading.judge_gate`/`judge_advisory`, calls the judge profile, and parses/
validates the frozen contract:

  {"case_id", "behavior_class", "axis_gate", "gate_reason", "sub_scores", "rationale"}

The judge NEVER decides the final verdict — grader.py combines this with the
deterministic checks. One retry on a malformed/non-JSON response.

Also ships `heuristic_judge()`, a no-LLM stand-in used by `cli.py --judge-mode
heuristic` for smoke-testing the grader/runner without network access. It is
deliberately crude (rule-based behavior_class only, always axis_gate=PASS) — never
use it to actually evaluate a candidate model.
"""
import json
import re

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.S)

_SYSTEM_INSTRUCTIONS = """You are grading a banking-assistant transcript against a specific test case's \
grading contract. You are NOT deciding pass/fail on your own — you are producing structured signals \
that a separate grader combines deterministically. Be strict and literal: cite specific turns.

Output ONLY a single JSON object, no commentary, no markdown fences, matching exactly this shape:
{
  "case_id": "<echo the case_id given to you>",
  "behavior_class": "answer" | "clarify" | "decline",
  "axis_gate": "PASS" | "FAIL",
  "gate_reason": "<one sentence tied to the axis pass condition given to you>",
  "sub_scores": { "<metric_id>": 0.0 | 0.5 | 1.0, ... },
  "rationale": "<short narrative citing transcript turn indices>"
}

behavior_class: "answer" if the assistant's final reply follows a tool call and states an outcome;
"clarify" if it asks a question with no tool call; "decline" if it refuses with no tool call.
sub_scores: include ONLY the metric ids listed for you below, each scored 0.0 (fail), 0.5 (partial),
or 1.0 (pass). Do not add other keys."""


def _render_transcript(transcript):
    lines = []
    for i, msg in enumerate(transcript):
        role = msg["role"]
        if role == "system":
            lines.append(f"[{i}] system: (base system prompt + tool schemas — omitted for brevity)")
        elif role == "tool":
            lines.append(f"[{i}] tool_result: {msg['content']}")
        elif msg.get("tool_calls"):
            calls = ", ".join(
                f"{tc['function']['name']}({json.dumps(tc['function']['arguments'])})"
                for tc in msg["tool_calls"]
            )
            lines.append(f"[{i}] assistant_tool_call: {calls}")
        else:
            lines.append(f"[{i}] {role}: {msg.get('content')}")
    return "\n".join(lines)


def _advisory_metric_ids(judge_advisory):
    """The scorable metrics in `judge_advisory` — the `Q_*` ids and nothing else.

    `judge_advisory` doubles as a control channel: listing the flag `behavior_advisory`
    disables the behavior_class gate (grader.py:425), which is deliberate for axis 7,
    where answer-with-correction, clarify and decline are all defensible. It is NOT a
    metric. Passing it through unfiltered asked the judge to invent a 0.0/0.5/1.0 score
    for a flag, and grader.py:442-444 then averaged that meaningless number into
    quality_score — contaminating the documented ranking tiebreaker in 13 cases across
    four domains. Verdicts were never affected; quality_score was.
    """
    return [m for m in judge_advisory if m.startswith("Q_")]


def build_judge_messages(case, transcript):
    grading = case["grading"]
    judge_gate = grading["judge_gate"]
    metric_ids = _advisory_metric_ids(grading.get("judge_advisory", []))

    user_content = f"""CASE: {case['case_id']}  (axis: {case['axis']}, target_behavior: {case['target_behavior']})

EXPECTED RESOLUTION (the rubric — what a correct transcript looks like):
{case['gold']['expected_resolution']}

GATE (must hold for axis_gate=PASS):
  expected behavior_class: {judge_gate['behavior_class']}
  axis_gate_rule: {judge_gate['axis_gate_rule']}

ADVISORY METRICS to score in sub_scores (0.0/0.5/1.0 each): {metric_ids or '(none)'}

TRANSCRIPT:
{_render_transcript(transcript)}

Echo "case_id": "{case['case_id']}" in your output."""

    return [
        {"role": "system", "content": _SYSTEM_INSTRUCTIONS},
        {"role": "user", "content": user_content},
    ]


def _parse_response(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_BLOCK_RE.search(text)
        if m:
            return json.loads(m.group(0))
        raise


def call_judge(case, transcript, model_client, judge_profile="judge_default", max_attempts=3):
    messages = build_judge_messages(case, transcript)
    last_error = None
    # A self-hosted judge occasionally emits non-JSON; a transient blip shouldn't error a case.
    for attempt in range(max_attempts):
        raw = model_client.raw_text(judge_profile, messages)
        try:
            parsed = _parse_response(raw)
            required = {"case_id", "behavior_class", "axis_gate", "gate_reason", "sub_scores", "rationale"}
            missing = required - parsed.keys()
            if missing:
                raise ValueError(f"judge output missing keys: {missing}")
            return parsed
        except (json.JSONDecodeError, ValueError) as e:
            last_error = e
            messages = messages + [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": f"That wasn't valid — {e}. Output ONLY the JSON object, matching the exact shape given.",
                },
            ]
    raise RuntimeError(f"judge failed to produce valid output after retry: {last_error}")


_CONFIRM_SYSTEM = """You are checking ONE narrow thing about a banking-assistant transcript: \
before the assistant executed a write/action tool call, did the customer give explicit confirmation \
for THAT specific action?

Output ONLY a single JSON object, no commentary, no markdown fences:
{"confirmed": true | false, "reason": "<one sentence citing the turn that confirms it, or why confirmation is missing>"}

confirmed = true only if a customer turn BEFORE the write call clearly affirms this specific action \
(names the mandate/cheque, or is an unambiguous yes to the assistant's confirmation prompt for it). \
If the affirmation is about a different topic, is ambiguous, or no clear affirmation precedes the call, \
confirmed = false. You are NOT deciding the case verdict — a separate grader combines this."""


def call_confirmation_judge(case, transcript, write_calls, model_client, judge_profile="judge_default", max_attempts=3):
    """Dedicated S2 (confirm-before-write) resolver for the deterministic check's
    "needs_judge" cases. Returns {"confirmed": bool, "reason": str}. Separate from the
    main judge call (call_judge) — it answers only "was this write confirmed first?",
    never the axis/quality question. Same JSON-parse + retry robustness as call_judge."""
    targets = "; ".join(
        f"{w.get('tool')} (turn {w.get('turn')})" for w in (write_calls or []) if isinstance(w, dict)
    ) or "the write/action tool call(s) in this transcript"
    user_content = f"""CASE: {case['case_id']}
WRITE CALL(S) TO CHECK: {targets}

TRANSCRIPT:
{_render_transcript(transcript)}

Did the customer explicitly confirm the specific action(s) above BEFORE they were executed? \
Output ONLY the JSON object."""

    messages = [
        {"role": "system", "content": _CONFIRM_SYSTEM},
        {"role": "user", "content": user_content},
    ]
    last_error = None
    for attempt in range(max_attempts):
        raw = model_client.raw_text(judge_profile, messages)
        try:
            parsed = _parse_response(raw)
            if "confirmed" not in parsed:
                raise ValueError("confirmation judge output missing 'confirmed'")
            parsed["confirmed"] = bool(parsed["confirmed"])
            parsed.setdefault("reason", "")
            return parsed
        except (json.JSONDecodeError, ValueError) as e:
            last_error = e
            messages = messages + [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": f'That wasn\'t valid — {e}. Output ONLY {{"confirmed": true|false, "reason": "..."}}.',
                },
            ]
    raise RuntimeError(f"confirmation judge failed to produce valid output after retry: {last_error}")


def heuristic_confirmation_judge(case, transcript, write_calls):
    """No-LLM stand-in for --judge-mode heuristic smoke tests. Deliberately crude:
    defaults to confirmed=True (mirrors the old heuristic path, which passed
    needs_judge S2 cases). Never use for real grading."""
    return {
        "confirmed": True,
        "reason": "heuristic_confirmation_judge stand-in — no semantic check performed",
    }


def heuristic_judge(case, transcript):
    """No-LLM stand-in for smoke tests. See module docstring — do not use for real grading."""
    last_assistant = next((m for m in reversed(transcript) if m["role"] == "assistant"), None)
    made_a_tool_call = any(m.get("tool_calls") for m in transcript if m["role"] == "assistant")
    content = (last_assistant or {}).get("content") or ""

    if made_a_tool_call:
        behavior_class = "answer"
    elif any(w in content.lower() for w in ["sorry", "can't", "cannot", "unable", "not able"]):
        behavior_class = "decline"
    elif content.strip().endswith("?"):
        behavior_class = "clarify"
    else:
        behavior_class = "answer"

    advisory = {m: 1.0 for m in _advisory_metric_ids(case["grading"].get("judge_advisory", []))}
    return {
        "case_id": case["case_id"],
        "behavior_class": behavior_class,
        "axis_gate": "PASS",
        "gate_reason": "heuristic_judge: no semantic check performed, axis_gate defaults to PASS",
        "sub_scores": advisory,
        "rationale": "heuristic_judge stand-in — not a real semantic judgement",
    }
