#!/usr/bin/env python3
"""tokenchecker — count AI coding-agent tokens per git branch and report them on PRs.

Collects token usage from local logs of Claude Code, Gemini CLI, Codex CLI and
Cursor, attributes it to the git branch it was spent on, syncs records across
machines through custom git refs (refs/token-usage/<machine>), and renders a
per-branch report that a GitHub Action posts as a PR comment.

Stdlib only. Python 3.9+.

Commands:
  collect   scan local agent logs for usage in this repo, store in .git
  push      publish this machine's records to origin (refs/token-usage/<id>)
  sync      collect + push
  report    aggregate records (local + all synced machines) per branch
  install   vendor this script, add pre-push hook + GitHub workflow
"""

import argparse
import getpass
import glob
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

REF_PREFIX = "refs/token-usage/"
RECORDS_BLOB = "records.jsonl"
LOCAL_DIR = "tokenchecker"  # under $GIT_DIR
COMMENT_MARKER = "<!-- tokenchecker-report -->"
DEFAULT_SINCE_DAYS = 90

# ---------------------------------------------------------------- utilities


def eprint(*a):
    print(*a, file=sys.stderr)


def run(cmd, cwd=None, check=True, input_=None, env=None):
    e = dict(os.environ)
    e["TOKENCHECKER_SKIP"] = "1"
    if env:
        e.update(env)
    p = subprocess.run(
        cmd, cwd=cwd, input=input_, env=e,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if check and p.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}\n{p.stderr.strip()}")
    return p.stdout


def git(repo, *args, check=True, input_=None):
    return run(["git", "-C", repo] + list(args), check=check, input_=input_)


def repo_root(path=None):
    try:
        return run(["git", "-C", path or os.getcwd(), "rev-parse", "--show-toplevel"]).strip()
    except RuntimeError:
        return None


def git_dir(repo):
    d = git(repo, "rev-parse", "--git-dir").strip()
    return d if os.path.isabs(d) else os.path.join(repo, d)


def machine_id():
    override = os.environ.get("TOKENCHECKER_MACHINE_ID")
    if override:
        return re.sub(r"[^a-zA-Z0-9-]", "-", override)
    host = socket.gethostname().split(".")[0]
    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    digest = hashlib.sha256(f"{host}:{user}:{os.path.expanduser('~')}".encode()).hexdigest()[:8]
    host = re.sub(r"[^a-zA-Z0-9-]", "-", host).strip("-") or "machine"
    return f"{host}-{digest}"


