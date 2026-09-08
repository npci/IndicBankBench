"""Entry point tying the harness together.

Commands:

    python -m harness.cli run [--run-id ID] [--passes N] [--cases PATH ...]
    python -m harness.cli compare RUN_ID RUN_ID [...]

`run` evaluates one model (whichever the CANDIDATE_* env vars point at) over the case bank
`N` times and writes a report. `compare` puts finished runs side by side. Results land under
results/<run-id>/: run.json, cases/, summary.json, REPORT.md.
"""
import argparse
import datetime
import hashlib
import json
import logging
import re
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import analysis, grader, judge, prompt, runner, stub_client, tools
from .model_client import ModelClient
from tqdm import tqdm

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_ROOT = Path(__file__).resolve().parents[1]
RESULTS_ROOT = BENCH_ROOT / "results"
CASE_BANK = BENCH_ROOT / "case_bank"

# Serialises appends to a pass's errors.jsonl — several worker threads may error at once.
_ERROR_LOG_LOCK = threading.Lock()


def _load_case(path):
    with open(path) as f:
        return json.load(f)


def _lazy_client():
    """Getter that constructs a single ModelClient on first use — so an offline run never
    instantiates the real client."""
    holder = {}

    def get():
        if "client" not in holder:
            holder["client"] = ModelClient()
        return holder["client"]

    return get


def _make_candidate_client(model_mode, real_client_getter):
    if model_mode == "live":
        return real_client_getter(), "candidate_default"
    if model_mode.startswith("stub:"):
        script_name = model_mode.split(":", 1)[1]
        if script_name not in stub_client.NAMED_SCRIPTS:
            print(f"Unknown stub script '{script_name}'. Known: {list(stub_client.NAMED_SCRIPTS)}", file=sys.stderr)
            sys.exit(2)
        return stub_client.StubClient(stub_client.NAMED_SCRIPTS[script_name]), "stub"
    print(f"Unknown model mode '{model_mode}'", file=sys.stderr)
    sys.exit(2)


def _get_judge_result(judge_mode, case, transcript, real_client_getter):
    if judge_mode == "live":
        return judge.call_judge(case, transcript, real_client_getter(), "judge_default")
    return judge.heuristic_judge(case, transcript)


def _get_g2_judge_result(judge_mode, case, transcript, real_client_getter):
    """S2 (confirm-before-write): resolve the deterministic check's 'needs_judge' cases with
    a DEDICATED confirmation-judge call (not the main judge's axis verdict)."""
    if "S2" not in set(case["grading"].get("invariants_active", [])):
        return None
    pre = grader.deterministic_g2(case, transcript)
    if pre.get("status") != "needs_judge":
        return None
    write_calls = [d for d in pre.get("details", []) if isinstance(d, dict) and d.get("status") == "needs_judge"]
    if judge_mode == "live":
        return judge.call_confirmation_judge(case, transcript, write_calls, real_client_getter(), "judge_default")
    return judge.heuristic_confirmation_judge(case, transcript, write_calls)


def _run_one(case, model_mode, judge_mode, real_client_getter, out_dir, case_fingerprint=None,
             base_prompt_path=None):
    out_dir.mkdir(parents=True, exist_ok=True)
    candidate_client, candidate_profile = _make_candidate_client(model_mode, real_client_getter)
    transcript = runner.run_case(case, candidate_client, candidate_profile, base_prompt_path=base_prompt_path)
    judge_result = _get_judge_result(judge_mode, case, transcript, real_client_getter)
    g2_judge_result = _get_g2_judge_result(judge_mode, case, transcript, real_client_getter)
    result = grader.grade(case, transcript, judge_result, g2_judge_result=g2_judge_result)
    if case_fingerprint:
        result["_fingerprint"] = case_fingerprint
    (out_dir / "transcript.json").write_text(json.dumps(transcript, indent=2))
    (out_dir / "score.json").write_text(json.dumps(result, indent=2))
    return result


