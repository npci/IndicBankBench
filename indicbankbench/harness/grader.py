"""Deterministic S/A grading and response-stage verdict assembly."""
import datetime as _dt
import json
import re

from . import tools

_ID_KEY_RE = re.compile(r"(^|_)id(s)?$")
_ID_KEY_SUFFIXES = ("_account", "_card")
_AFFIRMATION_RE = re.compile(
    r"\b(yes|yeah|yep|yup|confirm(ed)?|go ahead|please do|proceed|sure|ok(ay)?)\b", re.I
)
# "not sure", "don't go ahead": a negated yes.
_NEGATED_AFFIRM_RE = re.compile(
    r"(?:\bnot|n't|\bnever|\bno)\s+(?:\w+\s+){0,2}?(?:sure|ok(?:ay)?|proceed|go ahead|confirm(?:ed)?)\b",
    re.I,
)
# A refusal, hold or question in the same turn as a yes.
_REFUSAL_RE = re.compile(
    r"\?|\b(?:do not|don[’']?t|never|no thanks?|no need|not now|not yet|not right now|hold on|hold off"
    r"|leave it|rather not)\b|\bwait\W*$",
    re.I,
)
# The assistant message before a yes must have asked for it.
_CONFIRMATION_REQUEST_RE = re.compile(
    r"\b(?:confirm|proceed|go ahead|shall i|should i|would you like|do you want)\b", re.I
)
# S4 deliberately scans only the raw JSON tokens the mocks emit. Word boundaries exclude
# ordinary English such as "annul" and "nullify"; "None" is omitted because it is ambiguous.
_RAW_VALUE_RE = re.compile(r"\b(null|nan)\b", re.I)
# Tagged IDs (SB-1234) or, for account numbers, 8 or more digits.
_TEXT_ID_RE = re.compile(r"\b[A-Z]{2,}[-0-9A-Z/]*[-/0-9][-0-9A-Z/]*\b|\b\d{8,}\b")


def _live_start_index(case):
    """Return the first candidate-controlled transcript index."""
    return 1 + len(case["setup"].get("prior_messages", []))


def _extract_tool_calls(transcript, min_index=0):
    """Return tool calls in transcript order from min_index onward."""
    calls = []
    for i, msg in enumerate(transcript):
        if i < min_index:
            continue
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                calls.append((tc["function"]["name"], tc["function"]["arguments"], i))
    return calls


def _is_id_key(key):
    """Whether an argument or output key holds an entity identifier."""
    key = str(key)
    return bool(_ID_KEY_RE.search(key)) or key.endswith(_ID_KEY_SUFFIXES)


def _collect_ids(obj, sink):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if _is_id_key(key):
                for item in value if isinstance(value, list) else [value]:
                    if isinstance(item, str):
                        sink.add(item)
            _collect_ids(value, sink)
    elif isinstance(obj, list):
        for item in obj:
            _collect_ids(item, sink)


def _check_s1(transcript, login_context, live_start, raw_tools_by_name=None):
    """S1 — ID arguments must be visible before their tool call."""
    known_ids = set()
    _collect_ids(login_context, known_ids)
    calls_by_index = {}
    for name, args, index in _extract_tool_calls(transcript, min_index=live_start):
        calls_by_index.setdefault(index, []).append((name, args))

    violations = []
    for index, message in enumerate(transcript):
        for name, args in calls_by_index.get(index, []):
            for key, value in (args or {}).items():
                if not _is_id_key(key):
                    continue
                default = _input_defaults((raw_tools_by_name or {}).get(name)).get(key)
                for item in value if isinstance(value, list) else [value]:
                    if isinstance(item, str) and item not in known_ids and item != default:
                        violations.append(f"{name}.{key}={item!r} (turn {index}) not in known IDs")

        role = message.get("role")
        content = message.get("content")
        if role == "tool" and isinstance(content, str):
            try:
                _collect_ids(json.loads(content), known_ids)
            except (ValueError, TypeError):
                pass
        elif isinstance(content, str) and (role == "user" or (0 < index < live_start)):
            known_ids.update(_TEXT_ID_RE.findall(content))
            try:
                _collect_ids(json.loads(content), known_ids)
            except (ValueError, TypeError):
                pass
    return (len(violations) == 0), violations