def parse_ts(value):
    """ISO-8601 (Z or offset) or epoch seconds/ms -> unix seconds, or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value / 1000.0 if value > 4e10 else float(value)
    s = str(value).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm_remote_url(url):
    """Normalize a git remote URL so https/ssh/scp forms compare equal."""
    if not url:
        return ""
    u = url.strip().lower()
    u = re.sub(r"^(https?|ssh|git)://", "", u)
    u = re.sub(r"^[^@/]+@", "", u)
    # scp-like host:path -> host/path
    m = re.match(r"^([^/:]+):(.+)$", u)
    if m:
        u = f"{m.group(1)}/{m.group(2)}"
    u = u.rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    return u


def path_inside(path, root):
    if not path:
        return False
    try:
        p = os.path.realpath(path)
        r = os.path.realpath(root)
    except OSError:
        return False
    return p == r or p.startswith(r + os.sep)


# ------------------------------------------------------- branch attribution


class BranchTimeline:
    """Answers 'which branch was checked out at time T' using the HEAD reflog."""

    def __init__(self, repo):
        self.events = []  # sorted (unix_ts, branch_after)
        self.first_from = None
        self.current = None
        try:
            self.current = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() or None
            if self.current == "HEAD":
                self.current = None
        except RuntimeError:
            pass
        try:
            out = git(repo, "log", "-g", "--format=%gd|%gs", "--date=unix", "HEAD", check=False)
        except RuntimeError:
            out = ""
        for line in out.splitlines():
            m = re.match(r"HEAD@\{(\d+)\}\|checkout: moving from (\S+) to (\S+)", line)
            if m:
                self.events.append((int(m.group(1)), m.group(3)))
                self.first_from = m.group(2)  # log -g is newest-first
        self.events.sort()

    def branch_at(self, ts):
        if ts is None:
            return self.current
        best = None
        for ev_ts, branch in self.events:
            if ev_ts <= ts:
                best = branch
            else:
                break
        if best is None:
            best = self.first_from if self.events else self.current
        if best and re.fullmatch(r"[0-9a-f]{7,40}", best):
            return None  # detached HEAD
        return best


# ------------------------------------------------------------- record model
# A record is one deduplicatable unit of spend:
# {id, tool, model, ts, branch, session, machine,
#  input, cache_read, cache_write, output, total}


def make_record(rid, tool, model, ts, branch, session,
                input_t=0, cache_read=0, cache_write=0, output=0, total=None):
    input_t = max(0, int(input_t or 0))
    cache_read = max(0, int(cache_read or 0))
    cache_write = max(0, int(cache_write or 0))
    output = max(0, int(output or 0))
    if total is None:
        total = input_t + cache_read + cache_write + output
    return {
        "id": rid,
        "tool": tool,
        "model": model or "unknown",
        "ts": iso(ts) if ts else None,
        "branch": branch or "(unknown)",
        "session": session,
        "machine": machine_id(),
        "input": input_t,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "output": output,
        "total": int(total),
    }


def dedup(records):
    """Merge by id; keep the entry with the largest total (streaming snapshots grow)."""
    by_id = {}
    for r in records:
        rid = r.get("id")
        if not rid:
            continue
        prev = by_id.get(rid)
        if prev is None or r.get("total", 0) > prev.get("total", 0):
            by_id[rid] = r
    return list(by_id.values())


# ------------------------------------------------------------ Claude Code


def claude_projects_dir():
    return os.environ.get("TOKENCHECKER_CLAUDE_DIR",
                          os.path.join(os.path.expanduser("~"), ".claude", "projects"))


def collect_claude(repo, since_ts, timeline):
    base = claude_projects_dir()
    if not os.path.isdir(base):
        return []
    sanitized = re.sub(r"[^A-Za-z0-9-]", "-", os.path.realpath(repo))
    candidates = []
    for d in os.listdir(base):
        if d == sanitized or d.startswith(sanitized + "-"):
            candidates.append(os.path.join(base, d))
    records = []
    for pdir in candidates:
        for fp in glob.glob(os.path.join(pdir, "*.jsonl")):
            try:
                if os.path.getmtime(fp) < since_ts:
                    continue
            except OSError:
                continue
            records.extend(_parse_claude_file(fp, repo, since_ts, timeline))
    return records


def _parse_claude_file(fp, repo, since_ts, timeline):
    out = []
    try:
        fh = open(fp, encoding="utf-8", errors="replace")
    except OSError:
        return out
    with fh:
        for line in fh:
            try:
                d = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if d.get("type") != "assistant":
                continue
            msg = d.get("message") or {}
            usage = msg.get("usage") or {}
            if not usage:
                continue
            if not path_inside(d.get("cwd"), repo):
                continue
            model = msg.get("model") or ""
            if model == "<synthetic>":
                continue
            ts = parse_ts(d.get("timestamp"))
            if ts and ts < since_ts:
                continue
            session = d.get("sessionId") or os.path.basename(fp).rsplit(".", 1)[0]
            mid = msg.get("id") or d.get("requestId") or d.get("uuid")
            if not mid:
                continue
            branch = d.get("gitBranch") or timeline.branch_at(ts)
            out.append(make_record(
                rid=f"claude:{session}:{mid}",
                tool="claude-code", model=model, ts=ts, branch=branch, session=session,
                input_t=usage.get("input_tokens", 0),
                cache_read=usage.get("cache_read_input_tokens", 0),
                cache_write=usage.get("cache_creation_input_tokens", 0),
                output=usage.get("output_tokens", 0),
            ))
    return out


# -------------------------------------------------------------- Codex CLI


def codex_sessions_dir():
    return os.environ.get("TOKENCHECKER_CODEX_DIR",
                          os.path.join(os.path.expanduser("~"), ".codex", "sessions"))


def collect_codex(repo, since_ts, timeline, origin_urls):
    base = codex_sessions_dir()
    if not os.path.isdir(base):
        return []
    records = []
    for fp in glob.glob(os.path.join(base, "*", "*", "*", "*.jsonl")):
        try:
            if os.path.getmtime(fp) < since_ts:
                continue
        except OSError:
            continue
        rec = _parse_codex_file(fp, repo, since_ts, timeline, origin_urls)
        if rec:
            records.append(rec)
    return records


def _parse_codex_file(fp, repo, since_ts, timeline, origin_urls):
    session_id = None
    session_ts = None
    branch = None
    model = None
    matched = False
    best = None  # token usage dict with max total_tokens
    try:
        fh = open(fp, encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            try:
                d = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            t = d.get("type")
            p = d.get("payload") or {}
            if t == "session_meta":
                session_id = p.get("id")
                session_ts = parse_ts(p.get("timestamp") or d.get("timestamp"))
                gitinfo = p.get("git") or {}
                branch = gitinfo.get("branch")
                if path_inside(p.get("cwd"), repo):
                    matched = True
                elif norm_remote_url(gitinfo.get("repository_url")) in origin_urls:
                    matched = True
                if not matched:
                    return None
            elif t == "turn_context":
                model = p.get("model") or model
            elif t == "event_msg" and p.get("type") == "token_count":
                info = p.get("info") or {}
                usage = info.get("total_token_usage") or {}
                if usage and usage.get("total_tokens", 0) >= (best or {}).get("total_tokens", 0):
                    best = usage
                    session_ts = parse_ts(d.get("timestamp")) or session_ts
    if not (matched and best and session_id):
        return None
    if session_ts and session_ts < since_ts:
        return None
    if not branch:
        branch = timeline.branch_at(session_ts)
    cached = best.get("cached_input_tokens", 0) or 0
    input_total = best.get("input_tokens", 0) or 0
    return make_record(
        rid=f"codex:{session_id}",
        tool="codex", model=model, ts=session_ts, branch=branch, session=session_id,
        input_t=max(0, input_total - cached),
        cache_read=cached,
        output=best.get("output_tokens", 0),
        total=best.get("total_tokens"),
    )


# ------------------------------------------------------------- Gemini CLI


def gemini_tmp_dir():
    return os.environ.get("TOKENCHECKER_GEMINI_DIR",
                          os.path.join(os.path.expanduser("~"), ".gemini", "tmp"))


def collect_gemini(repo, since_ts, timeline):
    base = gemini_tmp_dir()
    if not os.path.isdir(base):
        return []
    # Gemini keys project dirs by sha256 of the launch cwd. Cover the repo root
    # and first-level subdirectories (common launch points).
    roots = {os.path.realpath(repo)}
    try:
        for name in os.listdir(repo):
            p = os.path.join(repo, name)
            if os.path.isdir(p) and not name.startswith("."):
                roots.add(os.path.realpath(p))
    except OSError:
        pass
    hashes = {hashlib.sha256(r.encode()).hexdigest() for r in roots}
    records = []
    for h in hashes:
        for fp in glob.glob(os.path.join(base, h, "chats", "*.json")):
            try:
                if os.path.getmtime(fp) < since_ts:
                    continue
            except OSError:
                continue
            records.extend(_parse_gemini_session(fp, since_ts, timeline))
    return dedup(records)


def _parse_gemini_session(fp, since_ts, timeline):
    try:
        with open(fp, encoding="utf-8", errors="replace") as fh:
            d = json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    session = d.get("sessionId") or os.path.basename(fp)
    out = []
    for m in d.get("messages") or []:
        if not isinstance(m, dict):
            continue
        tokens = m.get("tokens") or {}
        if not tokens or not tokens.get("total"):
            continue
        ts = parse_ts(m.get("timestamp")) or parse_ts(d.get("lastUpdated"))
        if ts and ts < since_ts:
            continue
        mid = m.get("id")
        if not mid:
            continue
        cached = tokens.get("cached", 0) or 0
        out.append(make_record(
            rid=f"gemini:{session}:{mid}",
            tool="gemini", model=m.get("model"), ts=ts,
            branch=timeline.branch_at(ts), session=session,
            input_t=max(0, (tokens.get("input", 0) or 0) - cached) + (tokens.get("tool", 0) or 0),
            cache_read=cached,
            output=(tokens.get("output", 0) or 0) + (tokens.get("thoughts", 0) or 0),
            total=tokens.get("total"),
        ))
    return out


# ----------------------------------------------------------------- Cursor


def cursor_global_db():
    return os.environ.get(
        "TOKENCHECKER_CURSOR_GLOBAL_DB",
        os.path.join(os.path.expanduser("~"), "Library", "Application Support",
                     "Cursor", "User", "globalStorage", "state.vscdb"))


def cursor_workspace_dir():
    return os.environ.get(
        "TOKENCHECKER_CURSOR_WS_DIR",
        os.path.join(os.path.expanduser("~"), "Library", "Application Support",
                     "Cursor", "User", "workspaceStorage"))


def _sqlite_ro(path):
    """Open a possibly-live sqlite db read-only; falls back to a temp copy."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("SELECT 1")
        return conn, None
    except sqlite3.Error:
        pass
    tmp = tempfile.mkdtemp(prefix="tokenchecker-db-")
    dst = os.path.join(tmp, os.path.basename(path))
    try:
        shutil.copy2(path, dst)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(path + suffix):
                shutil.copy2(path + suffix, dst + suffix)
        conn = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
        conn.execute("SELECT 1")
        return conn, tmp
    except (sqlite3.Error, OSError):
        shutil.rmtree(tmp, ignore_errors=True)
        return None, None