RETIRED_DIR = "_retired"


def _discover_case_files(paths):
    """Case files under a directory, skipping `_retired/`."""
    files = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            files.extend(sorted(f for f in p.rglob("*.json") if RETIRED_DIR not in f.parts))
        else:
            files.append(p)
    return files


def preflight_tool_names(case_files):
    """Every `tools_exposed` name must exist in the contract.

    `resolve_tools_exposed` raises on the first unknown name, which is correct but reports one
    case at a time — when a contract drops a tool, that means discovering the blast radius by
    repeatedly re-running. This reports every offender at once, before any model call is made.

    Returns {tool_name: [case paths]}, empty when the bank is clean.
    """
    _, tool_defs_by_name = tools.load_tool_definitions()
    offenders = {}
    for path in case_files:
        try:
            case = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            continue
        for entry in case.get("tools_exposed", []):
            if isinstance(entry, str) and entry not in tool_defs_by_name:
                offenders.setdefault(entry, []).append(str(path))
    return offenders


def _hash_files(paths):
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(Path(p).read_bytes())
    return h.hexdigest()[:12]


def _run_config():
    """The candidate/judge configuration this run will use — resolved the same way the
    client resolves it, so run.json records what was actually sent."""
    client = ModelClient()
    cand = client._profile("candidate_default")  # noqa: SLF001 - provenance needs the resolved values
    judge_p = client._profile("judge_default")  # noqa: SLF001
    return {
        "candidate_model": cand["model"],
        "candidate_base_url": cand["base_url"],
        "temperature": cand["temperature"],
        "max_tokens": cand["max_tokens"],
        "seed": cand.get("seed"),
        "judge_model": judge_p["model"],
        "judge_base_url": judge_p["base_url"],
    }


def _case_fingerprint(case_path, cfg):
    """Identifies the inputs that produced a cached result. If any of these changed, the
    cached score is stale and must be recomputed rather than silently reused.

    Includes the system-prompt content hash (cfg["prompt_hash"]) so a prompt edit — e.g.
    switching between the v1/v2 ablation files — invalidates the cache instead of silently
    reusing a score graded under the other prompt. Older cached results (written before this
    field existed) have no prompt_hash to match against and are correctly treated as stale.
    """
    return hashlib.sha256(
        b"|".join([
            Path(case_path).read_bytes(),
            str(cfg["candidate_model"]).encode(),
            str(cfg["temperature"]).encode(),
            str(cfg.get("seed", "")).encode(),
            str(cfg["judge_model"]).encode(),
            str(cfg.get("prompt_hash", "")).encode(),
            str(cfg.get("candidate_reasoning", "")).encode(),
        ])
    ).hexdigest()[:16]


def _run_case_for_pass(case_path, get_client, pass_dir, cfg, fresh, base_prompt_path):
    """Run one case for one pass. Never raises — a crashing case becomes an ERROR so the
    run continues; it writes no score.json and so counts as a non-pass. The error reason is
    appended (one JSON object per line) to <pass_dir>/errors.jsonl, so a run's failures are
    debuggable after the fact without hunting through 799 case directories."""
    case = None
    try:
        case = _load_case(case_path)
        case_id_raw = case["case_id"]
        if not re.match(r'^[a-z0-9._-]+$', case_id_raw):
            raise ValueError(f"Unsafe case_id rejected: {case_id_raw!r}")
        case_dir = pass_dir / case_id_raw
        fp = _case_fingerprint(case_path, cfg)
        score_file = case_dir / "score.json"
        if not fresh and score_file.exists():
            try:
                cached = json.loads(score_file.read_text())
                if cached.get("_fingerprint") == fp:
                    return cached, "cached"
            except json.JSONDecodeError:
                pass  # unreadable cache -> just re-run it
        return _run_one(case, "live", "live", get_client, case_dir, case_fingerprint=fp,
                         base_prompt_path=base_prompt_path), "ran"
    except Exception as e:  # noqa: BLE001 - surface and continue
        case_id = case.get("case_id") if isinstance(case, dict) else Path(case_path).stem
        tb = traceback.format_exc()
        logging.debug("Case %s failed:\n%s", case_id, tb)
        entry = {
            "case_id": case_id,
            "case_path": str(case_path),
            "error": str(e),
        }
        with _ERROR_LOG_LOCK:
            with open(pass_dir / "errors.jsonl", "a") as f:
                f.write(json.dumps(entry) + "\n")
            # Persist the partial transcript when one survived (e.g. a tool-call loop) so the
            # failure is diagnosable — otherwise what the model did is lost with the exception.
            partial = getattr(e, "transcript", None)
            if partial:
                err_dir = pass_dir / case_id
                err_dir.mkdir(parents=True, exist_ok=True)
                (err_dir / "transcript.json").write_text(json.dumps(partial, indent=2))
        return {"case_id": str(case_path), "verdict": "ERROR", "fail_reason": str(e), "quality_score": None}, "error"