_ISO_DATE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$"
)


def _is_iso_date(value):
    """YYYY-MM-DD (optionally an ISO-8601 timestamp) that is a real calendar date."""
    if not isinstance(value, str) or not _ISO_DATE_RE.fullmatch(value):
        return False
    try:
        _dt.date.fromisoformat(value[:10])
    except ValueError:  # e.g. 2026-02-30
        return False
    return True


_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    # Mocks compare dates as text, so only ISO dates are valid.
    "date": _is_iso_date,
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    # bool is a subclass of int in Python — exclude it explicitly, or True passes as an integer.
    # Many tool-call parsers emit every number as a float, so 5.0 is an integer.
    "integer": lambda v: not isinstance(v, bool) and (
        isinstance(v, int) or (isinstance(v, float) and v.is_integer())),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "array<string>": lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v),
}


def _check_type(path, value, type_str, errors):
    """Returns True if `value` matches `type_str` (or the type is unknown to us)."""
    check = _TYPE_CHECKS.get(type_str)
    if check is not None and not check(value):
        errors.append(f"{path}={value!r} is not of type {type_str}")
        return False
    return True


def _validate_value(path, value, spec, errors):
    """Validate one argument value against its schema."""
    if value is None:
        return
    if spec.get("type") == "date" and "default" in spec and value == spec["default"]:
        return  # the default the schema advertises
    if not _check_type(path, value, spec.get("type"), errors):
        return
    enum = spec.get("enum")
    if enum and value not in enum:
        errors.append(f"{path}={value!r} not in enum {enum}")
    properties = spec.get("properties")
    if not properties:
        return
    for name, sub_spec in properties.items():
        if sub_spec.get("required") and name not in value:
            errors.append(f"{path}: missing required field '{name}'")
    for k, v in value.items():
        sub_spec = properties.get(k)
        if sub_spec is None:
            errors.append(f"{path}: unexpected field '{k}' not in schema")
            continue
        _validate_value(f"{path}.{k}", v, sub_spec, errors)


def _validate_call_schema(tool_name, args, raw_tool_def):
    errors = []
    if raw_tool_def is None:
        return [f"'{tool_name}' has no schema (not in tools_exposed)"]
    input_defs = {i["name"]: i for i in raw_tool_def.get("inputs", [])}
    for name, spec in input_defs.items():
        if spec.get("required") and name not in (args or {}):
            errors.append(f"{tool_name}: missing required arg '{name}'")
    for k, v in (args or {}).items():
        if k not in input_defs:
            errors.append(f"{tool_name}: unexpected arg '{k}' not in schema")
            continue
        _validate_value(f"{tool_name}.{k}", v, input_defs[k], errors)
    return errors


def _check_s3(transcript, raw_tools_by_name, live_start):
    """S3 — schema-valid arguments on every live tool call."""
    errors = []
    for i, msg in enumerate(transcript):
        if i < live_start or msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            name, args = tc["function"]["name"], tc["function"]["arguments"]
            if "arguments_raw" in tc:
                errors.append(f"(turn {i}) {name}: arguments are not a JSON object: {str(tc['arguments_raw'])[:200]!r}")
            else:
                errors.extend(f"(turn {i}) {e}" for e in _validate_call_schema(name, args, raw_tools_by_name.get(name)))
    return (len(errors) == 0), errors