def collect_cursor(repo, since_ts, timeline):
    ws_dir = cursor_workspace_dir()
    gdb_path = cursor_global_db()
    if not os.path.exists(gdb_path):
        return []
    conn, tmp = _sqlite_ro(gdb_path)
    if conn is None:
        return []
    records = []
    try:
        composer_ids = _cursor_composers_for_repo(ws_dir, repo)
        composer_ids |= _cursor_composers_by_content(conn, repo, since_ts,
                                                     exclude=composer_ids)
        models = _cursor_composer_models(conn, composer_ids)
        for cid in composer_ids:
            try:
                rows = conn.execute(
                    "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
                    (f"bubbleId:{cid}:%",),
                ).fetchall()
            except sqlite3.Error:
                continue
            for key, value in rows:
                try:
                    b = json.loads(value)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                tc = b.get("tokenCount") or {}
                inp = tc.get("inputTokens", 0) or 0
                outp = tc.get("outputTokens", 0) or 0
                if inp + outp <= 0:
                    continue
                ts = parse_ts(b.get("createdAt"))
                if ts and ts < since_ts:
                    continue
                bubble_id = key.split(":")[-1]
                records.append(make_record(
                    rid=f"cursor:{cid}:{bubble_id}",
                    tool="cursor",
                    model=(b.get("modelInfo") or {}).get("modelName")
                    or b.get("modelName") or models.get(cid),
                    ts=ts, branch=timeline.branch_at(ts), session=cid,
                    input_t=inp, output=outp,
                ))
    finally:
        conn.close()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    return records


