"""Render the candidate system prompt for a case."""
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_ROOT = Path(__file__).resolve().parents[1]
BASE_PROMPT_PATH = BENCH_ROOT / "prompts" / "base_system_prompt.txt"


def render_system_prompt(setup, tool_names, base_prompt_path=None):
    path = Path(base_prompt_path) if base_prompt_path else BASE_PROMPT_PATH
    template = path.read_text()
    tool_list_line = ", ".join(tool_names)
    filled = (
        template.replace("{{CURRENT_DATE}}", setup["current_date"])
        .replace("{{CURRENT_TIME}}", setup["current_time"])
        .replace("{{LOGIN_CONTEXT_JSON}}", json.dumps(setup["login_context"]))
        .replace(
            "{{TOOL_SCHEMAS}}",
            f"You have the following tools available (schemas provided via function-calling, not repeated here): {tool_list_line}.",
        )
    )
    return filled
