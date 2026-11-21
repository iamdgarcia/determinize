# determinize

Compile what your coding agent actually does into deterministic flows. The
point is cost: everything in an agent trajectory that can be deterministic
should be deterministic, because the model is the most expensive component in
the loop and it is currently being paid to re-type TLS exports, re-read the
same file, and retry commands that hang.

`determinize` reads session transcripts (Claude Code JSONL, pi JSONL), finds
the steps that did not need a language model, and emits the deterministic fix
for each one - scripts, hooks, CLAUDE.md rules - with per-flow token-waste
estimates.

```
$ python determinize.py ~/.claude/projects/<proj>/session.jsonl --out report --scaffold
[ok] session.jsonl: 4 flows, ~86,009 tokens waste
[ok] report -> report/report.md
```

## What it detects

| flow | what it sees | deterministic fix |
|------|--------------|-------------------|
| `env_preamble` | TLS/proxy/PYTHONPATH exports re-typed inside shell calls | `flows/env.sh` + source-once hook |
| `retry_loop` | a command errors and the same command runs again within 3 calls | `flows/guarded.sh`: timeout + one retry + the fix baked in |
| `context_dump` | single tool results pushing 8k+ chars into context | excerpt wrapper with a byte budget |
| `repeat_read` | the same file read again with no edit in between | `flows/ctx.sh` bundle / project map |
| `readonly_rerun` | identical read-only commands re-run with no edit between | capture the result once; script per repeated query |
| `verify_rerun` | test/lint invocations re-typed | single `flows/verify.sh` entrypoint |

## The numbers on one real session

Ran on one 8.3 MB factory session (1,798 tool calls, a full working day of
multi-repo maintenance):

| flow | occurrences | est. waste |
|------|-------------|------------|
| retry_loop | 5 | ~46,700 tokens |
| context_dump | 8 | ~27,100 tokens |
| repeat_read | 5 | ~10,900 tokens |
| env_preamble | 7 | ~1,400 tokens |

~86k tokens, about 4% of that session's context, spent on steps a shell script
could have owned. One flow was literally the same file being read four times in
a row. Estimates are `chars/4` per result plus 60 tokens of call overhead per
avoidable call - rough by design, directionally right.

## The law

Only determinize repetition. Never determinize judgment. The generated layer
must keep error handling: a wrapper returns the exit code and a log excerpt,
then hands the failure to the model. Judgment starts where the deterministic
layer ends.

## Install

Copy as a Claude Code skill:

```bash
git clone https://github.com/iamdgarcia/determinize
cp -r determinize/SKILL.md determinize/determinize.py ~/.claude/skills/determinize/
```

(or just run `determinize.py` directly against any transcript).

## Usage

```bash
python determinize.py <session.jsonl> [more.jsonl ...] [--out DIR] [--scaffold] [--selftest]
```

- multiple session files enable cross-session detection (an env prologue seen
  in several sessions is the highest-priority flow)
- `--scaffold` generates `flows/`, `HOOKS.md` and `CLAUDE-ADDENDUM.md`
- `--selftest` runs the built-in check
- see `examples/report.sample.md` for the report format on a synthetic session

## Why this shape

Three 2026 papers triangulate the same lesson from different sides: the signal
for the meta-decision (retry, abstain, escalate) lives in the execution trace,
not in model confidence - LoopRoute (arXiv 2611.25363) triggers an LLM takeover
from repeated state-action pairs, ADAPT (arXiv 2611.25754) shows a
context-blind threshold fails silently, MM-SystemOneBias (arXiv 2611.26557)
shows answers stay stable while probabilities slide. determinize is that idea
applied at the harness level: mine the trace, move what repeats out of the
model, keep the model for what cannot repeat.

## Limitations

- Transcript formats: Claude Code and pi JSONL supported; unknown formats fail
  loudly ("0 tool calls parsed") instead of guessing.
- Waste estimates are heuristic; treat them as ranking, not accounting.
- The scaffold is a starting point: paste your project's real values before
  trusting any generated wrapper.

MIT. Daniel García Peña - [iamdgarcia](https://github.com/iamdgarcia).
Built inside the [HF Products Factory](https://github.com/iamdgarcia).