def _cursor_composers_for_repo(ws_dir, repo):
    """Composer ids referenced by workspaces whose folder is (in) the repo."""
    composer_ids = set()
    if not os.path.isdir(ws_dir):
        return composer_ids
    for wj in glob.glob(os.path.join(ws_dir, "*", "workspace.json")):
        try:
            with open(wj, encoding="utf-8") as fh:
                folder = (json.load(fh) or {}).get("folder", "")
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not folder.startswith("file://"):
            continue
        from urllib.parse import unquote, urlparse
        folder_path = unquote(urlparse(folder).path)
        if not (path_inside(folder_path, repo) or path_inside(repo, folder_path)):
            continue
        wdb = os.path.join(os.path.dirname(wj), "state.vscdb")
        if not os.path.exists(wdb):
            continue
        conn, tmp = _sqlite_ro(wdb)
        if conn is None:
            continue
        try:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE key='composer.composerData'"
            ).fetchone()
            if row:
                data = json.loads(row[0])
                for c in data.get("allComposers") or []:
                    if c.get("composerId"):
                        composer_ids.add(c["composerId"])
                for field in ("selectedComposerIds", "lastFocusedComposerIds"):
                    for cid in data.get(field) or []:
                        if isinstance(cid, str):
                            composer_ids.add(cid)
        except (sqlite3.Error, json.JSONDecodeError, ValueError):
            pass
        finally:
            conn.close()
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
    return composer_ids


def _cursor_composers_by_content(conn, repo, since_ts, exclude=frozenset()):
    """Newer Cursor builds keep no composer->workspace map, so match composers
    by whether their conversation blob references paths inside the repo."""
    matched = set()
    repo_real = os.path.realpath(repo)
    needle = re.compile(re.escape(repo_real) + r'["/\\]')
    try:
        row = conn.execute(
            "SELECT value FROM ItemTable WHERE key='composer.composerHeaders'"
        ).fetchone()
        headers = (json.loads(row[0]).get("allComposers") or []) if row else []
    except (sqlite3.Error, json.JSONDecodeError, ValueError):
        headers = []
    since_ms = since_ts * 1000
    for h in headers:
        cid = h.get("composerId")
        if not cid or cid in exclude:
            continue
        updated = h.get("lastUpdatedAt") or h.get("createdAt") or 0
        if updated and updated < since_ms:
            continue
        try:
            row = conn.execute(
                "SELECT value FROM cursorDiskKV WHERE key=?",
                (f"composerData:{cid}",),
            ).fetchone()
        except sqlite3.Error:
            continue
        if row and row[0] and needle.search(row[0]):
            matched.add(cid)
    return matched


def _cursor_composer_models(conn, composer_ids):
    models = {}
    for cid in composer_ids:
        try:
            row = conn.execute(
                "SELECT value FROM cursorDiskKV WHERE key=?",
                (f"composerData:{cid}",),
            ).fetchone()
            if row and row[0]:
                d = json.loads(row[0])
                name = (d.get("modelConfig") or {}).get("modelName")
                if name and name != "default":
                    models[cid] = name
        except (sqlite3.Error, json.JSONDecodeError, ValueError):
            continue
    return models


# ---------------------------------------------------------- local store/refs


def local_store_path(repo):
    return os.path.join(git_dir(repo), LOCAL_DIR, RECORDS_BLOB)


def log_path(repo):
    return os.path.join(git_dir(repo), LOCAL_DIR, "log")


def log_event(repo, message):
    try:
        path = log_path(repo)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{iso(datetime.now(timezone.utc).timestamp())} {message}\n")
    except OSError:
        pass


def load_jsonl(text):
    records = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
    return records


def load_local(repo):
    path = local_store_path(repo)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return load_jsonl(fh.read())


def save_local(repo, records):
    path = local_store_path(repo)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    records = sorted(records, key=lambda r: (r.get("ts") or "", r["id"]))
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, sort_keys=True) + "\n")


def read_ref_records(repo, ref):
    out = git(repo, "cat-file", "blob", f"{ref}:{RECORDS_BLOB}", check=False)
    return load_jsonl(out)


def list_usage_refs(repo):
    out = git(repo, "for-each-ref", "--format=%(refname)", REF_PREFIX + "*", check=False)
    return [line.strip() for line in out.splitlines() if line.strip()]