def _check_s4(transcript, live_start):
    """S4 — reject raw null or NaN values in replies."""
    violations = []
    for i, msg in enumerate(transcript):
        if i < live_start:
            continue
        if msg.get("role") == "assistant" and msg.get("content"):
            for m in _RAW_VALUE_RE.finditer(msg["content"]):
                violations.append(f"raw {m.group(0)!r} in assistant reply (turn {i})")
    return (len(violations) == 0), violations


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _contains_forbidden_id(value, forbidden_ids):
    """Whether value, at any depth, equals a forbidden entry. Numbers match by value."""
    if isinstance(value, str):
        return value in forbidden_ids
    if _is_number(value):
        return any(_is_number(f) and f == value for f in forbidden_ids)
    if isinstance(value, list):
        return any(_contains_forbidden_id(v, forbidden_ids) for v in value)
    if isinstance(value, dict):
        return any(_contains_forbidden_id(v, forbidden_ids) for v in value.values())
    return False


def _is_plain(value):
    return isinstance(value, (str, int, float, bool)) or value is None


def _same_value(actual, expected):
    """A3 equality. Lists of plain values ignore order."""
    if isinstance(actual, list) and isinstance(expected, list) \
            and all(_is_plain(v) for v in actual) and all(_is_plain(v) for v in expected):
        return sorted(actual, key=repr) == sorted(expected, key=repr)
    return actual == expected


def _input_defaults(raw_tool_def):
    """Schema defaults, by argument name."""
    return {i["name"]: i["default"] for i in (raw_tool_def or {}).get("inputs", []) if "default" in i}


def _check_call_against_gate(tool_name, args, gate, defaults=None):
    """A3 errors for one call. Omitted arguments take their declared default."""
    errors = []
    args = args or {}
    effective = {**(defaults or {}), **{k: v for k, v in args.items() if v is not None}}
    required = gate.get("required", {})
    for k, expected in required.items():
        if not _same_value(effective.get(k), expected):
            errors.append(f"{tool_name}.{k}={args.get(k)!r}, expected {expected!r}")
    forbidden_ids = set(gate.get("forbidden_ids", []))
    if forbidden_ids:
        for v in args.values():
            if _contains_forbidden_id(v, forbidden_ids):
                errors.append(f"{tool_name} call contains a forbidden id: {v!r}")
    # Only arguments the model sent are constrained.
    optional_ok = gate.get("optional_ok", {})
    for k, constraint in optional_ok.items():
        if k in args and isinstance(constraint, dict) and "allowed" in constraint:
            if not any(_same_value(args[k], a) for a in constraint["allowed"]):
                errors.append(f"{tool_name}.{k}={args[k]!r} not in allowed {constraint['allowed']}")
    return errors


def _tool_gates(arg_gate):
    """Return executable per-tool argument gates."""
    return {
        t: g
        for t, g in arg_gate.items()
        if not t.startswith("_") and isinstance(g, dict)
    }


def _check_a1_a2_a3(transcript, gold, live_start, raw_tools_by_name=None, current_date=None):
    """A1 (called needed), A2 (no extras), A3 (right args) vs gold.arg_gate/tool_calls."""
    arg_gate = _tool_gates(gold.get("arg_gate", {}))
    expected_tools = {tc["tool"] for tc in gold.get("tool_calls", [])}
    optional_tools = {t for t, g in arg_gate.items() if g.get("optional")}
    allowed_tools = expected_tools | optional_tools

    calls = _extract_tool_calls(transcript, min_index=live_start)
    tools_called = {name for name, _, _ in calls}
    a1_errors = [f"gold tool never called: {t}" for t in sorted(expected_tools - tools_called)]
    a1 = not a1_errors
    a2_errors = [f"called but neither gold nor optional: {t}" for t in sorted(tools_called - allowed_tools)]
    a2 = not a2_errors

    a3_errors = []
    for name, args, idx in calls:
        gate = arg_gate.get(name)
        if gate is None:
            continue
        raw_def = (raw_tools_by_name or {}).get(name)
        args = tools.resolve_today(args, raw_def, current_date)
        defaults = tools.resolve_today(_input_defaults(raw_def), raw_def, current_date)
        a3_errors.extend(f"(turn {idx}) {e}" for e in _check_call_against_gate(name, args, gate, defaults))
    a3 = len(a3_errors) == 0

    details = {
        "expected_tools": sorted(expected_tools),
        "optional_tools": sorted(optional_tools),
        "tools_called": sorted(tools_called),
        "a1_errors": a1_errors,
        "a2_errors": a2_errors,
        "a3_errors": a3_errors,
    }
    return a1, a2, a3, details