def cmd_run(args):
    case_paths = args.cases or [CASE_BANK]
    case_files = _discover_case_files(case_paths)
    if not case_files:
        print(f"No case files found under: {', '.join(str(p) for p in case_paths)}", file=sys.stderr)
        sys.exit(2)

    # Fail before spending a model call: an unresolvable tool name would otherwise abort mid-run.
    offenders = preflight_tool_names(case_files)
    if offenders:
        print("Preflight failed — tools_exposed names not present in tool_definitions.json:", file=sys.stderr)
        for tool, files in sorted(offenders.items()):
            print(f"  {tool}: {len(files)} case(s), e.g. {files[0]}", file=sys.stderr)
        print("Retire or repair these cases before running.", file=sys.stderr)
        sys.exit(2)

    cfg = _run_config()
    if not cfg["candidate_base_url"]:
        print("CANDIDATE_BASE_URL is not set — see HOW_TO_RUN.md §1.", file=sys.stderr)
        sys.exit(2)

    # Record both the prompt path (for a human) and a content hash (for the resume fingerprint).
    resolved_prompt_path = Path(args.prompt).resolve() if args.prompt else prompt.BASE_PROMPT_PATH
    if not resolved_prompt_path.exists():
        print(f"Prompt file not found: {resolved_prompt_path}", file=sys.stderr)
        sys.exit(2)
    try:
        cfg["prompt_path"] = str(resolved_prompt_path.relative_to(REPO_ROOT))
    except ValueError:
        cfg["prompt_path"] = str(resolved_prompt_path)
    cfg["prompt_hash"] = _hash_files([resolved_prompt_path])
    # Optional human label for the candidate; not derived from candidate_model, which is read
    # from the endpoint's /v1/models and can be wrong.
    if args.candidate_label:
        cfg["candidate_label"] = args.candidate_label

    started = datetime.datetime.now()
    run_id = args.run_id or f"{cfg['candidate_model'].replace('/', '_')}_{started:%Y%m%d_%H%M}"
    base = RESULTS_ROOT / run_id
    base.mkdir(parents=True, exist_ok=True)

    # Record the case ids up front. A case that crashes on every pass writes no score.json;
    # without this list it would drop out of the denominator and flatter the model, so the
    # expected set has to be pinned before the run rather than inferred from what survived.
    # Their axis/domain travels with them so a never-scored case can still be grouped in the
    # report instead of showing up as "unknown".
    case_ids, case_meta = [], {}
    for cp in case_files:
        try:
            c = _load_case(cp)
            case_ids.append(c["case_id"])
            case_meta[c["case_id"]] = {
                "axis": c.get("axis"), "domain": c.get("domain"), "tool": c.get("tool"),
            }
        except Exception:  # noqa: BLE001 - an unreadable case is reported when it runs
            case_ids.append(str(cp))

    meta = {
        "run_id": run_id,
        "passes": args.passes,
        "n_cases": len(case_files),
        "case_ids": case_ids,
        "case_meta": case_meta,
        "case_bank_hash": _hash_files(case_files),
        "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
        **cfg,
    }
    (base / "run.json").write_text(json.dumps(meta, indent=2))

    print(f"run {run_id}: {len(case_files)} cases x {args.passes} pass(es) "
          f"-> {cfg['candidate_model']} @ temp {cfg['temperature']}, prompt {cfg['prompt_path']} "
          f"({cfg['prompt_hash']})")

    get_client = _lazy_client()
    get_client()
    total_errors = 0
    for i in range(args.passes):
        pass_dir = base / "cases" / f"pass{i + 1}"
        pass_label = f"pass {i + 1}/{args.passes}"
        # Reset the error log for this pass so a resume doesn't pile new errors onto old ones:
        # the file should reflect only what errored on the most recent attempt of this pass.
        pass_dir.mkdir(parents=True, exist_ok=True)
        (pass_dir / "errors.jsonl").write_text("")
        counts = {"ran": 0, "cached": 0, "error": 0}
        pass_n = 0; fail_n = 0

        def _one(cp, _pd=pass_dir):
            return _run_case_for_pass(cp, get_client, _pd, cfg, args.fresh, resolved_prompt_path)

        pbar = tqdm(total=len(case_files), unit="case", desc=pass_label, ncols=100)
        def _record(out):
            nonlocal pass_n, fail_n
            result, how = out
            counts[how] += 1
            if how == "cached":
                pbar.set_postfix_str(f"P:{pass_n} F:{fail_n} E:{counts['error']} C:{counts['cached']}")
            elif how == "error":
                pbar.set_postfix_str(f"P:{pass_n} F:{fail_n} E:{counts['error']} C:{counts['cached']}")
            else:
                v = result.get('verdict', '?')
                if v == 'PASS': pass_n += 1
                else: fail_n += 1
                pbar.set_postfix_str(f"P:{pass_n} F:{fail_n} E:{counts['error']} C:{counts['cached']}")
            pbar.update(1)

        if args.concurrency == 1:
            for cp in case_files:
                _record(_one(cp))
        else:
            with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
                for fut in as_completed([ex.submit(_one, cp) for cp in case_files]):
                    _record(fut.result())
        pbar.close()
        total_errors += counts["error"]
        print(f"  {pass_label}: {counts['ran']} ran, {counts['cached']} cached, {counts['error']} errored")

    summary = analysis.summarize(analysis.load_run(run_id))
    (base / "summary.json").write_text(json.dumps(
        {k: v for k, v in summary.items() if k != "cases"}, indent=2, default=str))
    generated = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    report_text = analysis.render_run_report(summary, generated)
    (base / "REPORT.md").write_text(report_text)
    from rich.console import Console as _RC
    from rich.markdown import Markdown as _RM
    _RC().print(_RM(report_text))

    n = summary["passes"]
    label = "pass rate" if n == 1 else f"strict pass={n}"
    print("\n" + "=" * 64)

    # A run whose endpoints died still reaches this point with every case ERRORed, and a
    # bare "0/0 (0%)" reads like a finished run. Refuse to present the summary as though
    # it were complete: an incomplete run is a broken run, not a bad score.
    missing = len(case_files) - summary["n_cases"]
    if total_errors or missing > 0:
        print(f"!! INCOMPLETE RUN — {total_errors} case-run(s) errored"
              + (f", {missing} case(s) produced no score" if missing > 0 else ""))
        print("!! Common cause: the candidate or judge endpoint went down mid-run.")
        print("!! Fix the endpoint, then re-run the SAME --run-id to fill only what's missing.")
        if summary["n_cases"] == 0:
            print("!! No results at all — nothing to report.")
            return summary
        print("!! Numbers below cover only the cases that completed.\n")

    print(f"{label}: {summary['strict_pass']}/{summary['n_cases']} "
          f"({100 * summary['strict_rate']:.0f}%)")
    if n > 1:
        print(f"mean single-shot: {100 * summary['mean_single_shot']:.0f}%   "
              f"flaky: {summary['flaky']}   never passed: {summary['never']}")
    print(f"wrote {base}/REPORT.md")
    return summary


