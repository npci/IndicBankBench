# Contributing to IndicBankBench

IndicBankBench has separate repositories for the evaluation harness and case bank. This guide
explains where to contribute each type of change.

## Contribution routes

- **Harness changes** — fixes or documentation for the grader, runner, judge, or analysis. Open a
  GitHub pull request against the code repository.
- **Case proposals or corrections** — open a GitHub Issue. Describe the scenario, expected
  behavior, and relevant `case_id`; do not add case data to a code pull request.
- **Questions and non-sensitive bug reports** — open a GitHub Issue. Include the `case_id` where
  applicable and a minimal, sanitised reproduction.

All case data is synthetic. Never include real customer data, credentials, personal information,
or an unsanitised transcript in an issue or pull request. If you find sensitive real-world data
in a fixture, follow the private reporting process in [`SECURITY.md`](SECURITY.md).

Maintainers review contributions for benchmark scope, consistency, and quality.

## Setup for harness work

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

Use an editable installation from a source checkout. The harness reads `tools.json`,
`harness_tool_spec.json`, `prompts/`, and `config/models.yaml` from that checkout.

Copy `.env.example` to `.env` and point it at two OpenAI-compatible endpoints (candidate and
judge). See `HOW_TO_RUN.md` for the full setup and run workflow.

## Tests

```bash
python -m unittest discover -s indicbankbench/tests
```

The tests use only the stdlib plus the runtime dependencies in `pyproject.toml`.

## Harness pull requests

1. Fork the repo and create a branch.
2. Keep changes focused — one harness fix or feature per pull request.
3. Make sure the tests pass.
4. Open a PR with a clear description of what changed and why.
