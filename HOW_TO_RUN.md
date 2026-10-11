# Run IndicBankBench

This guide covers setup, running a model, and reading its results. See the
[paper](https://arxiv.org/abs/2609.29167) for the benchmark design and the
[results reference](docs/RESULTS.md) for scoring terms.

## Setup

Clone and install the harness:

```bash
git clone https://github.com/npci/IndicBankBench.git
cd IndicBankBench
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Copy `.env.example` to `.env` and set `CANDIDATE_*` for the model under test and `JUDGE_*` for
the response judge. Both endpoints must be OpenAI-compatible; the candidate must support tool
calls. They may use the same provider and API key. Use `EMPTY` only for an unauthenticated local
endpoint.

```bash
cp .env.example .env
hf download NPCI/IndicBankBench --repo-type dataset --local-dir ./data
export INDICBANKBENCH_DATA=./data
```

The cases are distributed in the [dataset](https://huggingface.co/datasets/NPCI/IndicBankBench),
not this repository. Set temperature, token limits, and timeouts in
`indicbankbench/config/models.yaml`; temperature is recorded in each run. Provider-specific
request options can be set through `CANDIDATE_EXTRA_BODY` when needed. These options apply only
to model profiles whose names start with `candidate`.

## Run

```bash
python -m harness.cli run --run-id my_model_v1
```

By default, the harness runs the full case bank three times. A case passes the strict measure
only if all three trials pass. Useful flags include `--passes`, `--cases`, `--concurrency`,
`--prompt`, and `--fresh`; use `python -m harness.cli run --help` for their arguments.

Reusing a run ID resumes missing trials and reuses scored trials whose case file, candidate model
ID, temperature, seed, judge model ID, and prompt hash still match. Endpoint, token-limit, and
provider-specific reasoning changes are not included in that cache check. Use a new run ID or
`--fresh` for those changes.

## Read the results

Each run is saved under `indicbankbench/results/<run-id>/`:

```text
run.json
cases/pass<N>/<case_id>/{transcript.json, score.json}
summary.json
REPORT.md
```

Start with a transcript to inspect what the model did, then use its score to see the verdict and
first blocking code. The [results reference](docs/RESULTS.md) explains summary values and
per-case codes.

## Compare runs

Run each model separately, then compare the completed run IDs:

```bash
python -m harness.cli compare my_model_v1 other_model_v1 --out COMPARISON.md
```

Keep the case bank, passes, temperature, and judge setup aligned. `compare` warns about selected
configuration differences, but it does not establish that every endpoint setting was identical.