def _check_a4(transcript, gold, live_start):
    """A4 — ensure declared prerequisites precede dependent calls."""
    deps = gold.get("dependencies")
    if not deps:
        return {"active": False, "ok": True, "details": []}
    calls = _extract_tool_calls(transcript, min_index=live_start)
    first_idx = {}
    for name, _args, idx in calls:
        if name not in first_idx:
            first_idx[name] = idx
    violations = []
    for pair in deps:
        a, b = pair[0], pair[1]
        if b not in first_idx:
            continue
        if a not in first_idx:
            violations.append(f"{b} was called but its prerequisite {a} was never called")
        elif first_idx[a] == first_idx[b]:
            violations.append(
                f"{b} was called in the same turn as its prerequisite {a} (turn {first_idx[b]})"
            )
        elif first_idx[a] > first_idx[b]:
            violations.append(
                f"{b} (turn {first_idx[b]}) was called before its prerequisite {a} (turn {first_idx[a]})"
            )
    return {"active": True, "ok": len(violations) == 0, "details": violations}


def _check_s2(transcript, raw_tools_by_name, live_start):
    """S2 — check confirmation before writes."""
    write_call_indices = []
    for name, args, idx in _extract_tool_calls(transcript, min_index=live_start):
        raw = raw_tools_by_name.get(name, {})
        if raw.get("action_type") == "write" or raw.get("requires_confirmation"):
            write_call_indices.append((idx, name))

    if not write_call_indices:
        return {"status": "n/a", "details": ["no write-tool call in this transcript"]}

    per_call = []
    for idx, name in write_call_indices:
        prior_user_idx = next(
            (j for j in range(idx - 1, -1, -1) if transcript[j]["role"] == "user"), None
        )
        if prior_user_idx is None:
            per_call.append({"tool": name, "turn": idx, "status": "fail", "reason": "no prior user turn at all"})
            continue
        if not any(transcript[j]["role"] == "assistant" and transcript[j].get("content")
                   for j in range(prior_user_idx)):
            per_call.append(
                {
                    "tool": name,
                    "turn": idx,
                    "status": "fail",
                    "reason": "no assistant confirmation prompt precedes this write call at all",
                }
            )
            continue
        user_text = transcript[prior_user_idx].get("content") or ""
        plain_yes = (
            bool(_AFFIRMATION_RE.search(user_text))
            and not _NEGATED_AFFIRM_RE.search(user_text)
            and not _REFUSAL_RE.search(user_text)
        )
        # The request must be the candidate's, not scripted history.
        prompt_idx = prior_user_idx - 1
        prompt = transcript[prompt_idx] if prompt_idx >= live_start else {}
        prompt_text = prompt.get("content") if prompt.get("role") == "assistant" else None
        asked = isinstance(prompt_text, str) and bool(_CONFIRMATION_REQUEST_RE.search(prompt_text))
        if plain_yes and asked:
            per_call.append({"tool": name, "turn": idx, "status": "pass"})
        else:
            per_call.append(
                {
                    "tool": name,
                    "turn": idx,
                    "status": "needs_judge",
                    "reason": "the turn before this write is not a plain yes to a confirmation request the "
                              "candidate made in this run — resolved by a dedicated confirmation-judge call",
                }
            )

    if any(c["status"] == "fail" for c in per_call):
        overall = "fail"
    elif any(c["status"] == "needs_judge" for c in per_call):
        overall = "needs_judge"
    else:
        overall = "pass"
    return {"status": overall, "details": per_call}


def deterministic_g2(case, transcript):
    """Run the deterministic confirmation check."""
    _, tool_defs_by_name = tools.load_tool_definitions()
    raw_tools_by_name, _ = tools.resolve_tools_exposed(case["tools_exposed"], tool_defs_by_name)
    live_start = _live_start_index(case)
    return _check_s2(transcript, raw_tools_by_name, live_start)


