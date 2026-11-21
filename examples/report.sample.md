# determinize report

tool calls analyzed: 8 | flows detected: 4 | estimated wasted context: ~275 tokens

Estimates: chars/4 per result + 60 tokens overhead per avoidable call.

| # | flow | occurrences | est. waste (tokens) | deterministic fix |
|---|------|-------------|---------------------|-------------------|
| 1 | commands re-tried right after failing | 1 | ~78 | wrap the flaky command in flows/guarded.sh: timeout + one retry + the environment fix baked in; the model stops re-deriving the workaround |
| 2 | environment prologue re-typed in shell calls | 1 | ~68 | write the exports once to flows/env.sh and make the harness source it (hook or first instruction); the model never re-derives TLS/proxy boilerplate |
| 3 | files re-read with no edit in between | 1 | ~65 | bundle the files once (flows/ctx.sh) or keep a project map; the model re-reads because the transcript no longer trusts its own context |
| 4 | read-only commands re-run with no edit in between | 1 | ~64 | capture the result once (append to a scratch file or the task state); or turn the query into a script whose name says what it returns |

## details

### 1. commands re-tried right after failing (`retry_loop`)
- spec: `flows/guarded.sh <timeout_s> <cmd...> (timeout + single retry + fix preamble)`
- `git fetch origin main` errored at call 6, retried at 7

### 2. environment prologue re-typed in shell calls (`env_preamble`)
- spec: `flows/env.sh + source-once hook`
- `export SSL_CERT_FILE=/x/cacert.pem; git status`

### 3. files re-read with no edit in between (`repeat_read`)
- spec: `flows/ctx.sh <paths> -> one dated context bundle`
- `src/app.py` re-read at call 3 (no edit to it in between)

### 4. read-only commands re-run with no edit in between (`readonly_rerun`)
- spec: `script per repeated query, output appended to task state`
- `ls -la` (call 4 -> 5)
