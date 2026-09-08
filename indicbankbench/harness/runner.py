"""Run a candidate through one scripted benchmark case."""
import copy
import json

from . import mock_executor, prompt, tools

MAX_TOOL_ITERS = 6


class RunnerError(Exception):
    def __init__(self, message, transcript=None):
        super().__init__(message)
        # Preserve the partial transcript for diagnosis.
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
    # Keep mock state isolated to this run.
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
