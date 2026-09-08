#!/usr/bin/env python3
"""Validate IndicBankBench case files."""
import json
import re
import sys
import traceback
from collections import Counter
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCH))

from harness import mock_executor  # noqa: E402
from harness import paths  # noqa: E402
from harness import tools as harness_tools  # noqa: E402
from harness.grader import (  # noqa: E402
    _AFFIRMATION_RE,
    _ID_KEY_RE,
    _TEXT_ID_RE as TEXT_ID_RE,
    _collect_ids as collect_ids,
    _contains_forbidden_id,
    _tool_gates,
    _validate_call_schema,
)

CASE_BANK = paths.case_bank_path()
REPORT_PATH = BENCH / "results" / "CASE_LINT.md"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BEHAVIOR_CLASSES = {"answer", "clarify", "decline"}
INVARIANT_IDS = {"S1", "S2", "S3", "S4"}
MOCK_KINDS = {"record_set_filtered", "outcome_fixed", "outcome_merged"}

# ID-bearing keys outside the standard *_id(s) pattern.
EXTRA_ID_KEYS = {"source_account", "source_card", "from_account", "to_account",
                 "target_account"}

# Looser affirmations handled by the confirmation judge.
WEAK_AFFIRM_RE = re.compile(
    r"\b(alright|i'?m ready|book it|go for it|make it happen|do it|start a|"
    r"good to go|please (?:go|do|book|start|create))\b",
    re.I,
)

SEVERITY_ORDER = {"ERROR": 0, "WARN": 1, "INFO": 2}


def collect_cases():
    """Load case files, retaining parse failures."""
    out = []
    if CASE_BANK is None:
        print(paths.FETCH_HINT, file=sys.stderr)
        sys.exit(2)
    for path in sorted(CASE_BANK.rglob("*.json")):
        if "_retired" in path.parts:
            continue
        try:
            case = json.loads(path.read_text())
        except (ValueError, OSError):
            out.append((path, None, None))
            continue
        out.append((path, case.get("case_id"), case))
    return out


def finding(case_id, severity, check, message):
    return {"case_id": case_id, "severity": severity, "check": check, "message": message}