def origin_urls(repo):
    urls = set()
    for remote in git(repo, "remote", check=False).split():
        for direction in ("", "--push"):
            args = ["remote", "get-url"] + ([direction] if direction else []) + [remote]
            u = git(repo, *args, check=False).strip()
            if u:
                urls.add(norm_remote_url(u))
    urls.discard("")
    return urls


# ------------------------------------------------------------- aggregation


def aggregate(records, branch=None):
    records = dedup(records)
    if branch:
        records = [r for r in records if r.get("branch") == branch]
    return records


def summarize(records):
    """-> {(tool, model): {msgs, sessions:set, input, cache_read, cache_write, output, total}}"""
    groups = {}
    for r in records:
        key = (r.get("tool", "?"), r.get("model", "?"))
        g = groups.setdefault(key, {
            "msgs": 0, "sessions": set(), "input": 0,
            "cache_read": 0, "cache_write": 0, "output": 0, "total": 0,
        })
        g["msgs"] += 1
        if r.get("session"):
            g["sessions"].add(r["session"])
        for f in ("input", "cache_read", "cache_write", "output", "total"):
            g[f] += int(r.get(f, 0) or 0)
    return groups


def fmt_n(n):
    return f"{n:,}"


def render_markdown(records, branch):
    lines = [COMMENT_MARKER, f"## 🤖 AI token usage for `{branch}`", ""]
    if not records:
        lines.append("_No AI agent token usage has been recorded for this branch yet._")
        lines.append("")
        lines.append("Records appear here once contributors run `tokenchecker sync` "
                      "(installed as a `pre-push` hook) on machines where AI sessions ran.")
        return "\n".join(lines) + "\n"
    groups = summarize(records)
    machines = sorted({r.get("machine", "?") for r in records})
    lines.append("| Tool | Model | Sessions | Msgs | Input | Cache read | Cache write | Output | Total |")
    lines.append("|---|---|--:|--:|--:|--:|--:|--:|--:|")
    grand = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0, "total": 0, "msgs": 0}
    grand_sessions = 0
    for (tool, model), g in sorted(groups.items(), key=lambda kv: -kv[1]["total"]):
        lines.append(
            f"| {tool} | `{model}` | {len(g['sessions'])} | {g['msgs']} "
            f"| {fmt_n(g['input'])} | {fmt_n(g['cache_read'])} | {fmt_n(g['cache_write'])} "
            f"| {fmt_n(g['output'])} | **{fmt_n(g['total'])}** |")
        grand_sessions += len(g["sessions"])
        for f in grand:
            grand[f] += g[f] if f != "msgs" else g["msgs"]
    lines.append(
        f"| **All** | | {grand_sessions} | {grand['msgs']} "
        f"| {fmt_n(grand['input'])} | {fmt_n(grand['cache_read'])} | {fmt_n(grand['cache_write'])} "
        f"| {fmt_n(grand['output'])} | **{fmt_n(grand['total'])}** |")
    lines.append("")
    per_machine = {}
    for r in records:
        per_machine[r.get("machine", "?")] = per_machine.get(r.get("machine", "?"), 0) + int(r.get("total", 0))
    if len(machines) > 1:
        lines.append("<details><summary>Per-machine breakdown</summary>")
        lines.append("")
        lines.append("| Machine | Total tokens |")
        lines.append("|---|--:|")
        for m in sorted(per_machine, key=lambda k: -per_machine[k]):
            lines.append(f"| `{m}` | {fmt_n(per_machine[m])} |")
        lines.append("")
        lines.append("</details>")
        lines.append("")
    ts_values = sorted(t for t in (parse_ts(r.get("ts")) for r in records) if t)
    span = ""
    if ts_values:
        span = f" between {iso(ts_values[0])[:10]} and {iso(ts_values[-1])[:10]}"
    lines.append(
        f"_{fmt_n(grand['total'])} tokens across {len(records)} messages from "
        f"{len(machines)} machine(s){span}. Cache reads are counted separately "
        f"from fresh input tokens._")
    return "\n".join(lines) + "\n"


def render_text(records, branch=None):
    if branch:
        header = f"Token usage for branch '{branch}'"
    else:
        header = "Token usage by branch"
    out = [header, "=" * len(header)]
    branches = sorted({r.get("branch", "(unknown)") for r in records})
    if branch:
        branches = [branch]
    for b in branches:
        subset = [r for r in records if r.get("branch") == b]
        if not subset:
            continue
        total = sum(int(r.get("total", 0)) for r in subset)
        if not branch:
            out.append(f"\n{b}: {fmt_n(total)} tokens")
        for (tool, model), g in sorted(summarize(subset).items(), key=lambda kv: -kv[1]["total"]):
            out.append(
                f"  {tool:12s} {model:40s} sessions={len(g['sessions']):<3d} "
                f"in={fmt_n(g['input']):>12s} cache_r={fmt_n(g['cache_read']):>13s} "
                f"cache_w={fmt_n(g['cache_write']):>12s} out={fmt_n(g['output']):>11s} "
                f"total={fmt_n(g['total']):>13s}")
        if branch:
            out.append(f"  {'TOTAL':12s} {'':40s} {'':13s} "
                       f"{'':16s} {'':21s} {'':20s} total={fmt_n(total):>13s}")
    if len(out) <= 2:
        out.append("(no records)")
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------- commands


