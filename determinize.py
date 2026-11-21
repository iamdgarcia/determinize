#!/usr/bin/env python3
"""determinize - compile agent-session trajectories into deterministic flows.

Reads coding-agent session transcripts (Claude Code JSONL, pi JSONL), finds the
parts of the trajectory that do not need a language model (repeated reads,
re-run read-only commands, retry loops, environment prologues, verification
re-runs, context dumps), and emits a report with the deterministic fix for each
flow plus per-flow token-waste estimates. The law: only determinize repetition;
never determinize judgment.

Stdlib only. Python 3.10+.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

OVERHEAD_TOKENS = 60          # per avoidable tool call (schema + framing)
DUMP_THRESHOLD_CHARS = 8000   # a single tool result worth excerpting
READONLY_FIRST = {
    "ls", "cat", "grep", "find", "head", "tail", "wc", "rg", "dir", "type",
    "echo", "pwd", "which", "where", "python", "git",
}
READONLY_GIT = {"status", "log", "diff", "branch", "show", "remote", "ls-remote", "rev-parse"}
VERIFY_RE = re.compile(r"^\s*(pytest|python -m pytest|npm (run )?test|npx (jest|vitest)|make |ruff|python -m ruff|eslint)")
ENV_SIG = re.compile(r"export\s+\w+=",)


@dataclass
class Call:
    tool: str
    arg: str                  # short human summary (command / path / pattern)
    result_chars: int
    status: str               # "ok" | "error" | "unknown"
    file_mutated: str | None = None   # path written/edited by this call, if any


@dataclass
class Flow:
    kind: str
    title: str
    occurrences: int
    waste_tokens: int
    evidence: list[str] = field(default_factory=list)
    fix: str = ""
    spec: str = ""


# ---------------------------------------------------------------- parsing

def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                if isinstance(c.get("text"), str):
                    parts.append(c["text"])
                elif isinstance(c.get("content"), str):
                    parts.append(c["content"])
        return "\n".join(parts)
    return ""


def _arg_summary(tool: str, args: dict) -> str:
    if not isinstance(args, dict):
        return str(args)[:80]
    for k in ("command", "cmd", "path", "file_path", "pattern", "url", "query"):
        if k in args and isinstance(args[k], str):
            return args[k][:120].replace("\n", " ")
    s = json.dumps(args, ensure_ascii=False)
    return s[:80]


def _mutated_path(tool: str, args: dict) -> str | None:
    if tool.lower() in ("write", "edit", "notebook_edit", "multiedit"):
        for k in ("path", "file_path"):
            if isinstance(args, dict) and isinstance(args.get(k), str):
                return args[k]
    return None


def _result_text_cc(content) -> str:
    """Claude Code tool_result content: str or [{type:text,text}]."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return _content_text(content)
    return ""


def parse_session(path: Path) -> list[Call]:
    """Extract the ordered tool-call sequence from one transcript.

    Supports: Claude Code JSONL (type assistant/user, tool_use/tool_result)
    and pi JSONL (type message, toolCall/toolResult).
    """
    calls: list[Call] = []
    pending: dict[str, tuple[str, str, str | None]] = {}  # id -> (tool, arg, mutated)

    def call_from(name: str, args: dict, cid: str) -> None:
        arg = _arg_summary(name, args)
        pending[cid] = (name, arg, _mutated_path(name, args))

    def result_from(cid: str, text: str, status: str) -> None:
        if cid in pending:
            tool, arg, mut = pending.pop(cid)
            calls.append(Call(tool, arg, len(text), status, mut))
        else:
            calls.append(Call("?", "?", len(text), status))

    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(d, dict):
                continue

            # ---- Claude Code shape
            if d.get("type") == "assistant":
                for c in d.get("message", {}).get("content", []) or []:
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        call_from(str(c.get("name", "?")), c.get("input") or {}, str(c.get("id")))
            elif d.get("type") == "user":
                for c in d.get("message", {}).get("content", []) or []:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        text = _result_text_cc(c.get("content"))
                        err = bool(c.get("is_error"))
                        result_from(str(c.get("tool_use_id")), text, "error" if err else "ok")

            # ---- pi shape
            elif d.get("type") == "message":
                m = d.get("message", {})
                role = m.get("role")
                content = m.get("content")
                if role == "assistant" and isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") in ("toolCall", "tool_use"):
                            call_from(str(c.get("name", "?")), c.get("arguments") or c.get("input") or {}, str(c.get("id")))
                elif role in ("toolResult", "tool"):
                    text = _content_text(content)
                    status = "error" if m.get("isError") or "is_error" in m else "unknown"
                    cid = str(m.get("toolCallId") or m.get("tool_use_id") or "")
                    result_from(cid, text, status)
    return calls


