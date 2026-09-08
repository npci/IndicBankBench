# IndicBankBench

A benchmark for **tool-calling banking assistants**: 799 realistic tricky situations that
check whether an assistant does the right thing (answer, ask, or refuse), calls the right
tools with the right arguments, and never violates a small set of hard safety rules.

Each case is a scripted, multi-turn conversation against mocked bank tools. A deterministic
code grader checks safety and tool use, and an LLM judge grades the one irreducibly-semantic
question (was the response right for this situation). See `indicbankbench/ARCHITECTURE.md` for the
full model.

---

## Quickstart

```bash
# 1. Install the harness (editable) into your environment
pip install -e .

# 2. Point the harness at two OpenAI-compatible endpoints (see `.env.example`)
#    CANDIDATE_*  = the model under test (must be served with tool-calling enabled)
#    JUDGE_*      = the grader (use a different model family to avoid self-leniency)

# 3. Run the case bank
python -m harness.cli run --run-id my_model_v1

# 4. Compare two finished runs
python -m harness.cli compare my_model_v1 other_model_v1
```

Full setup and flag reference: `HOW_TO_RUN.md`.

---

## What's measured

- **Three correct moves** per message — *answer*, *ask* (for a genuinely missing detail),
  or *refuse* (out of bounds / nonexistent). Picking the wrong one is a failure.
- **Eleven difficulty axes** plus capability checks (identity, adversarial, financial-advice)
  and multi-tool agentic tasks — broken tool data, non-obvious wording, missing detail,
  contradicting info, context switching, wrong info, long context, irrelevant clutter,
  unseen tools, out-of-scope refusal, and more.
- **Hard safety rules** enforced on every case: no fabricated IDs/numbers, confirm before
  mutating, no full disclosure of private details, no claiming unconfirmed outcomes, no raw
  junk (`null`/`NaN`) in replies.

## How it's scored — S → A → R → Q

| Phase | Question | Decided by | Gates? |
|-------|----------|------------|--------|
| **S** | Safe? | deterministic code | yes — hard fail |
| **A** | Actions (right tools, args, order)? | deterministic code | yes |
| **R** | Response right for this situation? | LLM judge | yes |
| **Q** | Quality (grounded / complete / tone)? | LLM judge | no — ranks only |

**Pass** iff every S, A, and R gate holds; otherwise fail at the first phase that breaks.
The judge never decides safety or tool correctness, so a lenient judge can't loosen those gates.
Details: `indicbankbench/ARCHITECTURE.md` (model), `indicbankbench/METRICS.md` (exact formulas),
`indicbankbench/CODES.md` (per-code lookup).

## Repository layout

```
README.md            this file
pyproject.toml       packaging — installs the `harness` package + `indicbankbench` entry point
.env.example         endpoint configuration template (copy to `.env`)
indicbankbench/
  case_bank/         799 cases: case_bank/<domain>/<tool>/<axis>.NNN.json
  harness/           CLI, grader, mock executor, judge, model client
  config/models.yaml model/endpoint knobs (temperature, timeouts, max_tokens)
  prompts/           system prompts
  scripts/           case_lint validator + OpenRouter runners
  tests/             unit tests (stdlib only, ~0.3s)
  results/           run outputs (gitignored) — results/<run-id>/REPORT.md
  tool_definitions.json           authoring + harness contract
  new_tool_definitions_v1.json    model-facing tool schemas
```

## Development

```bash
python -m unittest discover -s indicbankbench/tests     # no deps beyond stdlib
python -m harness.cli smoke <case.json> --stub <script>   # scripted candidate, no-LLM judge
```

Before adding a case, read `indicbankbench/CASE_DRAFTING_KIT.md` and `indicbankbench/CONVENTIONS.md`,
then run `python indicbankbench/scripts/case_lint.py` to validate it.

## Documentation

| Doc | What it covers |
|-----|----------------|
| `indicbankbench/ARCHITECTURE.md` | What is measured and how scoring works (plain English + technical model) |
| `HOW_TO_RUN.md` | Setup, running, reading output, troubleshooting |
| `indicbankbench/METRICS.md` | Exact formula reference for every number in `REPORT.md` |
| `indicbankbench/CODES.md` | Per-code lookup (each S/A/R code's meaning and gates) |
| `indicbankbench/CONVENTIONS.md` | Authoring rules and conventions |
| `indicbankbench/CASE_DRAFTING_KIT.md` | How to author a new case |

## Contributing

See `CONTRIBUTING.md` for how to add cases and open pull requests. By participating you agree to
follow the `CODE_OF_CONDUCT.md`. Report security issues per `SECURITY.md`.

## License

Licensed under the Apache License, Version 2.0 — see `LICENSE`.

## Data

All case data is synthetic: personas, accounts, amounts, and tool outputs are fabricated for
testing and contain no real customer information.
