# Metrics

IndicBankBench measures both whether a model succeeds and whether it does so consistently. Each
case is run **N** times. The report summarizes results across those repeated trials; see
[`ARCHITECTURE.md`](ARCHITECTURE.md) for the scoring model.

## Core results

For a case run **N** times, let *k* be the number of passing trials.

| Metric | Meaning | How to read it |
|---|---|---|
| **pass^N** | The case passed every trial (*k* = *N*). | Headline reliability measure. A high value means the model succeeds consistently. |
| **pass@N** | The case passed at least once (*k* > 0). | Shows whether the model can succeed, even if it is inconsistent. |
| **mean** | The share of all individual trials that passed. | Expected result from one run under the same settings. |
| **Inconsistent** | The case passed some, but not all, trials (0 < *k* < *N*). | Direct signal of inconsistent behavior. |
| **Failed all** | The case passed no trials (*k* = 0). | Consistent failure on that case. |
| **avg@N** | The average of pass@*k* estimates for *k* from 1 to *N*. | A summary of how success changes as more attempts are allowed; omitted when *N* = 1. |

For a run with *n* cases:

```
pass^N  = count(k = N) / n
pass@N  = count(k > 0) / n
mean    = total passing trials / (N × n)
```

The gap between **pass@N** and **pass^N** matters. If pass@N is much higher, the model can solve
the cases but does not do so reliably enough to pass every trial.

## Breakdowns

The report also shows **pass^N** by banking domain and evaluation axis. These use the same strict
definition as the headline: a case counts only if it passed every trial. Use them to locate where
a model is reliable and where it is not.

## Quality and failure reasons

Quality measures grounding, completeness, tone, and—where relevant—the quality of clarification
or refusal. They are reported separately from pass/fail and never turn a failed case into a pass.
Compare a quality mean with its **n scored** value, because only transcripts that reach the
response-stage judge receive quality scores.

When a failed trial produces a score, it reports one first blocking reason. This is useful for
locating where evaluation stopped, but it is not a complete defect count: a case with several
problems reports only the earliest one.

Errors or missing trial results count as non-passes in reliability metrics. Resolve incomplete
runs before drawing conclusions from a report.

## Comparing runs fairly

Compare runs only when they use the same case bank, number of passes, temperature, and judge
setup. A run scoped to a subset of cases represents that subset only.

With **N = 1**, pass^N, pass@N, and mean are the same number, and there can be no inconsistent cases.
Use multiple passes when consistency is part of the question.