def cmd_collect(args):
    repo = repo_root(args.repo)
    if not repo:
        eprint("tokenchecker: not inside a git repository")
        return 1
    since_ts = (datetime.now(timezone.utc) - timedelta(days=args.since)).timestamp()
    timeline = BranchTimeline(repo)
    urls = origin_urls(repo)
    collected = []
    sources = {
        "claude-code": lambda: collect_claude(repo, since_ts, timeline),
        "codex": lambda: collect_codex(repo, since_ts, timeline, urls),
        "gemini": lambda: collect_gemini(repo, since_ts, timeline),
        "cursor": lambda: collect_cursor(repo, since_ts, timeline),
    }
    counts = {}
    for name, fn in sources.items():
        try:
            recs = fn()
        except Exception as exc:  # a broken source must not block the others
            if not args.quiet:
                eprint(f"tokenchecker: {name} collector failed: {exc}")
            recs = []
        counts[name] = len(recs)
        collected.extend(recs)
    if args.dry_run:
        print(render_text(dedup(collected)))
        if not args.quiet:
            eprint("dry run — nothing written. per-source records: "
                   + ", ".join(f"{k}={v}" for k, v in counts.items()))
        return 0
    existing = load_local(repo)
    merged = dedup(existing + collected)
    save_local(repo, merged)
    new = len(merged) - len(dedup(existing))
    summary = (f"collected {len(collected)} records "
               f"({', '.join(f'{k}={v}' for k, v in counts.items())}); "
               f"{new} new; store has {len(merged)}")
    log_event(repo, "collect: " + summary)
    if not args.quiet:
        print(f"tokenchecker: {summary}")
    return 0


