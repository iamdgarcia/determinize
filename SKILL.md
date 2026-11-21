---
name: determinize
description: Compile an agent session's trajectory into deterministic flows. Finds the repeated reads, re-typed environment exports, retry loops and read-only command re-runs in a Claude Code (or pi) session transcript, and generates the scripts/hooks/CLAUDE.md rules that take them away from the model. Use when harness costs look high, after finishing a long session, or when the user says "determinize", "why is this so expensive", or "stop re-reading files".
---

# determinize

Your harness pays model tokens for things that are not judgment: re-reading the
same file, re-typing TLS exports, retrying a command that hangs, re-running
read-only queries. Every one of those is a deterministic flow pretending to be a
model decision. This skill compiles them out of the trajectory.

## The law

Only determinize repetition. Never determinize judgment. A deterministic layer
that guesses gets in the way; a deterministic layer that replays exactly what
the trajectory already did is pure savings.

## Steps

1. **Locate the transcript(s).** Claude Code keeps sessions in
   `~/.claude/projects/<project-slug>/*.jsonl`. pi keeps them in
   `~/.pi/agent/sessions/<project>/*.jsonl`. Ask the user which session if
   ambiguous; default to the most recent large one.

2. **Run the compiler** (stdlib only, no install):

   ```bash
   python determinize.py <session.jsonl> [more.jsonl ...] --out determinize-report --scaffold
   ```

   Multiple files enable cross-session detection (an environment prologue seen
   in several sessions is the highest-priority flow).

3. **Read `determinize-report/report.md`.** Flows are ranked by estimated
   token waste. Estimates are `chars/4` per result plus 60 tokens of
   call overhead per avoidable call - rough by design, directionally right.

4. **Implement the flows, cheapest-first.** The `--scaffold` output gives you
   starting points; every one needs the project's real values pasted in:
   - `flows/env.sh`: move the exports from the evidence into it, then wire it
     once (hook or first instruction).
   - `flows/guarded.sh`: for each retry-loop flow, bake the actual fix
     (timeout value, env preamble) and replace the raw command everywhere.
   - `flows/verify.sh`: replace the re-typed test/lint invocations with one
     entrypoint.
   - `repeat_read` / `context_dump`: add the excerpt budget (2k chars,
     matched lines only) to the reader script or a PreToolUse hook.
   - `CLAUDE-ADDENDUM.md`: append to the project's CLAUDE.md so the rules hold
     across sessions.
   - `HOOKS.md`: the PreToolUse wiring if the flow needs interception.

5. **Keep error handling.** A generated wrapper may not swallow errors: it
   returns the exit code and a log excerpt, then hands the failure to the
   model. Judgment starts where the deterministic layer ends.

6. **Re-measure.** Next session, run the compiler again on the new transcript.
   The flows that disappear paid for themselves; the ones that persist need a
   sharper rule, not more scaffolding.

## Reporting

When you finish, tell the user: N flows detected, top flow, estimated tokens
saved per session, and what you wired where. One line each. No celebration.