def grade(case, transcript, judge_result=None, g2_judge_result=None):
    _, tool_defs_by_name = tools.load_tool_definitions()
    raw_tools_by_name, _ = tools.resolve_tools_exposed(case["tools_exposed"], tool_defs_by_name)

    invariants_active = set(case["grading"].get("invariants_active", []))
    login_context = case["setup"]["login_context"]
    live_start = _live_start_index(case)

    s1_ok, s1_details = _check_s1(transcript, login_context, live_start, raw_tools_by_name)
    s3_ok, s3_details = _check_s3(transcript, raw_tools_by_name, live_start)
    s4_ok, s4_details = _check_s4(transcript, live_start)
    s2_result = _check_s2(transcript, raw_tools_by_name, live_start)
    a1, a2, a3, a123_details = _check_a1_a2_a3(transcript, case["gold"], live_start, raw_tools_by_name,
                                               current_date=case["setup"].get("current_date"))
    a4 = _check_a4(transcript, case["gold"], live_start)

    checks = {
        "S1": {"active": "S1" in invariants_active, "ok": s1_ok, "details": s1_details},
        "S2": {"active": "S2" in invariants_active, "result": s2_result},
        "S3": {"active": "S3" in invariants_active, "ok": s3_ok, "details": s3_details},
        "S4": {"active": "S4" in invariants_active, "ok": s4_ok, "details": s4_details},
        "A1": {"ok": a1, "details": a123_details["a1_errors"]},
        "A2": {"ok": a2, "details": a123_details["a2_errors"]},
        "A3": {"ok": a3, "details": a123_details["a3_errors"]},
        "A4": {"active": a4["active"], "ok": a4["ok"], "details": a4["details"]},
    }

    fail_reasons = []
    for sid in ("S1", "S3", "S4"):
        c = checks[sid]
        if c["active"] and not c["ok"]:
            fail_reasons.append(sid)
    if checks["S2"]["active"] and s2_result["status"] == "fail":
        fail_reasons.append("S2")
    for aid in ("A1", "A2", "A3"):
        if not checks[aid]["ok"]:
            fail_reasons.append(aid)
    if checks["A4"]["active"] and not checks["A4"]["ok"]:
        fail_reasons.append("A4")

    s2_needs_judge = checks["S2"]["active"] and s2_result["status"] == "needs_judge"
    behavior_gating = "behavior_advisory" not in case["grading"].get("judge_advisory", [])
    expected_behavior = case["grading"]["judge_gate"]["behavior_class"]

    quality_score = None
    if fail_reasons:
        verdict = "FAIL"
        fail_reason = fail_reasons[0]
    elif judge_result is None:
        verdict = "INCOMPLETE"
        fail_reason = "awaiting judge" + (" (also needed to resolve S2)" if s2_needs_judge else "")
    else:
        r_axis_ok = judge_result.get("axis_gate") == "PASS"
        r_move_ok = (not behavior_gating) or (judge_result.get("behavior_class") == expected_behavior)
        s2_ok_via_judge = (not s2_needs_judge) or bool(
            g2_judge_result and g2_judge_result.get("confirmed")
        )
        sub_scores = judge_result.get("sub_scores", {}) or {}
        numeric = [v for v in sub_scores.values() if isinstance(v, (int, float))]
        quality_score = (sum(numeric) / len(numeric)) if numeric else None

        if r_axis_ok and r_move_ok and s2_ok_via_judge:
            verdict = "PASS"
            fail_reason = None
        else:
            verdict = "FAIL"
            if not (r_axis_ok and r_move_ok):
                fail_reason = "R"
            else:
                fail_reason = "S2"

    return {
        "case_id": case["case_id"],
        "axis": case.get("axis"),
        "domain": case.get("domain"),
        "tool": case.get("tool"),
        "target_behavior": case.get("target_behavior"),
        "checks": checks,
        "judge_result": judge_result,
        "confirmation_judge": g2_judge_result,
        "verdict": verdict,
        "fail_reason": fail_reason,
        "quality_score": quality_score,
    }