def check_structure(case, findings):
    """Check required case structure."""
    cid = case.get("case_id") or "?"

    for field in ("case_id", "axis", "domain", "tool", "target_behavior"):
        if not isinstance(case.get(field), str) or not case.get(field):
            findings.append(finding(cid, "ERROR", "structure",
                                    f"top-level '{field}' missing or not a non-empty string"))

    gold = case.get("gold")
    if not isinstance(gold, dict):
        findings.append(finding(cid, "ERROR", "gold_missing",
                                "gold is missing or not a dict — the grader cannot run"))
    else:
        calls = gold.get("tool_calls")
        if not isinstance(calls, list):
            findings.append(finding(cid, "ERROR", "gold_tool_calls",
                                    "gold.tool_calls is missing or not a list"))
        else:
            for i, call in enumerate(calls):
                if not isinstance(call, dict) or not isinstance(call.get("tool"), str):
                    findings.append(finding(cid, "ERROR", "gold_call_shape",
                                            f"gold.tool_calls[{i}] missing a str 'tool'"))
                elif not isinstance(call.get("arguments"), dict):
                    findings.append(finding(cid, "ERROR", "gold_call_shape",
                                            f"gold.tool_calls[{i}] ({call['tool']}) arguments is not a dict"))
        if not isinstance(gold.get("expected_resolution"), str) or not gold["expected_resolution"].strip():
            findings.append(finding(cid, "ERROR", "gold_resolution",
                                    "gold.expected_resolution missing or empty — the judge prompt needs it"))

    grading = case.get("grading")
    if not isinstance(grading, dict):
        findings.append(finding(cid, "ERROR", "grading_missing",
                                "grading is missing or not a dict"))
    else:
        gate = grading.get("judge_gate")
        if not isinstance(gate, dict):
            findings.append(finding(cid, "ERROR", "judge_gate_missing",
                                    "grading.judge_gate is missing or not a dict"))
        else:
            if gate.get("behavior_class") not in BEHAVIOR_CLASSES:
                findings.append(finding(cid, "ERROR", "judge_gate_behavior",
                                        f"judge_gate.behavior_class={gate.get('behavior_class')!r} not in {sorted(BEHAVIOR_CLASSES)}"))
            if not isinstance(gate.get("axis_gate_rule"), str) or not gate["axis_gate_rule"].strip():
                findings.append(finding(cid, "ERROR", "judge_gate_rule",
                                        "judge_gate.axis_gate_rule missing or empty"))
        invariants = grading.get("invariants_active")
        if not isinstance(invariants, list) or not all(isinstance(x, str) for x in invariants):
            findings.append(finding(cid, "ERROR", "invariants",
                                    "invariants_active missing or not a list of strings"))
        elif not set(invariants) <= INVARIANT_IDS:
            findings.append(finding(cid, "ERROR", "invariants",
                                    f"invariants_active {invariants} not a subset of {sorted(INVARIANT_IDS)}"))

    setup = case.get("setup")
    if not isinstance(setup, dict):
        findings.append(finding(cid, "ERROR", "setup_missing", "setup is missing or not a dict"))
    else:
        date = setup.get("current_date")
        if not isinstance(date, str) or not DATE_RE.match(date):
            findings.append(finding(cid, "ERROR", "setup_date",
                                    f"setup.current_date={date!r} not YYYY-MM-DD"))
        if not isinstance(setup.get("current_time"), str) or not setup["current_time"].strip():
            findings.append(finding(cid, "ERROR", "setup_time", "setup.current_time missing or empty"))
        if not isinstance(setup.get("login_context"), dict):
            findings.append(finding(cid, "ERROR", "setup_login_context",
                                    "setup.login_context missing or not a dict — S1 needs it"))
        if not isinstance(setup.get("prior_messages"), list):
            findings.append(finding(cid, "ERROR", "setup_prior_messages",
                                    "setup.prior_messages missing or not a list"))

    turns = case.get("user_turns")
    if not isinstance(turns, list) or not turns:
        findings.append(finding(cid, "ERROR", "user_turns",
                                "user_turns missing or empty — the candidate would never be prompted"))
    else:
        for i, turn in enumerate(turns):
            if not isinstance(turn, dict) or not isinstance(turn.get("content"), str):
                findings.append(finding(cid, "ERROR", "user_turns",
                                        f"user_turns[{i}] missing a str 'content'"))

    exposed = case.get("tools_exposed")
    if not isinstance(exposed, list):
        findings.append(finding(cid, "ERROR", "tools_exposed",
                                "tools_exposed missing or not a list"))
    else:
        for i, entry in enumerate(exposed):
            if not isinstance(entry, (str, dict)):
                findings.append(finding(cid, "ERROR", "tools_exposed",
                                        f"tools_exposed[{i}] is {type(entry).__name__}, not a str or inline tool def"))
            elif isinstance(entry, dict) and not isinstance(entry.get("name"), str):
                findings.append(finding(cid, "ERROR", "tools_exposed",
                                        f"tools_exposed[{i}] inline tool def missing a str 'name'"))


def resolve_exposed(case, defs):
    """Resolve case tools without raising."""
    try:
        raw_by_name, _ = harness_tools.resolve_tools_exposed(case.get("tools_exposed", []), defs)
        return raw_by_name, None
    except (KeyError, TypeError) as exc:
        return None, str(exc)


