# tokenchecker

Count AI coding-agent tokens **per git branch** — across sessions and machines — and post
the totals as a comment on the GitHub PR when it opens.

Supported agents (parsed from their local logs, no API keys needed):

| Tool | Data source | Branch attribution |
|---|---|---|
| **Claude Code** | `~/.claude/projects/<project>/*.jsonl` (per-message `usage`) | exact — `gitBranch` recorded per message |
| **Codex CLI** | `~/.codex/sessions/Y/M/D/*.jsonl` (`token_count` events) | branch recorded in session metadata |
| **Gemini CLI** | `~/.gemini/tmp/<sha256(cwd)>/chats/*.json` (per-message `tokens`) | inferred from your repo's `HEAD` reflog at message time |
| **Cursor** | `state.vscdb` (per-message `tokenCount` in bubble records) | inferred from `HEAD` reflog at message time |

Single-file, stdlib-only Python 3.9+. macOS and Linux (Cursor paths auto-detected on macOS;
override with env vars elsewhere).

## How it works

```
 your laptop                         origin (GitHub)                PR
┌─────────────────────┐             ┌──────────────────────┐
│ claude / codex /    │  git push   │ refs/token-usage/    │  GitHub Action:
│ gemini / cursor logs│ ──────────► │   laptop-a1b2c3d4    │  fetch refs/token-usage/*
│        │            │ (pre-push   │   desktop-e5f6a7b8   │  aggregate head branch
│        ▼            │  hook runs  │   coworker-c9d0e1f2  │  upsert PR comment
│ tokenchecker sync   │  `sync`)    └──────────────────────┘
└─────────────────────┘
```

1. `tokenchecker collect` scans the local logs of all four tools, keeps only records whose
   working directory (or remote URL) matches the current repo, normalizes them into
   deduplicatable records (`input / cache_read / cache_write / output / total` + branch +
   timestamp + machine), and stores them under `.git/tokenchecker/`.
2. `tokenchecker push` publishes the machine's records to a custom git ref
   **`refs/token-usage/<machine-id>`** on origin. Each machine owns exactly one ref, so
   there are never conflicts, and no external storage is needed. Records are idempotent —
   re-collecting and re-pushing never double-counts.
3. A `pre-push` git hook runs `sync` (= collect + push) automatically, so usage data
   reaches origin together with the branch you're about to open a PR for. If the branch
   has an open PR and the `gh` CLI is available, the hook also upserts the sticky PR
   comment right then — **no CI or repo files required**.
4. Optionally, a tiny committed workflow re-renders the comment on every
   `pull_request` event via the `ZohaibAhmed/tokenchecker@v0` action, so the comment
   stays current no matter who pushes or whether they have `gh`.

## Setup

### Global (recommended): once per machine

```bash
pipx install tokenchecker        # or: uv tool install tokenchecker
# or, straight from git (works for private forks too):
#   pipx install git+https://github.com/ZohaibAhmed/tokenchecker
# or, no package manager at all — the script is a single stdlib-only file:
#   python3 tokenchecker.py install --global   (from a checkout)

tokenchecker install --global
```

This copies the script to `~/.tokenchecker/`, adds a `tokenchecker` CLI wrapper at
`~/.tokenchecker/bin/`, and sets git's global `core.hooksPath` to a directory of
dispatcher hooks that **chain to each repository's own `.git/hooks/*` first**, then record
token usage on `pre-push`. Every repo on the machine is covered automatically — no
per-repo, per-clone setup.

- Opt a repo out: `git config tokenchecker.enabled false`
- Repos that set `core.hooksPath` themselves (e.g. husky) bypass the global hooks — add
  the sync line to their hook system or use the per-repo install there.
- If you already had a global `core.hooksPath`, the installer refuses to clobber it and
  prints the one line to add to your existing pre-push hook.
- Hook runs are quiet when there is nothing new; you only see output when records are
  actually collected or pushed.

**PR comments need nothing more.** When you `git push` a branch with an open PR, the
hook posts/updates the report comment straight from your machine using the `gh` CLI
(opt out with `git config tokenchecker.comment false`). For teams that want the comment
maintained by CI as well — so it updates regardless of who pushes — commit one small
workflow per repo: run `tokenchecker install` there and commit
`.github/workflows/token-usage.yml` (a dozen boilerplate lines that call the
`ZohaibAhmed/tokenchecker@v0` action; the action pip-installs tokenchecker in CI, so
there is nothing else to vendor or keep up to date).

### Per repository (alternative)

```bash
# one time, by anyone: add the CI workflow
tokenchecker install                 # run from inside the target repo
git add .github/workflows/token-usage.yml
git commit -m "add tokenchecker workflow"

# every contributor, once per machine:
tokenchecker install --global        # (or `tokenchecker install` per clone)
```

`install` is idempotent. It:
- writes `.github/workflows/token-usage.yml` (committed) — a few boilerplate lines
  calling the `ZohaibAhmed/tokenchecker@v0` action, which installs tokenchecker from
  PyPI in CI (pin with `with: {version: "tokenchecker==0.3.0"}` if you like)
- appends a guarded block to `.git/hooks/pre-push` (local; preserves existing hooks;
  skipped when the global install already covers the repo)
