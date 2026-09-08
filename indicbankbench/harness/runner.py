"""Runner — ARCHITECTURE.md Layer 2, step 3.

Assembles the system message (base prompt + login_context) + tool schemas, drives
the user_turns + tool loop to a terminal answer per turn, and returns the full
transcript. No grading here — that's grader.py/judge.py.

Internal transcript convention: an assistant tool-call message's
`tool_calls[i].function.arguments` is a plain Python dict (for readability and for
the grader). It is only serialized to a JSON string when sent to the model as
conversation history (OpenAI wire format requires a string there) — see _to_wire.
"""
import copy
import json

from . import mock_executor, prompt, tools

MAX_TOOL_ITERS = 6


class RunnerError(Exception):
    def __init__(self, message, transcript=None):
        super().__init__(message)
        # Attach the partially-built transcript so a loop-exceeded failure is diagnosable —
        # the caller can persist it instead of losing what the model actually did.
        self.transcript = transcript


def _to_wire(transcript):
    wire = []
    for msg in transcript:
        m = copy.deepcopy(msg)
        if m.get("tool_calls"):
            for tc in m["tool_calls"]:
                if isinstance(tc["function"]["arguments"], dict):
                    tc["function"]["arguments"] = json.dumps(tc["function"]["arguments"])
        wire.append(m)
    return wire


def run_case(case, model_client, candidate_profile="candidate_default", max_tool_iters=MAX_TOOL_ITERS,
             base_prompt_path=None):
    _, tool_defs_by_name = tools.load_tool_definitions()
    raw_tools_by_name, openai_schemas = tools.resolve_tools_exposed(
        case["tools_exposed"], tool_defs_by_name
    )

    system_content = prompt.render_system_prompt(
        case["setup"], list(raw_tools_by_name.keys()), base_prompt_path=base_prompt_path
    )
    # Per-run mock state, owned here and never written back to `case`: the same case object is
    # shared across passes and worker threads, so mutating it would leak one run's writes into
    # another's reads. Only `outcome_merged` mocks use it (mock_executor._merge_state).
    mock_state = {}
    transcript = [{"role": "system", "content": system_content}]
    transcript.extend(copy.deepcopy(case["setup"].get("prior_messages", [])))

    for turn in case["user_turns"]:
        transcript.append({"role": "user", "content": turn["content"]})

        for _ in range(max_tool_iters):
            response = model_client.chat(
                candidate_profile, _to_wire(transcript), tools=openai_schemas
            )
            if response.get("tool_calls"):
                assistant_msg = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": response["tool_calls"],
                }
                # Preserve reasoning for multi-turn context (model needs its thread).
                # The judge never sees this field — judge.py:_render_transcript reads
                # only content and tool_calls.
                if response.get("reasoning"):
                    assistant_msg["reasoning"] = response["reasoning"]
                if response.get("reasoning_details"):
                    assistant_msg["reasoning_details"] = response["reasoning_details"]
                transcript.append(assistant_msg)
                for tc in response["tool_calls"]:
                    name = tc["function"]["name"]
                    args = tc["function"]["arguments"]
                    output = mock_executor.execute(name, args, case, raw_tools_by_name, mock_state)
                    transcript.append(
                        {"role": "tool", "tool_call_id": tc["id"], "content": output}
                    )
                continue
            final_msg = {"role": "assistant", "content": response.get("content")}
            if response.get("reasoning"):
                final_msg["reasoning"] = response["reasoning"]
            if response.get("reasoning_details"):
                final_msg["reasoning_details"] = response["reasoning_details"]
            transcript.append(final_msg)
            break
        else:
            raise RunnerError(
                f"max_tool_iters ({max_tool_iters}) exceeded without a final answer "
                f"for case {case.get('case_id')}",
                transcript,
            )

    return transcript