def check_consistency(case, defs, findings, stats):
    """Check consistency between case fields and gold calls."""
    cid = case["case_id"]
    gold = case["gold"]
    gates = _tool_gates(gold.get("arg_gate", {}))

    raw_by_name, resolve_err = resolve_exposed(case, defs)
    if resolve_err:
        findings.append(finding(cid, "ERROR", "tools_exposed_unknown",
                                f"tools_exposed fails to resolve: {resolve_err}"))
        return
    exposed_names = set(raw_by_name)

    seen = set()
    for entry in case.get("tools_exposed", []):
        name = entry if isinstance(entry, str) else entry["name"]
        if name in seen:
            findings.append(finding(cid, "INFO", "tools_exposed_dup",
                                    f"'{name}' appears more than once in tools_exposed"))
        seen.add(name)

    for call in gold.get("tool_calls", []):
        if call["tool"] not in exposed_names:
            findings.append(finding(cid, "ERROR", "gold_tool_unexposed",
                                    f"gold calls '{call['tool']}' but it is not in tools_exposed"))
    for tool, gate in gold.get("arg_gate", {}).items():
        if tool.startswith("_"):
            continue
        if not isinstance(gate, dict):
            findings.append(finding(cid, "ERROR", "arg_gate_not_dict",
                                    f"arg_gate['{tool}'] is {type(gate).__name__} — the grader's _tool_gates silently skips it, dropping the gate"))
            continue
        if tool not in exposed_names:
            findings.append(finding(cid, "ERROR", "arg_gate_unexposed",
                                    f"arg_gate keys '{tool}' which is not in tools_exposed — a dead gate the grader can never apply"))

    mocks = case.get("mock") or {}
    needed = {call["tool"] for call in gold.get("tool_calls", [])}
    needed |= {t for t, g in gates.items() if g.get("optional")}
    for tool in sorted(needed):
        if tool not in mocks:
            findings.append(finding(cid, "ERROR", "mock_missing",
                                    f"tool '{tool}' is gold-required or optional-gated but has no mock entry"))

    for call in gold.get("tool_calls", []):
        errors = _validate_call_schema(call["tool"], call["arguments"], raw_by_name.get(call["tool"]))
        for err in errors:
            findings.append(finding(cid, "ERROR", "gold_s3",
                                    f"gold call {call['tool']} fails S3: {err}"))

    for call in gold.get("tool_calls", []):
        gate = gates.get(call["tool"]) or {}
        args = call["arguments"]
        for key, expected in (gate.get("required") or {}).items():
            if args.get(key) != expected:
                findings.append(finding(cid, "ERROR", "gate_required_mismatch",
                                        f"gate requires {call['tool']}.{key}={expected!r} but the gold call passes {args.get(key)!r} — the grader compares with != and would fail the gold flow"))
            if isinstance(expected, list) and len(expected) > 1:
                findings.append(finding(cid, "WARN", "gate_order_sensitive",
                                        f"gate pins {call['tool']}.{key}={expected!r} as required — an order-sensitive list gate; prefer optional_ok.allowed"))
        for value in args.values():
            if _contains_forbidden_id(value, gate.get("forbidden_ids") or []):
                findings.append(finding(cid, "ERROR", "gate_forbidden",
                                        f"gold call {call['tool']} passes {value!r} which contains a forbidden_id — the grader would fail the gold flow"))
        for key, constraint in (gate.get("optional_ok") or {}).items():
            if key in args and isinstance(constraint, dict) and "allowed" in constraint \
                    and args[key] not in constraint["allowed"]:
                findings.append(finding(cid, "ERROR", "gate_optional_ok",
                                        f"gold call {call['tool']}.{key}={args[key]!r} is not in the gate's allowed list"))

    order = {}
    for i, call in enumerate(gold.get("tool_calls", [])):
        order.setdefault(call["tool"], i)  # first occurrence, like the grader's first_idx
    for pair in gold.get("dependencies") or []:
        if len(pair) != 2:
            findings.append(finding(cid, "WARN", "dependencies_shape",
                                    f"dependency pair {pair!r} is not [A, B]"))
            continue
        a, b = pair
        if a in order and b in order and order[a] > order[b]:
            findings.append(finding(cid, "WARN", "dep_order",
                                    f"gold.tool_calls lists {b} before its prerequisite {a} — A4 is enforced on the live transcript at runtime, so the gold flow itself violates it"))

    if "S2" in case["grading"].get("invariants_active", []):
        texts = [t.get("content") or "" for t in case.get("user_turns", [])]
        for tool in sorted({call["tool"] for call in gold.get("tool_calls", [])}):
            raw = raw_by_name.get(tool) or {}
            if raw.get("action_type") != "write" and not raw.get("requires_confirmation"):
                continue
            if any(_AFFIRMATION_RE.search(t) for t in texts):
                continue
            weak_matches = [t for t in texts if WEAK_AFFIRM_RE.search(t)]
            if weak_matches:
                weak = weak_matches[-1]
                findings.append(finding(cid, "WARN", "s2_weak_affirmation",
                                        f"write tool '{tool}' has no user turn matching grader._AFFIRMATION_RE; {weak[:60]!r} only loosely affirms — the runtime falls back to the confirmation judge"))
            else:
                findings.append(finding(cid, "ERROR", "s2_affirmation",
                                        f"write tool '{tool}' has no affirming user turn at all — the deterministic S2 fast path can never pass"))

    judge_gate = case["grading"].get("judge_gate") or {}
    if case.get("target_behavior") != judge_gate.get("behavior_class"):
        findings.append(finding(cid, "WARN", "behavior_class_mismatch",
                                f"target_behavior={case.get('target_behavior')!r} but judge_gate.behavior_class={judge_gate.get('behavior_class')!r}"))

    check_mocks(case, mocks, findings)
    check_id_provenance(case, raw_by_name, findings, stats)