# ---------------------------------------------------------------- detectors

def _is_readonly_bash(cmd: str) -> bool:
    first = cmd.strip().split(" ")[0].strip("'\"") if cmd.strip() else ""
    if first == "git":
        sub = cmd.strip().split(" ")[1] if len(cmd.strip().split(" ")) > 1 else ""
        return sub in READONLY_GIT
    if first in ("python", "python3"):
        return "-c" in cmd
    return first in READONLY_FIRST


def _is_env_prologue(cmd: str) -> bool:
    return bool(ENV_SIG.search(cmd)) and bool(
        re.search(r"SSL|CERT|CA_BUNDLE|PYTHONPATH|REQUESTS_|PATH=|PROXY|TOKEN|KEY", cmd)
    )


def _is_verify(cmd: str) -> bool:
    return bool(VERIFY_RE.match(cmd))


def detect(calls: list[Call]) -> list[Flow]:
    flows: list[Flow] = []

    def waste(n_calls: int, chars: int) -> int:
        return n_calls * OVERHEAD_TOKENS + chars // 4

    # 1. environment prologue repeated
    env = [c for c in calls if c.tool.lower() in ("bash", "shell") and _is_env_prologue(c.arg)]
    if len(env) >= 1:
        f = Flow("env_preamble", "environment prologue re-typed in shell calls",
                 len(env), waste(len(env), sum(c.result_chars for c in env)),
                 [f"`{c.arg[:90]}`" for c in env[:5]])
        f.fix = ("write the exports once to flows/env.sh and make the harness source it "
                 "(hook or first instruction); the model never re-derives TLS/proxy boilerplate")
        f.spec = "flows/env.sh + source-once hook"
        flows.append(f)

    # 2. same file read again with no mutation in between
    reads: dict[str, list[int]] = {}
    for i, c in enumerate(calls):
        if c.tool.lower() in ("read", "view") or (
            c.tool.lower() in ("bash", "shell")
            and re.match(r"^\s*(cat|type|head|tail|sed -n)\s+", c.arg)
        ):
            key = c.arg.strip().split(" ")[0].strip("'\"").lower()
            reads.setdefault(key, []).append(i)
    reread_waste = 0
    reread_ev = []
    for path, idxs in reads.items():
        last_seen = None
        for i in idxs:
            if (last_seen is not None
                    and not any(calls[j].file_mutated and calls[j].file_mutated.lower() == path
                                for j in range(last_seen, i))):
                reread_waste += waste(1, calls[i].result_chars)
                if len(reread_ev) < 5:
                    reread_ev.append(f"`{path}` re-read at call {i+1} (no edit to it in between)")
            last_seen = i
    n_reread = len(reread_ev)
    if n_reread:
        f = Flow("repeat_read", "files re-read with no edit in between",
                 n_reread, reread_waste, reread_ev)
        f.fix = ("bundle the files once (flows/ctx.sh) or keep a project map; the model "
                 "re-reads because the transcript no longer trusts its own context")
        f.spec = "flows/ctx.sh <paths> -> one dated context bundle"
        flows.append(f)

    # 3. identical read-only command re-run with no mutation in between
    seen: dict[str, tuple[int, bool]] = {}
    rerun_waste = 0
    rerun_ev = []
    n_rerun = 0
    for i, c in enumerate(calls):
        if c.tool.lower() not in ("bash", "shell") or not _is_readonly_bash(c.arg):
            continue
        key = " ".join(c.arg.split())
        j, mut_pending = seen.get(key, (None, False))
        if j is not None and not mut_pending:
            n_rerun += 1
            rerun_waste += waste(1, c.result_chars)
            if len(rerun_ev) < 5:
                rerun_ev.append(f"`{key[:90]}` (call {j+1} -> {i+1})")
            seen[key] = (i, True)  # one flag per identical repeat chain
            continue
        mut_between = any(calls[k].file_mutated for k in range((j if j is not None else i), i + 1))
        seen[key] = (i, mut_between)
    if n_rerun:
        f = Flow("readonly_rerun", "read-only commands re-run with no edit in between",
                 n_rerun, rerun_waste, rerun_ev)
        f.fix = ("capture the result once (append to a scratch file or the task state); "
                 "or turn the query into a script whose name says what it returns")
        f.spec = "script per repeated query, output appended to task state"
        flows.append(f)

    # 4. retry loops: same tool+similar arg right after an error
    retry_waste = 0
    retry_ev = []
    for i, c in enumerate(calls):
        if c.status != "error":
            continue
        for j in range(i + 1, min(i + 4, len(calls))):
            nxt = calls[j]
            same = nxt.tool == c.tool and (
                nxt.arg.split(" ")[0] == c.arg.split(" ")[0] if nxt.tool.lower() in ("bash", "shell")
                else nxt.arg == c.arg
            )
            if same:
                retry_waste += waste(1, c.result_chars + nxt.result_chars)
                if len(retry_ev) < 5:
                    retry_ev.append(f"`{c.arg[:80]}` errored at call {i+1}, retried at {j+1}")
                break
    if retry_ev:
        f = Flow("retry_loop", "commands re-tried right after failing",
                 len(retry_ev), retry_waste, retry_ev)
        f.fix = ("wrap the flaky command in flows/guarded.sh: timeout + one retry + the "
                 "environment fix baked in; the model stops re-deriving the workaround")
        f.spec = "flows/guarded.sh <timeout_s> <cmd...> (timeout + single retry + fix preamble)"
        flows.append(f)

    # 5. verification re-runs
    ver = [c for c in calls if c.tool.lower() in ("bash", "shell") and _is_verify(c.arg)]
    if len(ver) >= 3:
        f = Flow("verify_rerun", "verification commands re-typed",
                 len(ver), waste(0, sum(c.result_chars for c in ver) // max(1, len(ver)) * (len(ver) - 1)),
                 [f"`{c.arg[:70]}` (x{len([x for x in ver if x.arg == c.arg])})" for c in ver[:3]])
        f.fix = ("one entrypoint flows/verify.sh that runs the suite; the model calls a "
                 "verb, not a sentence, and the invocation is identical every time")
        f.spec = "flows/verify.sh"
        flows.append(f)

    # 6. context dumps
    dumps = [c for c in calls if c.result_chars >= DUMP_THRESHOLD_CHARS and (
        c.tool.lower() in ("read", "view") or (
            c.tool.lower() in ("bash", "shell") and _is_readonly_bash(c.arg)))]
    if dumps:
        f = Flow("context_dump", "huge tool results pushed whole into context",
                 len(dumps), waste(len(dumps), sum(c.result_chars for c in dumps)),
                 [f"call {i+1}: `{c.arg[:70]}` -> {c.result_chars//1000}k chars" for i, c in enumerate(dumps[:5])])
        f.fix = ("excerpt on the boundary: head/tail/grep with a byte budget before the "
                 "result reaches the model")
        f.spec = "excerpt wrapper with byte budget (e.g. 2k chars, matched lines only)"
        flows.append(f)

    # cross-session marker
    return flows


# ---------------------------------------------------------------- report

RANK = {"retry_loop": 0, "env_preamble": 1, "context_dump": 2, "repeat_read": 3,
        "readonly_rerun": 4, "verify_rerun": 5}


def report_md(per_session: dict[Path, list[Flow]], all_calls: int) -> str:
    merged: dict[str, Flow] = {}
    for flows in per_session.values():
        for f in flows:
            if f.kind in merged:
                m = merged[f.kind]
                m.occurrences += f.occurrences
                m.waste_tokens += f.waste_tokens
                m.evidence += f.evidence[:3]
            else:
                merged[f.kind] = f
    flows = sorted(merged.values(), key=lambda f: (RANK.get(f.kind, 9), -f.waste_tokens))
    total_waste = sum(f.waste_tokens for f in flows)
    lines = ["# determinize report", "",
             f"tool calls analyzed: {all_calls} | flows detected: {len(flows)} | "
             f"estimated wasted context: ~{total_waste:,} tokens", "",
             "Estimates: chars/4 per result + 60 tokens overhead per avoidable call.", ""]
    if flows:
        lines += ["| # | flow | occurrences | est. waste (tokens) | deterministic fix |",
                  "|---|------|-------------|---------------------|-------------------|"]
        for i, f in enumerate(flows, 1):
            lines.append(f"| {i} | {f.title} | {f.occurrences} | ~{f.waste_tokens:,} | {f.fix} |")
        lines.append("")
        lines.append("## details")
        for i, f in enumerate(flows, 1):
            lines += [f"", f"### {i}. {f.title} (`{f.kind}`)",
                      f"- spec: `{f.spec}`"]
            lines += [f"- {e}" for e in f.evidence[:6]]
    else:
        lines.append("No deterministic-able flows found above threshold. Either the "
                     "session was already tight or the transcript format was not recognized.")
    return "\n".join(lines) + "\n"


SCAFFOLD = {
    "flows/env.sh": """#!/usr/bin/env bash
# deterministic environment preamble (generated by determinize)
# source this instead of letting the model re-type exports
export SSL_CERT_FILE="${SSL_CERT_FILE:-$HOME/certs/cacert.pem}"
export REQUESTS_CA_BUNDLE="${REQUESTS_CA_BUNDLE:-$SSL_CERT_FILE}"
# add your project's real exports below (copy them from the report evidence)
""",
    "flows/guarded.sh": """#!/usr/bin/env bash
# deterministic guard for flaky commands (generated by determinize)
# usage: flows/guarded.sh <timeout_s> <cmd...>
set -u
T="$1"; shift
timeout "$T" "$@" && exit 0
rc=$?
echo "[guarded] exit $rc; single retry under env preamble" >&2
source "$(dirname "$0")/env.sh"
exec timeout "$T" "$@"
""",
    "flows/verify.sh": """#!/usr/bin/env bash
# single verification entrypoint (generated by determinize)
# every check the model used to re-type, in one deterministic order
set -e
# python -m pytest -q
# npm test
# ruff check .
echo "[verify] add your real checks here"
""",
    "HOOKS.md": """# Hooks (Claude Code) — wire the deterministic layer

Put in `.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          { "type": "command",
            "command": "python .claude/hooks/bash_guard.py" }
        ]
      }
    ]
  }
}
```

`bash_guard.py` (minimal, fail-open): reads the PreToolUse JSON on stdin, and for
network git commands without a timeout wrapper, returns a deny-and-instruct
decision so the model re-issues the call through `flows/guarded.sh`.
""",
    "CLAUDE-ADDENDUM.md": """# Deterministic layer (append to CLAUDE.md)

- Environment (TLS/proxy/paths) lives in flows/env.sh. Never re-type exports; if a
  command fails for environment reasons, re-run through flows/guarded.sh.
- Verification is flows/verify.sh. Never re-type test/lint invocations.
- Reading the same file twice without editing it in between is a bug: bundle with
  flows/ctx.sh instead.
- Commands that failed once get ONE guarded retry, then hand the failure to the
  model with the exit code and the log excerpt. Judgment starts where the
  deterministic layer ends.
""",
}


def scaffold(out: Path) -> None:
    for rel, content in SCAFFOLD.items():
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------- selftest

FIXTURE = [
    {"type": "message", "message": {"role": "assistant", "content": [
        {"type": "toolCall", "id": "1", "name": "bash",
         "arguments": {"command": "export SSL_CERT_FILE=/x/cacert.pem; git status"}}]}},
    {"type": "message", "message": {"role": "toolResult", "toolCallId": "1", "content": [
        {"type": "text", "text": "On branch main\nnothing to commit"}]}},
    {"type": "message", "message": {"role": "assistant", "content": [
        {"type": "toolCall", "id": "2", "name": "read",
         "arguments": {"path": "src/app.py"}}]}},
    {"type": "message", "message": {"role": "toolResult", "toolCallId": "2", "content": [
        {"type": "text", "text": "def main():\n    pass"}]}},
    {"type": "message", "message": {"role": "assistant", "content": [
        {"type": "toolCall", "id": "3", "name": "read",
         "arguments": {"path": "src/app.py"}}]}},
    {"type": "message", "message": {"role": "toolResult", "toolCallId": "3", "content": [
        {"type": "text", "text": "def main():\n    pass"}]}},
    {"type": "message", "message": {"role": "assistant", "content": [
        {"type": "toolCall", "id": "4", "name": "bash",
         "arguments": {"command": "ls -la"}}]}},
    {"type": "message", "message": {"role": "toolResult", "toolCallId": "4", "content": [
        {"type": "text", "text": "src tests README.md"}]}},
    {"type": "message", "message": {"role": "assistant", "content": [
        {"type": "toolCall", "id": "5", "name": "bash",
         "arguments": {"command": "ls -la"}}]}},
    {"type": "message", "message": {"role": "toolResult", "toolCallId": "5", "content": [
        {"type": "text", "text": "src tests README.md"}]}},
    {"type": "message", "message": {"role": "assistant", "content": [
        {"type": "toolCall", "id": "6", "name": "bash",
         "arguments": {"command": "git fetch origin main"}}]}},
    {"type": "message", "message": {"role": "toolResult", "toolCallId": "6", "isError": True, "content": [
        {"type": "text", "text": "fatal: unable to access: gnutls handshake failed"}]}},
    {"type": "message", "message": {"role": "assistant", "content": [
        {"type": "toolCall", "id": "7", "name": "bash",
         "arguments": {"command": "git fetch origin main"}}]}},
    {"type": "message", "message": {"role": "toolResult", "toolCallId": "7", "content": [
        {"type": "text", "text": "From origin\n * branch main"}]}},
    {"type": "message", "message": {"role": "assistant", "content": [
        {"type": "toolCall", "id": "8", "name": "write",
         "arguments": {"path": "src/app.py"}}]}},
    {"type": "message", "message": {"role": "toolResult", "toolCallId": "8", "content": [
        {"type": "text", "text": "OK"}]}},
]


def selftest() -> int:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "fixture.jsonl"
        p.write_text("\n".join(json.dumps(x) for x in FIXTURE), encoding="utf-8")
        calls = parse_session(p)
        kinds = {f.kind for f in detect(calls)}
        expect = {"env_preamble", "repeat_read", "readonly_rerun", "retry_loop"}
        missing = expect - kinds
        assert not missing, f"selftest FAILED: missing flows {missing}; got {kinds}"
        print(f"selftest PASS: {len(calls)} calls -> {sorted(kinds)}")
    return 0


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="determinize",
                                 description="compile agent trajectories into deterministic flows")
    ap.add_argument("sessions", nargs="*", help="session transcript .jsonl files")
    ap.add_argument("--out", default="determinize-report", help="output directory")
    ap.add_argument("--scaffold", action="store_true", help="also generate flows/ + hooks + addendum")
    ap.add_argument("--selftest", action="store_true", help="run the built-in check")
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()
    if not a.sessions:
        ap.error("give me at least one session .jsonl (or --selftest)")

    per: dict[Path, list[Flow]] = {}
    total = 0
    for s in a.sessions:
        p = Path(s)
        calls = parse_session(p)
        total += len(calls)
        per[p] = detect(calls)
        if not calls:
            print(f"[warn] {p}: 0 tool calls parsed - unrecognized transcript format?", file=sys.stderr)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(report_md(per, total), encoding="utf-8")
    if a.scaffold:
        scaffold(out)
        print(f"[ok] scaffolded flows/ + HOOKS.md + CLAUDE-ADDENDUM.md in {out}/")

    for p, flows in per.items():
        w = sum(f.waste_tokens for f in flows)
        print(f"[ok] {p.name}: {len(flows)} flows, ~{w:,} tokens waste")
    print(f"[ok] report -> {out}/report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
