"""Aggregate completed evaluation runs."""
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_ROOT = Path(__file__).resolve().parents[1]
RESULTS_ROOT = BENCH_ROOT / "results"

# Run settings that affect comparability.
COMPARABLE_FIELDS = ("passes", "temperature", "case_bank_hash", "judge_model")


def run_dir(run_id, results_root=None):
    return Path(results_root or RESULTS_ROOT) / run_id


def load_run(run_id, results_root=None):
    """Read run metadata and per-case scores."""
    base = run_dir(run_id, results_root)
    meta_path = base / "run.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"no run.json under {base} — not a run directory")
    meta = json.loads(meta_path.read_text())
    n = int(meta.get("passes", 1))

    scores = defaultdict(lambda: [None] * n)
    # Preserve never-scored cases in the denominator.
    for case_id in meta.get("case_ids") or []:
        _ = scores[case_id]
    for i in range(n):
        pass_dir = base / "cases" / f"pass{i + 1}"
        if not pass_dir.is_dir():
            continue
        for case_dir in sorted(pass_dir.iterdir()):
            score_file = case_dir / "score.json"
            if not score_file.is_file():
                continue
            try:
                scores[case_dir.name][i] = json.loads(score_file.read_text())
            except json.JSONDecodeError:
                scores[case_dir.name][i] = None
    return {"run_id": run_id, "meta": meta, "passes": n, "scores": dict(scores)}


def _passed(score):
    return bool(score) and score.get("verdict") == "PASS"


def per_case(run):
    """Summarize pass counts and metadata for each case."""
    out = {}
    fallback = (run.get("meta") or {}).get("case_meta") or {}
    for case_id, samples in run["scores"].items():
        k = sum(1 for s in samples if _passed(s))
        n = len(samples)
        first = next((s for s in samples if s), None) or fallback.get(case_id) or {}
        out[case_id] = {
            "k": k,
            "n": n,
            "strict": k == n and n > 0,
            "inconsistent": 0 < k < n,
            "axis": first.get("axis") or "unknown",
            "domain": first.get("domain") or "unknown",
            "tool": first.get("tool") or "unknown",
            "fail_reasons": sorted({s.get("fail_reason") for s in samples if s and s.get("fail_reason")}),
        }
    return out