def check_mocks(case, mocks, findings):
    """Check mock structure and visible payloads."""
    cid = case["case_id"]
    for tool, mock in mocks.items():
        if not isinstance(mock, dict):
            findings.append(finding(cid, "ERROR", "mock_kind",
                                    f"mock['{tool}'] is not a dict"))
            continue
        kind = mock.get("kind")
        if kind not in MOCK_KINDS:
            findings.append(finding(cid, "ERROR", "mock_kind",
                                    f"mock['{tool}'].kind={kind!r} not in {sorted(MOCK_KINDS)} — the executor raises MockExecutionError"))
            continue
        if kind == "outcome_merged":
            merge = mock.get("merge")
            if not isinstance(merge, dict):
                findings.append(finding(cid, "ERROR", "mock_merge",
                                        f"mock['{tool}'] is outcome_merged but has no merge config"))
            else:
                for field in ("state_from", "match_on", "field"):
                    if not merge.get(field):
                        findings.append(finding(cid, "ERROR", "mock_merge",
                                                f"mock['{tool}'].merge.{field} missing"))
                src = merge.get("state_from")
                if src:
                    if src not in mocks:
                        findings.append(finding(cid, "ERROR", "mock_state_from",
                                                f"mock['{tool}'].merge.state_from='{src}' names no mock in this case"))
                    elif (mocks.get(src) or {}).get("kind") != "record_set_filtered":
                        findings.append(finding(cid, "ERROR", "mock_state_from",
                                                f"mock['{tool}'].merge.state_from='{src}' is not a record_set_filtered mock — the executor raises"))
        reject = mock.get("reject_when")
        if reject is not None:
            if not isinstance(reject, dict) or not isinstance(reject.get("payload_has_all_of"), list) \
                    or not isinstance(reject.get("output"), dict):
                findings.append(finding(cid, "ERROR", "mock_reject_when",
                                        f"mock['{tool}'].reject_when must carry payload_has_all_of (list) + output (dict)"))
            else:
                scan_payload(cid, tool, "reject_when.output", reject["output"], findings)
        for zone in ("records", "output", "static_output"):
            payload = mock.get(zone)
            if isinstance(payload, (dict, list)):
                scan_payload(cid, tool, zone, payload, findings)