def cmd_compare(args):
    try:
        summaries, warnings = analysis.compare(args.run_ids)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)
    generated = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    md = analysis.render_comparison(summaries, warnings, generated)
    if args.out:
        Path(args.out).write_text(md)
        print(f"wrote {args.out}")
    else:
        print(md)
    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)


def cmd_smoke(args):
    """Offline harness self-test — scripted candidate + no-LLM judge, no network. For
    developing the harness itself, not for evaluating a model."""
    case = _load_case(args.case_path)
    out_dir = RESULTS_ROOT / "_smoke" / case["case_id"]
    result = _run_one(case, f"stub:{args.stub}", "heuristic", _lazy_client(), out_dir)
    print(f"{result['verdict']}  {result['case_id']}  {result.get('fail_reason') or ''}")
    print(f"wrote {out_dir}")
    return result


def main():
    parser = argparse.ArgumentParser(
        prog="python -m harness.cli",
        description="Banking assistant eval harness. `run` evaluates one model; `compare` puts runs side by side.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "run",
        help="evaluate one model over the case bank N times",
        description=(
            "Evaluate the model at CANDIDATE_BASE_URL/CANDIDATE_MODEL over the case bank.\n\n"
            "Scoring is STRICT: with --passes N a case counts only if it passes all N times.\n"
            "Temperature is not a flag — set it in config/models.yaml; it is recorded in run.json.\n"
            "Re-running the same --run-id resumes automatically, reusing only results whose case\n"
            "file and model/temperature are unchanged."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--run-id", default=None,
                   help="name for this run (default: <model>_<timestamp>); results land in results/<run-id>/")
    p.add_argument("--passes", type=int, default=3, metavar="N",
                   help="how many times to run every case (default: 3). 1 = plain pass rate, no consistency signal")
    p.add_argument("--cases", nargs="+", default=None, metavar="PATH",
                   help="case files or directories to run (default: the whole case bank)")
    p.add_argument("--concurrency", type=int, default=4, metavar="N",
                   help="cases in parallel (default: 4). All models share one judge, so keep this modest")
    p.add_argument("--fresh", action="store_true",
                   help="ignore cached results and re-run every case")
    p.add_argument("--prompt", default=None, metavar="PATH",
                   help="system-prompt file to render (default: prompts/base_system_prompt.txt). "
                        "Recorded as prompt_path + a content hash in run.json, and folded into "
                        "the resume fingerprint, so a prompt change invalidates the cache.")
    p.add_argument("--candidate-label", default=None, metavar="LABEL",
                   help="explicit human label for the candidate, recorded in run.json as "
                        "candidate_label. Use this when the endpoint's /v1/models id is "
                        "ambiguous or known-wrong (e.g. a 31b deployment serving under the id "
                        "'gemma-e4b') — never rely on candidate_model alone in that case.")
    p.set_defaults(func=cmd_run)

    c = sub.add_parser(
        "compare",
        help="compare finished runs",
        description=("Put finished runs side by side. Each run's run.json carries its own configuration, "
                     "so mismatches (different temperature, passes, or case bank) are reported as warnings."),
    )
    c.add_argument("run_ids", nargs="+", metavar="RUN_ID", help="run ids under results/")
    c.add_argument("--out", default=None, help="write markdown here instead of stdout")
    c.set_defaults(func=cmd_compare)

    s = sub.add_parser("smoke", help="offline harness self-test (no network; for harness development)")
    s.add_argument("case_path")
    s.add_argument("--stub", required=True, help=f"scripted candidate; one of: {', '.join(stub_client.NAMED_SCRIPTS)}")
    s.set_defaults(func=cmd_smoke)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
