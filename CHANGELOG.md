# Changelog

## 0.2.0

Grading and run fixes. The dataset is unchanged. Scores aren't directly comparable with 0.1, which
the README and the paper report; updated reference results will come with the next stable release.

### Fixed

- **Mock tools:** `from_date` and `to_date` filter records. Thanks to aniket04bit (#2).
- **Candidate options:** `CANDIDATE_EXTRA_BODY` no longer reaches the judge; `JUDGE_EXTRA_BODY` is
  its own setting. Thanks to AdvayV (#4).
- **S1 and S3:** account arguments that don't end in `_id` are checked, account numbers the
  customer typed are accepted, and whole-number floats count as integers. Reported by katepallewarprathmesh-sketch (#3); thanks to Mohit Raj (#5).
- **S2:** a write needs a plain yes to a message that asked for it; other turns go to the
  confirmation judge.
- **S3:** dates must be ISO dates, and arguments that aren't a JSON object fail.
- **A3:** the order of plain lists is ignored, declared defaults are filled in, and forbidden values
  are found at any depth.
- **Judge:** it sees the system prompt and the text sent with tool calls, and classifies the final
  reply only. In the 295 cases that accept more than one move, it is shown no expected class.
- **Runs:** provider errors sent as a reply are retried and then recorded as ERROR. An answer whose
  judging failed is judged on resume, a failed re-run leaves no old score, and reports say how many
  trials have no score. `JUDGE_BASE_URL` is required.

Tests are split into offline harness regression tests (`tests/core`) and explicit case-bank
validation (`tests/data`), under `indicbankbench/`. Enum-inference and naming heuristics, corpus-size quotas, and field-order
assertions were removed. See `CONTRIBUTING.md` for commands.