def scan_payload(cid, tool, zone, payload, findings):
    """Report suspicious values in visible mock payloads."""
    def walk(obj, path):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(key, str) and key.startswith("_"):
                    findings.append(finding(cid, "INFO", "mock_underscore_key",
                                            f"mock.{tool}.{zone}{path}.{key} is an _-prefixed key inside a candidate-visible payload — sent verbatim to the candidate"))
                walk(value, f"{path}.{key}")
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                walk(item, f"{path}[{i}]")
        else:
            if obj is None:
                findings.append(finding(cid, "INFO", "mock_null_token",
                                        f"mock.{tool}.{zone}{path} is null — a bare JSON null the model will see (legal for bad-response fixtures, but confirm it is deliberate)"))
            elif isinstance(obj, str) and obj.strip().lower() in ("null", "nan"):
                findings.append(finding(cid, "INFO", "mock_null_token",
                                        f"mock.{tool}.{zone}{path}={obj!r} — a literal 'null'/'NaN' string in candidate-visible output"))

    walk(payload, "")


def topological_order(gold):
    """Return gold tools in dependency order."""
    names = [c["tool"] for c in gold.get("tool_calls", [])]
    succ = {}
    for pair in gold.get("dependencies") or []:
        if len(pair) == 2 and pair[0] in names and pair[1] in names:
            succ.setdefault(pair[0], set()).add(pair[1])
    visited, stack = set(), []

    def visit(name):
        if name in visited:
            return
        visited.add(name)
        for nxt in sorted(succ.get(name, ())):
            visit(nxt)
        stack.append(name)

    for name in names:
        visit(name)
    stack.reverse()
    return stack


def check_id_provenance(case, raw_by_name, findings, stats):
    """Check whether gold-call IDs can be reached from prior context."""
    cid = case["case_id"]
    gold = case["gold"]
    calls = gold.get("tool_calls") or []
    if not calls:
        return

    pool = set()
    collect_ids(case.get("setup", {}).get("login_context") or {}, pool)
    for msg in case.get("setup", {}).get("prior_messages") or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, str):
            continue
        pool.update(TEXT_ID_RE.findall(content))
        try:
            collect_ids(json.loads(content), pool)
        except (ValueError, TypeError):
            pass
    for turn in case.get("user_turns", []):
        pool.update(TEXT_ID_RE.findall(turn.get("content") or ""))
    static_pool = set(pool)

    state = {}
    simulation_ok = True
    sim_failure_note = None
    for tool in topological_order(gold):
        call = next(c for c in calls if c["tool"] == tool)
        args = call["arguments"]
        for key, value in args.items():
            if not (_ID_KEY_RE.search(str(key)) or key in EXTRA_ID_KEYS):
                continue
            for val in (value if isinstance(value, list) else [value]):
                if not isinstance(val, str):
                    continue
                stats["id_values"] += 1
                stats["_id_cases"].add(cid)
                if val in static_pool:
                    continue
                if val in pool:
                    stats["id_via_sim"] += 1
                    continue
                if simulation_ok:
                    stats["id_unreachable"] += 1
                    findings.append(finding(cid, "ERROR", "id_unreachable",
                                            f"gold {tool}.{key}={val!r} not obtainable from login_context, prior_messages, the user turns, or any earlier gold call's output — S1 would flag a model that guesses"))
                else:
                    findings.append(finding(cid, "WARN", "id_maybe_unreachable",
                                            f"gold {tool}.{key}={val!r} was not reachable, but the mock simulation degraded ({sim_failure_note}); verify by hand"))
        try:
            output = mock_executor.execute(tool, args, case, raw_by_name, state=state)
            collect_ids(json.loads(output), pool)
        except Exception as exc:
            simulation_ok = False
            sim_failure_note = f"{type(exc).__name__}: {exc}"[:80]
            stats["sim_failures"] += 1
            findings.append(finding(cid, "WARN", "id_sim_failure",
                                    f"simulating mock output for gold call {tool} failed ({sim_failure_note}) — later ID reachability is unverifiable"))


def check_case(case, defs, findings, stats):
    """Run all checks for one case."""
    check_structure(case, findings)
    if not isinstance(case.get("gold"), dict) or not isinstance(case.get("grading"), dict) \
            or not isinstance(case.get("setup"), dict):
        return
    check_consistency(case, defs, findings, stats)


