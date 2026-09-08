"""Resolve a case's tools_exposed into:
  - OpenAI-style function-calling schemas (passed to the model via `tools=`)
  - raw tool-def dicts (used by the grader for S3 schema validation and by
    mock_executor to know each tool's input/output field names)

TWO FILES, TWO AUDIENCES (both describe the same 33 tools):

  tool_definitions.json          — the AUTHORING + HARNESS contract. Carries the
    structured `inputs` (S3 validation), `outputs` (mock output shapes + the
    result-wrapper field name mock_executor emits), `action_type` /
    `requires_confirmation` (S2 write detection), plus `prerequisites` / `notes`
    that case authors reason against. NEVER sent to the candidate.

  new_tool_definitions_v1.json   — the MODEL-FACING payload, already in OpenAI
    function format. Same names, same parameters, same enums (asserted below),
    but with category / action_type / prerequisites / notes folded into the
    `description` prose, so everything the candidate is graded on is disclosed to
    it in the one field it actually reads.

Keeping them split is what lets a case gate on a documented prerequisite without
violating the "never gate on something the model was not told" doctrine: the
prerequisite now IS in the description the model sees.

tools_exposed entries are either a string (looked up by name) or an inline dict
(a fabricated tool for axis 10 — see ARCHITECTURE.md Part 6 and
case_bank/accounts_and_transactions/cancel_mandate/unseen_tools.001.json). An
inline entry has no model-facing counterpart, so its schema is derived locally by
to_openai_schema().
"""
import json
from pathlib import Path

from . import contract

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_ROOT = Path(__file__).resolve().parents[1]
TOOL_DEFINITIONS_PATH = BENCH_ROOT / "tool_definitions.json"
MODEL_SCHEMAS_PATH = BENCH_ROOT / "new_tool_definitions_v1.json"

_TYPE_MAP = {
    "string": {"type": "string"},
    "date": {"type": "string", "format": "date"},
    "integer": {"type": "integer"},
    "number": {"type": "number"},
    "boolean": {"type": "boolean"},
    "object": {"type": "object"},
    "array<string>": {"type": "array", "items": {"type": "string"}},
}


def _json_type(input_def):
    """Convert one input/property spec to JSON Schema.

    Recurses through `properties` so NESTED object shapes actually reach the model.
    Without this, a nested arg was advertised as a bare {"type": "object"} — e.g.
    set_card_controls' atm/online/pos, whose real shape is channel x region ->
    {enabled, daily_limit}, and create_fd/create_rd's `nominee`. The model could not
    know the structure and had to guess, then got graded against the full schema by S3.
    """
    schema = dict(_TYPE_MAP.get(input_def["type"], {"type": "string"}))
    if "enum" in input_def:
        schema["enum"] = input_def["enum"]
    if "description" in input_def:
        schema["description"] = input_def["description"]
    properties = input_def.get("properties")
    if properties:
        schema["properties"] = {name: _json_type(spec) for name, spec in properties.items()}
        nested_required = [name for name, spec in properties.items() if spec.get("required")]
        if nested_required:
            schema["required"] = nested_required
    return schema


def load_tool_definitions(path=None):
    """Returns (raw_file_data, canonical_defs_by_name).

    The contract file is `schema_version: 3.0-extended` (JSON Schema `parameters` /
    `output_schema`), while grader.py and mock_executor.py read the canonical legacy shape
    (`inputs` / `outputs`). contract.normalize() bridges the two — see contract.py for why the
    conversion lives at the boundary rather than in the readers. The RAW data is returned
    alongside because assert_schema_parity() must diff the contract's own `parameters` against
    the model file's, not a converted copy of it.
    """
    path = Path(path) if path else TOOL_DEFINITIONS_PATH
    with open(path) as f:
        data = json.load(f)
    by_name = {t["name"]: contract.normalize(t) for t in data["tools"]}
    return data, by_name


def load_model_schemas(path=None):
    """The model-facing OpenAI function schemas, keyed by tool name.

    The file is already in `tools=` shape, so entries are passed through verbatim
    rather than rebuilt — the descriptions are the point of the file and must reach
    the candidate unaltered.
    """
    path = Path(path) if path else MODEL_SCHEMAS_PATH
    with open(path) as f:
        data = json.load(f)
    return {entry["function"]["name"]: entry for entry in data["tools"]}


