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
   reaches origin together with the branch you're about to open a PR for.
4. On `pull_request` (opened / synchronize / reopened) the GitHub Action fetches all
   `refs/token-usage/*` refs, aggregates every machine's records for the head branch, and
   creates/updates a single sticky PR comment.

## Setup (per repository)

```bash
# one time, by anyone: vendor the tool + workflow into the repo
curl -fsSL https://raw.githubusercontent.com/<you>/tokenchecker/main/tokenchecker.py \
  -o /tmp/tokenchecker.py            # or copy it from this repo
python3 /tmp/tokenchecker.py install # run from inside the target repo
git add scripts/tokenchecker.py .github/workflows/token-usage.yml
git commit -m "add tokenchecker"

# every contributor, once per clone (installs their local pre-push hook):
python3 scripts/tokenchecker.py install
```

`install` is idempotent. It:
- vendors the script at `scripts/tokenchecker.py` (committed, used by CI and teammates)
- appends a guarded block to `.git/hooks/pre-push` (local; preserves existing hooks)
- writes `.github/workflows/token-usage.yml` (committed)
- with `--claude-hook`, also adds a Claude Code `SessionEnd` hook to
  `.claude/settings.json` so usage syncs when a Claude session ends, not just on push

## Commands

```bash
python3 scripts/tokenchecker.py sync                 # collect + publish (what the hook runs)
python3 scripts/tokenchecker.py collect --dry-run    # preview per-branch usage, write nothing
python3 scripts/tokenchecker.py report               # all branches, all machines
python3 scripts/tokenchecker.py report --branch feature/x --markdown  # PR-comment format
python3 scripts/tokenchecker.py report --json        # raw records
```

Useful flags: `--since N` (lookback days, default 90), `--remote NAME` (default `origin`),
`--repo PATH` (default: cwd).

## The PR comment

```
## 🤖 AI token usage for `feature/x`

| Tool        | Model            | Sessions | Msgs | Input | Cache read | Cache write | Output | Total |
| claude-code | claude-fable-5   |        2 |    3 |   211 |      2,000 |         100 |     74 | 2,385 |
| codex       | gpt-5.5          |        1 |    1 |   500 |        400 |           0 |    100 | 1,000 |
| ...
```

Cache reads are reported separately from fresh input tokens (they're billed differently
and dominate agent usage), and the per-machine breakdown is in a collapsible section.

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
`TOKENCHECKER_GEMINI_DIR`, `TOKENCHECKER_CURSOR_GLOBAL_DB`, `TOKENCHECKER_CURSOR_WS_DIR`.
Setting `TOKENCHECKER_SKIP=1` disables the pre-push hook (used internally to prevent
recursion).

## Cleaning up

Usage refs are plain git refs; delete them with
`git push origin --delete refs/token-usage/<machine-id>` and remove
`.git/tokenchecker/` locally. Nothing else is stored anywhere.