def render_report(total_cases, findings, stats):
    """Render the lint report."""
    by_sev = {"ERROR": [], "WARN": [], "INFO": []}
    for f in findings:
        by_sev[f["severity"]].append(f)

    lines = [
        "# Case Bank Lint Report",
        "",
        f"Generated by `indicbankbench/scripts/case_lint.py` — {total_cases} cases scanned "
        f"under `indicbankbench/case_bank/` (excluding `_retired/`).",
        "",
        "## Summary",
        "",
        "| Severity | Hits |",
        "|---|---|",
    ]
    for sev in ("ERROR", "WARN", "INFO"):
        lines.append(f"| {sev} | {len(by_sev[sev])} |")
    lines.append("")
    lines.append("| Check | Severity | Hits |")
    lines.append("|---|---|---|")
    for (sev, check), count in sorted(Counter((f["severity"], f["check"]) for f in findings).items()):
        lines.append(f"| {check} | {sev} | {count} |")
    lines += [
        "",
        f"ID provenance: {stats['id_values']} ID-shaped gold argument values checked across "
        f"{len(stats['_id_cases'])} cases; {stats['id_via_sim']} reachable only via simulated "
        f"earlier-call outputs; {stats['id_unreachable']} unreachable; "
        f"{stats['sim_failures']} simulation failures.",
        "",
    ]

    for sev in ("ERROR", "WARN", "INFO"):
        lines += [f"## {sev}", ""]
        if not by_sev[sev]:
            lines += ["_none_", ""]
            continue
        for f in by_sev[sev]:
            lines.append(f"- `{f['case_id']}` — **{f['check']}**: {f['message']}")
        lines.append("")

    lines += [
        "## Notes",
        "",
        "- `gate_order_sensitive` (acct.stop_cheque_payment.bad_response.001) is a deliberate "
        "single-occurrence pattern; the WARN stays so future copies are caught.",
        "- The three `s2_weak_affirmation` cases (\"Alright, book it.\" family) rely on the "
        "dedicated confirmation-judge fallback in grader.py — defensible, hence WARN not ERROR.",
        "- Customer-stated references (e.g. \"my request REQ-888\") count as reachable IDs.",
        "",
    ]
    return "\n".join(lines)


def main():
    cases = collect_cases()
    stats = {"id_values": 0, "_id_cases": set(), "id_via_sim": 0, "id_unreachable": 0,
             "sim_failures": 0}

    _, defs = harness_tools.load_tool_definitions()

    findings = []
    crashed = 0
    for path, cid, case in cases:
        if case is None:
            findings.append(finding(path.name, "ERROR", "case_parse",
                                    f"case file failed to parse: {path}"))
            crashed += 1
            continue
        try:
            check_case(case, defs, findings, stats)
        except Exception as exc:  # noqa: BLE001 — a lint crash must not hide the rest of the bank
            snippet = traceback.format_exc().strip().splitlines()
            findings.append(finding(cid, "ERROR", "case_crash",
                                    f"lint crashed on this case: {type(exc).__name__}: {exc} ({snippet[-1] if snippet else ''})"))
            crashed += 1

    ids = Counter(c[1] for c in cases if c[1] is not None)
    for cid, count in ids.items():
        if count > 1:
            findings.append(finding(cid, "ERROR", "duplicate_case_id",
                                    f"case_id {cid!r} appears {count} times across the bank"))

    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["case_id"], f["check"], f["message"]))
    counts = Counter(f["severity"] for f in findings)
    total = sum(counts.values())

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(render_report(len(cases), findings, stats))

    print(f"case_lint: scanned {len(cases)} cases (per-case lint crashes: {crashed})")
    print(f"ERROR {counts['ERROR']} | WARN {counts['WARN']} | INFO {counts['INFO']} ({total} findings)")
    for f in findings:
        if f["severity"] == "ERROR":
            print(f"  [ERROR] {f['case_id']} — {f['check']}: {f['message']}")
    print(f"report written: {REPORT_PATH}")
    return 1 if counts["ERROR"] else 0


if __name__ == "__main__":
    sys.exit(main())
