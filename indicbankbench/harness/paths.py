"""Resolve the downloaded IndicBankBench case-bank location."""
import os
from pathlib import Path

BENCH_ROOT = Path(__file__).resolve().parents[1]
DATA_ENV = "INDICBANKBENCH_DATA"

FETCH_HINT = (
    f"No case bank found. It ships as a dataset, not in this repository:\n"
    f"  hf download NPCI/IndicBankBench --repo-type dataset --local-dir ./data\n"
    f"  export {DATA_ENV}=./data\n"
    f"Or point the harness at it directly with --cases <path>."
)


def _has_cases(p):
    """Return whether a directory contains case files."""
    return p.is_dir() and any(p.rglob("*.json"))


def case_bank_path(explicit=None):
    """Return the case-bank directory, if available."""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get(DATA_ENV)
    if env:
        candidates.append(Path(env))
    candidates.append(BENCH_ROOT / "case_bank")

    for candidate in candidates:
        nested = candidate / "case_bank"
        if _has_cases(nested):
            return nested
        if _has_cases(candidate):
            return candidate
    return None