def cmd_push(args):
    repo = repo_root(args.repo)
    if not repo:
        eprint("tokenchecker: not inside a git repository")
        return 1
    records = load_local(repo)
    if not records:
        if not args.quiet:
            print("tokenchecker: no local records to push")
        return 0
    ref = REF_PREFIX + machine_id()
    merged = dedup(read_ref_records(repo, ref) + records)
    payload = "".join(
        json.dumps(r, sort_keys=True) + "\n"
        for r in sorted(merged, key=lambda r: (r.get("ts") or "", r["id"]))
    )
    blob = git(repo, "hash-object", "-w", "--stdin", input_=payload).strip()
    tree = git(repo, "mktree", input_=f"100644 blob {blob}\t{RECORDS_BLOB}\n").strip()
    commit = git(repo, "commit-tree", tree, "-m",
                 f"tokenchecker records from {machine_id()}").strip()
    git(repo, "update-ref", ref, commit)
    remote = args.remote
    p = subprocess.run(
        ["git", "-C", repo, "push", "--force", "--no-verify", remote, f"{ref}:{ref}"],
        env={**os.environ, "TOKENCHECKER_SKIP": "1"},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        log_event(repo, f"push FAILED to {remote} {ref}: {p.stderr.strip().splitlines()[-1] if p.stderr.strip() else 'unknown error'}")
        eprint(f"tokenchecker: push of {ref} to {remote} failed:\n{p.stderr.strip()}")
        return 1
    log_event(repo, f"push: {len(merged)} records -> {remote} {ref}")
    if not args.quiet:
        print(f"tokenchecker: pushed {len(merged)} records to {remote} {ref}")
    return 0


def cmd_sync(args):
    rc = cmd_collect(args)
    if rc != 0:
        return rc
    return cmd_push(args)


def cmd_report(args):
    repo = repo_root(args.repo)
    if not repo:
        eprint("tokenchecker: not inside a git repository")
        return 1
    records = [] if args.refs_only else load_local(repo)
    for ref in list_usage_refs(repo):
        records.extend(read_ref_records(repo, ref))
    records = aggregate(records, branch=args.branch)
    if args.json:
        print(json.dumps(records, indent=2))
    elif args.markdown:
        print(render_markdown(records, args.branch or "(all branches)"), end="")
    else:
        print(render_text(records, args.branch), end="")
    return 0


def cmd_status(args):
    repo = repo_root(args.repo)
    if not repo:
        eprint("tokenchecker: not inside a git repository")
        return 1
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
    print(f"repo:         {repo}" + (f" (branch {branch})" if branch else ""))
    hook = os.path.join(git(repo, "rev-parse", "--git-path", "hooks").strip(), "pre-push")
    if not os.path.isabs(hook):
        hook = os.path.join(repo, hook)
    installed = os.path.exists(hook) and PRE_PUSH_MARKER in open(
        hook, encoding="utf-8", errors="replace").read()
    print(f"pre-push hook: {'installed' if installed else 'NOT installed — run: python3 tokenchecker.py install'}")
    print(f"machine id:   {machine_id()}")
    store = load_local(repo)
    if store:
        branches = sorted({r.get("branch", "?") for r in store})
        last_ts = max((r.get("ts") or "" for r in store), default="")
        print(f"local store:  {len(store)} records, {len(branches)} branch(es), "
              f"newest record {last_ts or 'n/a'}")
    else:
        print("local store:  empty (run `sync` or `collect`, or just `git push`)")
    lp = log_path(repo)
    if os.path.exists(lp):
        with open(lp, encoding="utf-8", errors="replace") as fh:
            tail = fh.read().splitlines()[-args.log_lines:]
        print(f"\nrecent runs (last {len(tail)} of {lp}):")
        for line in tail:
            print(f"  {line}")
    else:
        print("\nrecent runs:  none logged yet")
    out = git(repo, "for-each-ref",
              "--format=%(refname)|%(committerdate:iso8601)|%(committerdate:relative)",
              REF_PREFIX + "*", check=False)
    rows = [line.split("|") for line in out.splitlines() if line.strip()]
    print("\nsynced machines (local refs):")
    if rows:
        for refname, date, rel in rows:
            n = len(read_ref_records(repo, refname))
            print(f"  {refname[len(REF_PREFIX):]:24s} {n:6d} records   last push {date} ({rel})")
    else:
        print("  none fetched — try: git fetch", args.remote,
              f"'+{REF_PREFIX}*:{REF_PREFIX}*'")
    ls = run(["git", "-C", repo, "ls-remote", args.remote, REF_PREFIX + "*"], check=False)
    remote_refs = [line.split("\t")[1] for line in ls.splitlines() if "\t" in line]
    if remote_refs:
        print(f"\non {args.remote}: " + ", ".join(
            r[len(REF_PREFIX):] for r in remote_refs))
    return 0


PRE_PUSH_MARKER = "# >>> tokenchecker pre-push >>>"
PRE_PUSH_END_MARKER = "# <<< tokenchecker pre-push <<<"
PRE_PUSH_BLOCK = """
# >>> tokenchecker pre-push >>>
# Collect local AI agent token usage and publish it to refs/token-usage/<machine>
if [ -z "$TOKENCHECKER_SKIP" ]; then
  _tc_root="$(git rev-parse --show-toplevel 2>/dev/null)"
  if [ -n "$_tc_root" ] && [ -f "$_tc_root/scripts/tokenchecker.py" ]; then
    TOKENCHECKER_SKIP=1 python3 "$_tc_root/scripts/tokenchecker.py" sync || true
  fi
fi
# <<< tokenchecker pre-push <<<
"""

WORKFLOW_PATH = ".github/workflows/token-usage.yml"
WORKFLOW_YAML = """\
name: AI Token Usage

on:
  pull_request:
    types: [opened, reopened, synchronize]

permissions:
  contents: read
  pull-requests: write

jobs:
  token-usage:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Fetch token-usage refs
        run: git fetch origin '+refs/token-usage/*:refs/token-usage/*' || true

      - name: Build report
        run: |
          python3 scripts/tokenchecker.py report \\
            --branch "${{ github.head_ref }}" \\
            --refs-only --markdown > tokenchecker-report.md
          cat tokenchecker-report.md
          cat tokenchecker-report.md >> "$GITHUB_STEP_SUMMARY"

      - name: Upsert PR comment
        uses: actions/github-script@v7
        with:
          script: |
            const fs = require('fs');
            const body = fs.readFileSync('tokenchecker-report.md', 'utf8');
            const marker = '<!-- tokenchecker-report -->';
            const { data: comments } = await github.rest.issues.listComments({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.issue.number,
              per_page: 100,
            });
            const existing = comments.find(c => c.body && c.body.includes(marker));
            if (existing) {
              await github.rest.issues.updateComment({
                owner: context.repo.owner,
                repo: context.repo.repo,
                comment_id: existing.id,
                body,
              });
            } else {
              await github.rest.issues.createComment({
                owner: context.repo.owner,
                repo: context.repo.repo,
                issue_number: context.issue.number,
                body,
              });
            }
"""

CLAUDE_HOOK_COMMAND = ("python3 \"$CLAUDE_PROJECT_DIR/scripts/tokenchecker.py\" "
                       "sync --quiet >/dev/null 2>&1 || true")


def cmd_install(args):
    repo = repo_root(args.repo)
    if not repo:
        eprint("tokenchecker: not inside a git repository")
        return 1
    changed = []

    # 1. vendor this script at scripts/tokenchecker.py
    dst = os.path.join(repo, "scripts", "tokenchecker.py")
    src = os.path.realpath(__file__)
    if os.path.realpath(dst) != src:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        os.chmod(dst, 0o755)
        changed.append("scripts/tokenchecker.py")

    # 2. pre-push hook
    hooks_dir = git(repo, "rev-parse", "--git-path", "hooks").strip()
    if not os.path.isabs(hooks_dir):
        hooks_dir = os.path.join(repo, hooks_dir)
    os.makedirs(hooks_dir, exist_ok=True)
    hook_path = os.path.join(hooks_dir, "pre-push")
    existing = ""
    if os.path.exists(hook_path):
        with open(hook_path, encoding="utf-8", errors="replace") as fh:
            existing = fh.read()
    if PRE_PUSH_MARKER in existing:
        # replace any stale block with the current one
        pattern = re.compile(
            re.escape(PRE_PUSH_MARKER) + r".*?" + re.escape(PRE_PUSH_END_MARKER) + r"\n?",
            re.DOTALL)
        updated = pattern.sub(PRE_PUSH_BLOCK.strip() + "\n", existing)
        if updated != existing:
            with open(hook_path, "w", encoding="utf-8") as fh:
                fh.write(updated)
            os.chmod(hook_path, 0o755)
            changed.append(os.path.relpath(hook_path, repo) + " (hook block updated)")
    else:
        content = existing if existing.strip() else "#!/bin/sh\n"
        with open(hook_path, "w", encoding="utf-8") as fh:
            fh.write(content.rstrip("\n") + "\n" + PRE_PUSH_BLOCK)
        os.chmod(hook_path, 0o755)
        changed.append(os.path.relpath(hook_path, repo) + " (local, not committed)")

    # 3. GitHub workflow
    wf_path = os.path.join(repo, WORKFLOW_PATH)
    if not os.path.exists(wf_path):
        os.makedirs(os.path.dirname(wf_path), exist_ok=True)
        with open(wf_path, "w", encoding="utf-8") as fh:
            fh.write(WORKFLOW_YAML)
        changed.append(WORKFLOW_PATH)

    # 4. optional Claude Code SessionEnd hook (project settings)
    if args.claude_hook:
        settings_path = os.path.join(repo, ".claude", "settings.json")
        settings = {}
        if os.path.exists(settings_path):
            try:
                with open(settings_path, encoding="utf-8") as fh:
                    settings = json.load(fh)
            except (json.JSONDecodeError, ValueError):
                eprint(f"tokenchecker: {settings_path} is not valid JSON; skipping Claude hook")
                settings = None
        if settings is not None:
            hooks = settings.setdefault("hooks", {})
            session_end = hooks.setdefault("SessionEnd", [])
            already = any(
                CLAUDE_HOOK_COMMAND in json.dumps(entry) for entry in session_end)
            if not already:
                session_end.append(
                    {"hooks": [{"type": "command", "command": CLAUDE_HOOK_COMMAND}]})
                os.makedirs(os.path.dirname(settings_path), exist_ok=True)
                with open(settings_path, "w", encoding="utf-8") as fh:
                    json.dump(settings, fh, indent=2)
                    fh.write("\n")
                changed.append(".claude/settings.json")

    if changed:
        print("tokenchecker installed. Created/updated:")
        for c in changed:
            print(f"  - {c}")
    else:
        print("tokenchecker: already installed, nothing to do")
    print("\nNext steps:")
    print("  1. Commit scripts/tokenchecker.py and .github/workflows/token-usage.yml")
    print("  2. Every contributor runs: python3 scripts/tokenchecker.py install")
    print("     (sets up their local pre-push hook)")
    print("  3. Token usage syncs automatically on every git push;")
    print("     run `python3 scripts/tokenchecker.py sync` to publish manually.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tokenchecker", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=None, help="path inside the target repo (default: cwd)")
    sub = ap.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--since", type=int, default=DEFAULT_SINCE_DAYS,
                       help=f"look back N days (default {DEFAULT_SINCE_DAYS})")
        p.add_argument("--remote", default="origin")

    p = sub.add_parser("collect", help="scan local agent logs into the local store")
    common(p)
    p.add_argument("--dry-run", action="store_true", help="print what would be stored")
    p.set_defaults(fn=cmd_collect)

    p = sub.add_parser("push", help="publish local records to refs/token-usage/<machine>")
    common(p)
    p.set_defaults(fn=cmd_push)

    p = sub.add_parser("sync", help="collect + push")
    common(p)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("report", help="aggregate and print usage")
    common(p)
    p.add_argument("--branch", default=None, help="restrict to one branch")
    p.add_argument("--markdown", action="store_true", help="PR-comment markdown output")
    p.add_argument("--json", action="store_true", help="raw records as JSON")
    p.add_argument("--refs-only", dest="refs_only", action="store_true",
                   help="ignore the local store; use only synced refs (for CI)")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("status", help="show when tokenchecker last ran and what is synced")
    common(p)
    p.add_argument("--log-lines", type=int, default=10, help="log lines to show (default 10)")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("install", help="vendor script, add pre-push hook + workflow")
    common(p)
    p.add_argument("--claude-hook", action="store_true",
                   help="also add a Claude Code SessionEnd hook to .claude/settings.json")
    p.set_defaults(fn=cmd_install)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
