# Contributing to IndicBankBench

Thanks for your interest. This guide covers how to report issues, add test cases, and submit
changes.

## Ways to contribute

- **Bug reports** — open an issue with the `case_id`, the model/label, and the relevant
  `transcript.json` if you have one.
- **New cases** — the main way the benchmark grows (see below).
- **Harness changes** — fixes and features for the grader, runner, judge, or analysis.

## Setup

```bash
pip install -e .
```

Copy `.env.example` to `.env` and point it at two OpenAI-compatible endpoints (candidate and
judge). See `HOW_TO_RUN.md` for the full setup and run workflow.

## Tests and validation

```bash
python -m unittest discover -s indicbankbench/tests
python indicbankbench/scripts/case_lint.py
```

The tests use only the stdlib plus the runtime dependencies in `pyproject.toml`.

## Adding a case

Read `indicbankbench/CASE_DRAFTING_KIT.md` (how to author) and `indicbankbench/CONVENTIONS.md` (fixture and
naming rules) first, then:

1. Add the case JSON at `indicbankbench/case_bank/<domain>/<tool>/<axis>.NNN.json`.
2. Run `python indicbankbench/scripts/case_lint.py` — no new ERRORs.
3. Run `python -m unittest indicbankbench.tests.test_tool_contract` — green.

All case data is synthetic. Do not add real customer data, credentials, or personal information.

## Pull requests

1. Fork the repo and create a branch.
2. Keep changes focused — one case family or one harness fix per PR.
3. Make sure the tests pass.
4. Open a PR with a clear description of what changed and why.