- with `--vendor`, embeds the script at `scripts/tokenchecker.py` and writes a fully
  self-contained workflow instead — for repos that can't use PyPI or third-party
  actions in CI
- with `--claude-hook`, also adds a Claude Code `SessionEnd` hook to
  `.claude/settings.json` so usage syncs when a Claude session ends, not just on push

## Commands

```bash
tokenchecker sync                 # collect + publish (what the hook runs)
tokenchecker collect --dry-run    # preview per-branch usage, write nothing
tokenchecker status               # when did it last run, what's synced where
tokenchecker report               # all branches, all machines, with costs
tokenchecker report --branch feature/x --markdown  # PR-comment format
tokenchecker report --json        # raw records
tokenchecker comment              # post/update the PR comment for this branch (gh)
```

## Seeing it run

- **On every `git push`** the hook prints two lines in your terminal:
  ```
  tokenchecker: collected 7 records (claude-code=4, codex=1, gemini=1, cursor=1); 0 new; store has 6
  tokenchecker: pushed 6 records to origin refs/token-usage/laptop-a1b2c3d4
  ```
- **`status`** shows the machine id, the local store, a timestamped log of recent
  collect/push runs (kept in `.git/tokenchecker/log`), every synced machine ref with its
  last-push time, and which refs exist on origin.
- **In CI**, the Action run appears in the PR's Checks tab; the rendered report is printed
  in the job log and on the run's Summary page, in addition to the PR comment itself.

Useful flags: `--since N` (lookback days, default 90), `--remote NAME` (default `origin`),
`--repo PATH` (default: cwd).

## The PR comment

```
## 🤖 AI token usage for `feature/x`

| Tool        | Model            | Sessions | Msgs | Input | Cache read | Cache write | Output | Total | Est. cost |
| claude-code | claude-fable-5   |        2 |    3 |   211 |      2,000 |         100 |     74 | 2,385 |     $0.01 |
| codex       | gpt-5.5          |        1 |    1 |   500 |        400 |           0 |    100 | 1,000 |     $0.01 |
| ...
```

Cache reads are reported separately from fresh input tokens (they're billed differently
and dominate agent usage), and the per-machine breakdown is in a collapsible section.

## Cost estimates

Every report includes an **Est. cost** column: the four token buckets priced at each
model's API list rates (input / cache read / cache write / output). It's an estimate —
subscription plans (Claude Max, ChatGPT plans, Cursor Pro) bill differently — but it
answers "what did this branch consume in model time".

Costs are computed at report time, never stored, so a price update retroactively
corrects every report. Prices resolve through three layers, and each report's footer
says which one was used (and its date):

1. **`TOKENCHECKER_PRICES=<file>`** — pin your own rates. Accepts either LiteLLM's
   schema or a simple `{ "<model>": {"input": $, "cache_read": $, "cache_write": $,
   "output": $} }` map in $/MTok.
2. **Live fetch** of [LiteLLM's community price table](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json),
   cached for 7 days at `~/.tokenchecker/prices.json` (skip with
   `TOKENCHECKER_NO_NETWORK=1`). CI always gets fresh prices this way.
3. **Embedded fallback table** baked into the script, regenerated monthly by the
   `update-prices` workflow (which opens a PR only when prices drifted).

Models with no known pricing show `—`, are excluded from the total, and are listed in
a footnote — never silently counted as $0 without a trace.

## Semantics & accuracy notes

- **Dedup**: every record has a stable id (message id / session id / bubble id). Streaming
  snapshots, re-collection, and multi-machine overlap all collapse to the max-total record,
  so numbers never double-count.
- **Codex** reports a cumulative counter per session; tokenchecker takes the session's
  final counter (one record per session), attributed to the branch the session started on.
- **Gemini / Cursor** don't record the branch, so tokenchecker replays your repo's `HEAD`
  reflog to determine which branch was checked out at each message's timestamp. This is
  accurate unless you rewrite history, and reflog entries expire after ~90 days (git's
  default) — matching the default `--since 90` window.
- **Cursor** exposes `inputTokens`/`outputTokens` per message locally but no cache split.
  Older Cursor conversations (before it started persisting `tokenCount`) report 0.
- Tokens spent on a machine only reach the PR after that machine pushes (any push — the
  hook publishes usage for all branches, not just the pushed one) or runs `sync` manually.
- Forks: contributors pushing from forks publish `refs/token-usage/*` to their fork, which
  the base repo's Action can't see. Same-repo branches are fully supported.

## Env overrides

`TOKENCHECKER_MACHINE_ID`, `TOKENCHECKER_CLAUDE_DIR`, `TOKENCHECKER_CODEX_DIR`,
`TOKENCHECKER_GEMINI_DIR`, `TOKENCHECKER_CURSOR_GLOBAL_DB`, `TOKENCHECKER_CURSOR_WS_DIR`,
`TOKENCHECKER_PRICES` (path to a pinned price table), `TOKENCHECKER_NO_NETWORK`
(never fetch live prices).
Setting `TOKENCHECKER_SKIP=1` disables the pre-push hook (used internally to prevent
recursion).

## Cleaning up

Usage refs are plain git refs; delete them with
`git push origin --delete refs/token-usage/<machine-id>` and remove
`.git/tokenchecker/` locally. Nothing else is stored anywhere.