def to_openai_schema(tool_def):
    """Convert one tool_definitions.json entry (or an inline fabricated dict of the
    same shape) into an OpenAI chat-completions `tools[]` function schema."""
    properties = {}
    required = []
    for inp in tool_def.get("inputs", []):
        properties[inp["name"]] = _json_type(inp)
        if inp.get("required"):
            required.append(inp["name"])
    return {
        "type": "function",
        "function": {
            "name": tool_def["name"],
            "description": tool_def.get("description", ""),
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def resolve_tools_exposed(tools_exposed, tool_defs_by_name, model_schemas_by_name=None):
    """tools_exposed: list of str | dict (inline fabricated tool def).
    Returns (raw_defs_by_name, openai_schemas).

    raw defs come from tool_definitions.json (harness contract); the schema handed
    to the candidate comes from new_tool_definitions_v1.json when the tool has an
    entry there. A named tool missing from the model-facing file falls back to a
    schema derived from the raw def, so the harness still runs — but the candidate
    then sees the terse description, which is a case-authoring hazard, not a
    silent one: assert_schema_parity() in the test suite catches it.
    """
    if model_schemas_by_name is None:
        model_schemas_by_name = load_model_schemas()
    raw_by_name = {}
    schemas = []
    for entry in tools_exposed:
        if isinstance(entry, str):
            if entry not in tool_defs_by_name:
                raise KeyError(
                    f"tools_exposed names '{entry}' but it is not in tool_definitions.json"
                )
            tool_def = tool_defs_by_name[entry]
            schema = model_schemas_by_name.get(entry) or to_openai_schema(tool_def)
        elif isinstance(entry, dict):
            # Fabricated axis-10 tool: exists only in the case file, so there is no
            # model-facing entry to look up. These are authored in the legacy shape, which
            # normalize() passes through unchanged — that identity case is the main reason the
            # conversion lives at the boundary instead of in the readers.
            tool_def = contract.normalize(entry)
            schema = to_openai_schema(tool_def)
        else:
            raise TypeError(f"tools_exposed entry must be str or dict, got {type(entry)}")
        raw_by_name[tool_def["name"]] = tool_def
        schemas.append(schema)
    return raw_by_name, schemas


def assert_schema_parity(raw_contract=None, model_schemas_by_name=None):
    """The two files must describe identical tool parameters.

    S3 validates the candidate's arguments against the contract file, while the candidate only
    ever sees the model-facing file. Any divergence grades a model against a schema it was never
    shown.

    Both files now carry JSON Schema, so this is a direct diff of the contract's `parameters`
    against the model file's `function.parameters` — no conversion in between. That is both
    simpler and stricter than the previous name/required/enum spot-check: it catches a changed
    type, format, default, nested shape or description-only edit that the spot-check would have
    waved through. Comparing raw-to-raw also keeps this test independent of contract.normalize(),
    so a normalizer bug cannot mask a real mismatch.

    Returns a list of mismatch strings (empty when the files agree).
    """
    if raw_contract is None:
        raw_contract, _ = load_tool_definitions()
    if model_schemas_by_name is None:
        model_schemas_by_name = load_model_schemas()

    problems = []
    # A well-formed but EMPTY contract iterates zero tools and returns zero problems — reported as
    # clean. That matters beyond this function: three tests in test_tool_contract.py loop over
    # raw_contract["tools"] directly and would also pass vacuously, and this is the backstop they
    # rely on. A malformed file raises KeyError; an empty one has to be caught here.
    if len(raw_contract["tools"]) < 30:
        problems.append(
            f"contract declares only {len(raw_contract['tools'])} tools — expected ~32; refusing "
            f"to report parity as clean over a near-empty contract")
    for tool in raw_contract["tools"]:
        name = tool["name"]
        entry = model_schemas_by_name.get(name)
        if entry is None:
            problems.append(f"{name}: absent from the model-facing schema file")
            continue
        contract_params = tool.get("parameters", {})
        model_params = entry["function"].get("parameters", {})
        if contract_params != model_params:
            problems.append(
                f"{name}: parameters differ between the two files — "
                f"contract keys {sorted(contract_params.get('properties', {}))} / "
                f"required {sorted(contract_params.get('required', []))} vs "
                f"model keys {sorted(model_params.get('properties', {}))} / "
                f"required {sorted(model_params.get('required', []))}"
            )
    for name in sorted(set(model_schemas_by_name) - {t["name"] for t in raw_contract["tools"]}):
        problems.append(f"{name}: present in the model-facing file but absent from the contract")
    return problems
