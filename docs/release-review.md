# Pre-announcement bug review

Reviewed the command-line entry points, all four collectors, local storage, remote
sync, hook installation, report/comment code, package build, and GitHub workflows.
The fixes are in the working tree; no release or live PR comment was published.

## Bugs fixed

| Area | Failure | Fix |
|---|---|---|
| GitHub Action / vendored workflow | A branch name containing shell substitution could execute commands in the report step. Action input interpolation had the same problem. | Pass event values and package requirements through environment variables and quote their use. |
| Vendored workflow | The workflow executed the PR's modified copy of the reporting script with comment permissions. | Check out the PR base commit for the vendored reporting job. |
| Fork PRs | The workflow attempted comments with unavailable write permissions and couldn't access the fork's usage refs. | Skip fork PR reports explicitly. |
| Dry runs | `sync --dry-run` still called push and could update a PR comment. | Return immediately after preview collection. |
| Dry runs | A collection preview could create a price-cache file despite promising no writes. | Disable cache writes for previews. |
| Remote sync | A local matching ref incorrectly skipped publishing to another remote or restoring a deleted remote ref. | Compare with the selected destination's actual snapshot. |
| Clones and worktrees | Different checkouts on one machine could force-overwrite each other's records. Concurrent publishers could also lose data. | Merge the destination snapshot and use an explicit force-with-lease; reject concurrent changes for a safe retry. |
| Local storage | Simultaneous collectors could overwrite each other, and interrupted writes could truncate the store. | Lock the read/merge/write operation and atomically replace the completed file. |
| Claude usage | Nested subagent logs were not scanned. | Recursively collect project JSONL files while retaining repository filtering and deduplication. |
| Codex attribution | Without branch metadata, a session was assigned using its final event time instead of its start time. | Keep the start timestamp for branch lookup. |
| Branch attribution | Checkouts within one second were sorted alphabetically, producing the wrong final branch. | Preserve reflog order for timestamp ties. |
| Cursor attribution | A workspace containing several repositories assigned all its conversations to every child repository. | Require the workspace folder to be inside the target repo; retain content-based matching separately. |
| Malformed data | Non-object JSON, invalid token counts, timestamps, and malformed price overrides/caches could crash collection or reporting. | Validate these inputs and skip invalid records or fall back to embedded prices. |
| Existing hooks | Appending shell code after `exit 0` never ran; appending it to a Python hook broke the hook. | Preserve the original as a separate executable and invoke it through a shell wrapper, propagating failure and forwarding stdin/arguments. |
| Linked worktree hooks | Installation and global dispatch looked in the worktree-specific git directory instead of the shared hooks directory. | Resolve the common git directory. |
| Hook sync behavior | Hooks always targeted origin; per-repo hooks ignored the opt-out flag; global plus per-repo installs could sync twice. | Forward the actual push remote, honor the opt-out, and suppress nested tokenchecker sync. |
| Installer paths | Shell metacharacters in the installation directory were evaluated by generated wrappers. The per-repo fallback also ignored `TOKENCHECKER_HOME`. | Shell-quote generated script paths and honor the configured home. |
| Claude hook installation | Searching JSON-escaped text for the raw command failed to recognize an installed hook, adding duplicates. | Compare the actual command fields. |
| PR comment maintenance | The vendored workflow inspected only the first 100 comments; a failed CLI lookup could be treated as no existing comment. | Paginate the workflow lookup and propagate CLI lookup failures before creating comments. |
| Price updater | Unchanged rates still rewrote the table date and opened unnecessary update PRs. | Compare rates before rewriting the table. |

The installer upgrades the exact stock 0.3.1 workflows to include the workflow fixes.
Customized workflows are preserved. README installation, dry-run, concurrency, fork,
and cleanup descriptions were corrected to reflect the implementation.

## Verification

- 34 offline regression tests covering the reproduced failures, preservation of
  existing behavior, concurrent collectors/publishers, and installer upgrades.
- Tests run on local Python 3.9.6, 3.12.9, and 3.14.3.
- Source distribution and wheel build; isolated wheel installation and CLI smoke test.
- Workflow YAML parsing and shell syntax validation, including both generated templates.
- Static checks for undefined names and syntax errors; clean whitespace diff.
- Added macOS/Linux CI coverage for Python 3.9 and 3.14; release builds now run tests.

Run the suite with `python3 -m unittest discover -s tests -v`.
GitHub API calls use stubs, and sync integration tests use local bare repositories.
Live GitHub permissions, hosted CI execution, and every upstream agent-log variant
were not exercised. This review is not a guarantee that no bugs remain.

## Applying the fixes to existing installations

Publish an updated package/action version before announcing the fixes as available
to users. This review intentionally leaves release tags and the package version alone.
After upgrading, rerun `install --global` for global hooks and `install` in repositories
with local hooks or generated workflows. Review customized workflows manually for
the environment-variable handling and trusted-base checkout changes. The initial
vendored script must be on the base branch before its PR report job can use it.