def quality_breakdown(run):
    """Aggregate judge sub-scores by metric."""
    totals = defaultdict(lambda: [0.0, 0])  # metric -> [sum, count]
    for samples in run["scores"].values():
        for s in samples:
            if not s:
                continue
            judge = s.get("judge_result") or {}
            for metric, value in (judge.get("sub_scores") or {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    totals[metric][0] += value
                    totals[metric][1] += 1
    return [
        {"metric": m, "mean": totals[m][0] / totals[m][1], "n": totals[m][1]}
        for m in sorted(totals)
    ]


def _rate_by(cases, key):
    """Return strict pass rates grouped by a case field."""
    groups = defaultdict(lambda: {"strict": 0, "total": 0})
    for c in cases.values():
        g = groups[c[key]]
        g["total"] += 1
        g["strict"] += int(c["strict"])
    rows = []
    for name, g in groups.items():
        rows.append({key: name, **g, "rate": g["strict"] / g["total"] if g["total"] else 0.0})
    return sorted(rows, key=lambda r: r[key])


def _avg_at_k(cases, n_passes):
    """Compute the average pass@k estimate."""
    if n_passes <= 1:
        return {"pass_at_k": {}, "mean": 0}
    total = len(cases)
    if total == 0:
        return {"pass_at_k": {}, "mean": 0}
    pass_at_k = {}
    for k in range(1, n_passes + 1):
        denom = math.comb(n_passes, k)
        s = 0.0
        for c in cases.values():
            c_i = c["k"]
            misses = n_passes - c_i
            if misses < k:
                s += 1.0
            else:
                s += 1.0 - math.comb(misses, k) / denom
        pass_at_k[k] = round(s / total * 100)
    mean = round(sum(pass_at_k.values()) / n_passes)
    return {"pass_at_k": pass_at_k, "mean": mean}


def summarize(run):
    """Headline metrics for one run."""
    cases = per_case(run)
    n_cases = len(cases)
    n = run["passes"]
    strict = sum(1 for c in cases.values() if c["strict"])
    inconsistent = sum(1 for c in cases.values() if c["inconsistent"])
    failed_all = sum(1 for c in cases.values() if c["k"] == 0)
    total_pass = sum(c["k"] for c in cases.values())
    dist = Counter(c["k"] for c in cases.values())

    fail_reasons = Counter(
        s.get("fail_reason") for samples in run["scores"].values() for s in samples
        if s and s.get("verdict") != "PASS" and s.get("fail_reason")
    )

    return {
        "run_id": run["run_id"],
        "meta": run["meta"],
        "passes": n,
        "n_cases": n_cases,
        "fail_reasons": dict(fail_reasons.most_common()),
        "strict_pass": strict,
        "strict_rate": strict / n_cases if n_cases else 0.0,
        "any_pass": n_cases - failed_all,
        "any_rate": (n_cases - failed_all) / n_cases if n_cases else 0.0,
        "mean_single_shot": total_pass / (n * n_cases) if n_cases and n else 0.0,
        "inconsistent": inconsistent,
        "failed_all": failed_all,
        "k_distribution": {k: dist.get(k, 0) for k in range(n + 1)},
        "quality": quality_breakdown(run),
        "by_axis": _rate_by(cases, "axis"),
        "by_domain": _rate_by(cases, "domain"),
        "cases": cases,
        "avg_at_k": _avg_at_k(cases, n),
    }


def compare(run_ids, results_root=None):
    """Summarize runs and return comparability warnings."""
    summaries = [summarize(load_run(rid, results_root)) for rid in run_ids]
    warnings = []
    for field in COMPARABLE_FIELDS:
        seen = {}
        for s in summaries:
            seen.setdefault(s["meta"].get(field), []).append(s["run_id"])
        if len(seen) > 1:
            detail = "; ".join(f"{v!r}: {', '.join(rids)}" for v, rids in seen.items())
            warnings.append(f"{field} differs across runs — {detail}")

    all_cases = {c for s in summaries for c in s["cases"]}
    common = set.intersection(*[set(s["cases"]) for s in summaries]) if summaries else set()
    if len(common) != len(all_cases):
        warnings.append(
            f"runs cover different case sets — {len(all_cases)} cases seen, {len(common)} common to all; "
            "totals below are over each run's own cases and are NOT directly comparable"
        )
    return summaries, warnings


def _pct(x):
    return f"{100 * x:.0f}%"


def render_run_report(summary, generated_at=None):
    """Render a Markdown report for one run."""
    m, n = summary["meta"], summary["passes"]
    L = [f"# Eval run — `{summary['run_id']}`", ""]
    L.append(f"- candidate: **{m.get('candidate_model', '?')}**  (temp {m.get('temperature', '?')})  `{m.get('candidate_base_url', '?')}`")
    L.append(f"- judge: {m.get('judge_model', '?')}")
    L.append(f"- passes: **{n}**   cases: **{summary['n_cases']}**   started: {m.get('started_at', '?')}")
    if generated_at:
        L.append(f"- report generated: {generated_at}")
    L.append("")

    L.append(f"## pass^{n} — **{summary['strict_pass']}/{summary['n_cases']} ({_pct(summary['strict_rate'])})**")
    L.append("")

    L.append("| metric | value |")
    L.append("|---|---|")
    L.append(f"| pass^{n} | **{summary['strict_pass']}/{summary['n_cases']} "
             f"({_pct(summary['strict_rate'])})** |")
    L.append(f"| pass@{n} | {summary['any_pass']}/{summary['n_cases']} "
             f"({_pct(summary['any_rate'])}) |")
    L.append(f"| mean | {_pct(summary['mean_single_shot'])} |")
    if summary.get("avg_at_k") and summary["avg_at_k"]["pass_at_k"]:
        L.append(f"| avg@{n} | {summary['avg_at_k']['mean']}% |")
    L.append("")

    for key, title in (("domain", "domain"), ("axis", "axis")):
        rows = summary[f"by_{key}"]
        if len(rows) <= 1 and key == "domain":
            continue
        L.append(f"## pass^{n} by {title}")
        L.append("")
        L.append(f"| {title} | pass^{n} | total | rate |")
        L.append("|---|---|---|---|")
        for r in rows:
            L.append(f"| {r[key]} | {r['strict']} | {r['total']} | {_pct(r['rate'])} |")
        L.append("")

    if summary["fail_reasons"]:
        L.append("## Where failures land  (counted over every pass)")
        L.append("")
        L.append("| gate | failures |")
        L.append("|---|---|")
        for reason, count in summary["fail_reasons"].items():
            L.append(f"| {reason} | {count} |")
        L.append("")
        L.append("> `A1` = one or more required tools were not called; `R` = the response "
                 "missed its semantic criterion. Inspect the transcript to determine the cause.")
        L.append("")

    if summary["quality"]:
        L.append("## Quality sub-scores by metric  (mean over scored instances)")
        L.append("")
        L.append("| metric | mean | n scored |")
        L.append("|---|---|---|")
        for row in summary["quality"]:
            L.append(f"| {row['metric']} | {row['mean']:.2f} | {row['n']} |")
        L.append("")
        L.append("> Judged 0.0 / 0.5 / 1.0 per case; `n` is scored instances, not cases. "
                 "Cases that stop before response-stage judging do not contribute quality scores.")
        L.append("")
    return "\n".join(L) + "\n"


def render_comparison(summaries, warnings, generated_at=None):
    """Render a Markdown comparison of completed runs."""
    L = ["# Run comparison", ""]
    if generated_at:
        L.append(f"Generated {generated_at}.")
        L.append("")
    if warnings:
        L.append("> [!WARNING]")
        L.append("> **These runs are not strictly comparable:**")
        for w in warnings:
            L.append(f"> - {w}")
        L.append("")

    n_values = {s["passes"] for s in summaries}
    n_label = f"pass={next(iter(n_values))}" if len(n_values) == 1 else "strict"

    header_n = f"pass^{n_label}"
    L.append(f"| run | model | temp | passes | cases | **{header_n}** | mean | pass@{n_label} | avg@{n_label} |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for s in sorted(summaries, key=lambda s: -s["strict_rate"]):
        m = s["meta"]
        ak = s.get("avg_at_k", {})
        avg_k_mean = ak.get("mean", "—") if ak else "—"
        if isinstance(avg_k_mean, (int, float)):
            avg_k_mean = f"{avg_k_mean}%"
        L.append(
            f"| `{s['run_id']}` | {m.get('candidate_model', '?')} | {m.get('temperature', '?')} | "
            f"{s['passes']} | {s['n_cases']} | **{s['strict_pass']}/{s['n_cases']} ({_pct(s['strict_rate'])})** | "
            f"{_pct(s['mean_single_shot'])} | {_pct(s['any_rate'])} | {avg_k_mean} |"
        )
    L.append("")

    domains = sorted({r["domain"] for s in summaries for r in s["by_domain"]})
    if len(domains) > 1:
        L.append("## Strict pass rate by domain")
        L.append("")
        L.append("| domain | " + " | ".join(s["run_id"] for s in summaries) + " |")
        L.append("|---|" + "---|" * len(summaries))
        for d in domains:
            cells = []
            for s in summaries:
                row = next((r for r in s["by_domain"] if r["domain"] == d), None)
                cells.append(f"{row['strict']}/{row['total']}" if row else "—")
            L.append(f"| {d} | " + " | ".join(cells) + " |")
        L.append("")
    return "\n".join(L) + "\n"
