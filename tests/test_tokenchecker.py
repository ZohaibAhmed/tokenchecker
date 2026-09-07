"""Offline regression tests. All stores, hooks and remotes are disposable."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

import tokenchecker as tc


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        environment = {k: v for k, v in os.environ.items()
                       if not k.startswith(('GIT_', 'TOKENCHECKER_'))}
        environment.update(HOME=str(self.root / 'home'),
                           GIT_CONFIG_NOSYSTEM='1',
                           GIT_CONFIG_GLOBAL=str(self.root / 'gitconfig'),
                           TOKENCHECKER_HOME=str(self.root / 'tc'),
                           TOKENCHECKER_MACHINE_ID='test-machine',
                           TOKENCHECKER_NO_NETWORK='1')
        for key in ('CLAUDE_DIR', 'CODEX_DIR', 'GEMINI_DIR', 'CURSOR_WS_DIR', 'CURSOR_GLOBAL_DB'):
            environment['TOKENCHECKER_' + key] = str(self.root / key)
        self.env = mock.patch.dict(os.environ, environment, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.repo = self.new_repo('repo')
        self.args = argparse.Namespace(repo=str(self.repo), remote='origin', since=90,
                                       quiet=True, hook=False, dry_run=False)

    def git(self, repo, *args):
        return tc.git(str(repo), *args)

    def new_repo(self, name):
        path = self.root / name
        self.git(self.root, 'init', '-q', '-b', 'main', str(path))
        self.git(path, 'config', 'user.name', 'Test')
        self.git(path, 'config', 'user.email', 'test@example.invalid')
        self.git(path, 'commit', '--allow-empty', '-qm', 'initial')
        return path

    def record(self, rid='one', total=10):
        return tc.make_record(rid, 'test', 'model', 1700000000, 'main', 'session',
                              input_t=total)

    def remote(self, name='origin', repo=None):
        remote = self.root / (name + '.git')
        self.git(self.root, 'init', '--bare', '-q', str(remote))
        self.git(repo or self.repo, 'remote', 'add', name, str(remote))
        return remote

    def write_jsonl(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))

    def test_sync_dry_run_never_pushes_or_comments(self):
        self.args.dry_run = True
        self.args.hook = True
        with mock.patch.object(tc, 'cmd_collect', return_value=0), \
             mock.patch.object(tc, 'cmd_push') as push, \
             mock.patch.object(tc, '_maybe_comment_from_hook') as comment:
            self.assertEqual(tc.cmd_sync(self.args), 0)
            push.assert_not_called()
            comment.assert_not_called()

    def test_collect_dry_run_does_not_write_price_cache(self):
        self.args.dry_run = True
        os.environ.pop('TOKENCHECKER_NO_NETWORK')
        with mock.patch.object(tc, 'fetch_live_prices', return_value={'m': {'input': 1, 'output': 2}}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tc.cmd_collect(self.args), 0)
        self.assertFalse(Path(tc.prices_cache_path()).exists())
        self.assertFalse(Path(tc.local_store_path(str(self.repo))).exists())

    def test_push_to_second_remote_is_not_skipped(self):
        self.remote()
        second = self.remote('second')
        tc.save_local(str(self.repo), [self.record()])
        self.assertEqual(tc.cmd_push(self.args), 0)
        self.args.remote = 'second'
        self.assertEqual(tc.cmd_push(self.args), 0)
        self.assertEqual(len(tc.read_ref_records(str(second), tc.REF_PREFIX + tc.machine_id())), 1)

    def test_push_preserves_same_machine_records_from_other_clone(self):
        remote = self.remote()
        other = self.new_repo('other')
        self.git(other, 'remote', 'add', 'origin', str(remote))
        tc.save_local(str(self.repo), [self.record('first')])
        tc.cmd_push(self.args)
        tc.save_local(str(other), [self.record('second')])
        self.args.repo = str(other)
        tc.cmd_push(self.args)
        rows = tc.read_ref_records(str(remote), tc.REF_PREFIX + tc.machine_id())
        self.assertEqual({r['id'] for r in rows}, {'first', 'second'})

    def test_push_restores_deleted_remote_ref(self):
        remote = self.remote()
        tc.save_local(str(self.repo), [self.record()])
        tc.cmd_push(self.args)
        ref = tc.REF_PREFIX + tc.machine_id()
        self.git(remote, 'update-ref', '-d', ref)
        self.assertEqual(tc.cmd_push(self.args), 0)
        self.assertEqual(len(tc.read_ref_records(str(remote), ref)), 1)

    def test_push_unchanged_does_not_create_new_commit(self):
        remote = self.remote()
        tc.save_local(str(self.repo), [self.record()])
        tc.cmd_push(self.args)
        ref = tc.REF_PREFIX + tc.machine_id()
        before = self.git(remote, 'rev-parse', ref)
        tc.cmd_push(self.args)
        self.assertEqual(self.git(remote, 'rev-parse', ref), before)

    def test_push_rejects_concurrent_remote_update(self):
        remote = self.remote()
        tc.save_local(str(self.repo), [self.record()])
        tc.cmd_push(self.args)
        ref = tc.REF_PREFIX + tc.machine_id()
        previous = self.git(remote, 'rev-parse', ref).strip()
        tree = self.git(self.repo, 'rev-parse', previous + '^{tree}').strip()
        concurrent = self.git(self.repo, 'commit-tree', tree, '-m', 'concurrent').strip()
        tc.save_local(str(self.repo), [self.record(total=20)])
        original_run = subprocess.run
        def racing_run(cmd, **kwargs):
            if isinstance(cmd, list) and 'push' in cmd and any('--force-with-lease=' in a for a in cmd):
                original_run(['git', '-C', str(self.repo), 'push', '--no-verify', '--force',
                              'origin', f'{concurrent}:{ref}'], check=True, capture_output=True)
            return original_run(cmd, **kwargs)
        with mock.patch.object(subprocess, 'run', side_effect=racing_run), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tc.cmd_push(self.args), 1)
        self.assertEqual(self.git(remote, 'rev-parse', ref).strip(), concurrent)

    def test_same_second_checkouts_preserve_reflog_order(self):
        reflog = 'HEAD@{100}|checkout: moving from z to a\nHEAD@{100}|checkout: moving from main to z\n'
        with mock.patch.object(tc, 'git', side_effect=['a\n', reflog]):
            timeline = tc.BranchTimeline(str(self.repo))
        self.assertEqual(timeline.branch_at(100), 'a')
        self.assertEqual(timeline.branch_at(99), 'main')

    def test_codex_fallback_uses_session_start_branch(self):
        path = self.root / 'codex.jsonl'
        self.write_jsonl(path, [
            {'type': 'session_meta', 'payload': {'id': 's', 'cwd': str(self.repo), 'timestamp': 100}},
            {'type': 'event_msg', 'timestamp': 200, 'payload': {'type': 'token_count', 'info': {
                'total_token_usage': {'input_tokens': 10, 'total_tokens': 10}}}},
        ])
        timeline = mock.Mock()
        timeline.branch_at.side_effect = lambda ts: 'start' if ts == 100 else 'end'
        self.assertEqual(tc._parse_codex_file(str(path), str(self.repo), 0, timeline, set())['branch'], 'start')

    def test_claude_collects_nested_subagent_usage(self):
        project = tc.re.sub(r'[^A-Za-z0-9-]', '-', os.path.realpath(self.repo))
        path = Path(tc.claude_projects_dir()) / project / 'session' / 'subagents' / 'agent.jsonl'
        self.write_jsonl(path, [{'type': 'assistant', 'cwd': str(self.repo), 'sessionId': 's',
                                'gitBranch': 'main', 'timestamp': 100,
                                'message': {'id': 'm', 'model': 'm', 'usage': {'input_tokens': 10}}}])
        records = tc.collect_claude(str(self.repo), 0, mock.Mock())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['total'], 10)

    def test_parent_cursor_workspace_not_assigned_to_every_child_repo(self):
        ws = Path(tc.cursor_workspace_dir()) / 'workspace'
        ws.mkdir(parents=True)
        (ws / 'workspace.json').write_text(json.dumps({'folder': self.root.as_uri()}))
        with contextlib.closing(sqlite3.connect(ws / 'state.vscdb')) as db:
            db.execute('CREATE TABLE ItemTable (key TEXT, value TEXT)')
            db.execute('INSERT INTO ItemTable VALUES (?, ?)', ('composer.composerData',
                       json.dumps({'allComposers': [{'composerId': 'unrelated'}]})))
            db.commit()
        self.assertEqual(tc._cursor_composers_for_repo(tc.cursor_workspace_dir(), str(self.repo)), set())

    def test_claude_hook_install_is_idempotent(self):
        tc._install_claude_hook(str(self.repo), [])
        tc._install_claude_hook(str(self.repo), [])
        settings = json.loads((self.repo / '.claude/settings.json').read_text())
        self.assertEqual(len(settings['hooks']['SessionEnd']), 1)

    def test_malformed_prices_fall_back_without_crashing(self):
        cache = Path(tc.prices_cache_path())
        cache.parent.mkdir()
        for data in ([], ['x'], {'date': 'bad', 'prices': {'m': {'input': 1, 'output': 2}}},
                     {'date': '2026-01-01'}, {'date': '2026-01-01', 'prices': {'m': 'bad'}}):
            with self.subTest(data=data):
                cache.write_text(json.dumps(data))
                self.assertEqual(tc.resolve_prices()[1], 'built-in')

    def test_invalid_custom_prices_fall_back(self):
        override = self.root / 'prices.json'
        os.environ['TOKENCHECKER_PRICES'] = str(override)
        for data in ([1], {'m': {'input': 'bad', 'output': 2}}, {'prices': None},
                     {'m': {'input_cost_per_token': 1, 'output_cost_per_token': 1}}):
            with self.subTest(data=data), contextlib.redirect_stderr(io.StringIO()):
                override.write_text(json.dumps(data))
                self.assertEqual(tc.resolve_prices()[1], 'built-in')

    def install_hook(self, existing=None):
        hook = Path(tc.git_dir(str(self.repo))) / 'hooks' / 'pre-push'
        if existing is not None:
            hook.write_text(existing)
            hook.chmod(0o755)
        tc._install_repo_hook(str(self.repo), [])
        # A harmless local recorder stands in for the sync command.
        script = self.repo / 'scripts/tokenchecker.py'
        script.parent.mkdir(exist_ok=True)
        script.write_text("import pathlib, sys\npathlib.Path('synced').write_text(' '.join(sys.argv[1:]))\n")
        return hook

    def execute_hook(self, hook):
        return subprocess.run([str(hook), 'second', '/remote'], cwd=self.repo,
                              input='ref-data\n', capture_output=True, text=True)

    def test_existing_exit_zero_hook_still_syncs(self):
        hook = self.install_hook('#!/bin/sh\ncat > original-input\nexit 0\n')
        self.assertEqual(self.execute_hook(hook).returncode, 0)
        self.assertTrue((self.repo / 'synced').exists())
        self.assertEqual((self.repo / 'original-input').read_text(), 'ref-data\n')
        tc._install_repo_hook(str(self.repo), [])
        self.assertEqual(self.execute_hook(hook).returncode, 0)

    def test_existing_python_hook_still_runs(self):
        hook = self.install_hook('#!/usr/bin/env python3\nfrom pathlib import Path\nPath("original-ran").touch()\n')
        self.assertEqual(self.execute_hook(hook).returncode, 0)
        self.assertTrue((self.repo / 'original-ran').exists())
        self.assertTrue((self.repo / 'synced').exists())

    def test_existing_failing_hook_still_blocks_push(self):
        hook = self.install_hook('#!/bin/sh\nexit 7\n')
        self.assertEqual(self.execute_hook(hook).returncode, 7)
        self.assertFalse((self.repo / 'synced').exists())

    def test_hook_uses_actual_push_remote(self):
        hook = self.install_hook()
        self.execute_hook(hook)
        self.assertIn('--remote second', (self.repo / 'synced').read_text())

    def test_repo_opt_out_is_respected(self):
        hook = self.install_hook()
        self.git(self.repo, 'config', 'tokenchecker.enabled', 'false')
        self.execute_hook(hook)
        self.assertFalse((self.repo / 'synced').exists())

    def test_worktree_hook_installs_in_shared_hooks_dir(self):
        worktree = self.root / 'worktree'
        self.git(self.repo, 'worktree', 'add', '-qb', 'feature', str(worktree))
        tc._install_repo_hook(str(worktree), [])
        actual = self.git(worktree, 'rev-parse', '--git-path', 'hooks/pre-push').strip()
        self.assertIn(tc.PRE_PUSH_MARKER, Path(actual).read_text())

    def test_global_dispatcher_chains_worktree_hooks_and_syncs_once(self):
        hook = self.install_hook('#!/bin/sh\nprintf x >> original-runs\nexit 0\n')
        worktree = self.root / 'worktree'
        self.git(self.repo, 'worktree', 'add', '-qb', 'feature', str(worktree))
        script = self.root / 'fake.py'
        script.write_text("from pathlib import Path\nimport sys\nPath('global-synced').write_text(' '.join(sys.argv[1:]))\n")
        dispatcher = self.root / 'global' / 'pre-push'
        dispatcher.parent.mkdir()
        dispatcher.write_text(tc.DISPATCHER_TEMPLATE.format(marker=tc.DISPATCHER_MARKER,
                                                             script=shlex.quote(str(script))))
        dispatcher.chmod(0o755)
        result = subprocess.run([str(dispatcher), 'second', '/remote'], cwd=worktree, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((worktree / 'original-runs').read_text(), 'x')
        self.assertIn('--remote second', (worktree / 'global-synced').read_text())

    def test_atomic_store_keeps_previous_data_on_write_failure(self):
        tc.save_local(str(self.repo), [self.record()])
        with mock.patch.object(tc.os, 'replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                tc.save_local(str(self.repo), [self.record('new')])
        self.assertEqual([r['id'] for r in tc.load_local(str(self.repo))], ['one'])

    def test_malformed_usage_records_do_not_break_reporting(self):
        invalid = [None, [], 42, {"id": []}, {"id": "bad", "total": "ten"},
                   {"id": "bad", "total": -1}, {"id": "bad", "total": 1, "model": []}]
        data = "\n".join(json.dumps(x) for x in invalid + [self.record()])
        rows = tc.load_jsonl(data)
        self.assertEqual(len(rows), 1)
        self.assertEqual(tc.summarize(tc.aggregate(rows))[('test', 'model')]['total'], 10)

    def test_malformed_claude_event_does_not_discard_good_events(self):
        path = self.root / 'claude.jsonl'
        good = {'type': 'assistant', 'cwd': str(self.repo), 'sessionId': 's',
                'gitBranch': 'main', 'message': {'id': 'm', 'model': 'm',
                                                'usage': {'input_tokens': 10}}}
        self.write_jsonl(path, [None, [], {'type': 'assistant', 'message': 'bad'},
                               {'type': 'assistant', 'message': {'usage': {'input_tokens': 'bad'}}}, good])
        rows = tc._parse_claude_file(str(path), str(self.repo), 0, mock.Mock())
        self.assertEqual(len(rows), 1)

    def test_malformed_codex_event_does_not_discard_good_events(self):
        path = self.root / 'codex.jsonl'
        self.write_jsonl(path, [None, {'type': 'session_meta', 'payload': {
            'id': 's', 'cwd': str(self.repo), 'git': {'branch': 'main'}}},
            {'type': 'event_msg', 'payload': 'bad'},
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': {
                'input_tokens': 'bad', 'total_tokens': 'bad'}}}},
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': {
                'input_tokens': 10, 'total_tokens': 10}}}}])
        record = tc._parse_codex_file(str(path), str(self.repo), 0, mock.Mock(), set())
        self.assertEqual(record['total'], 10)

    def test_invalid_timestamps_are_ignored(self):
        for ts in (float('nan'), float('inf'), 10**1000):
            with self.subTest(ts=str(ts)[:30]):
                self.assertIsNone(tc.parse_ts(ts))

    def test_install_global_handles_shell_characters_in_home_path(self):
        os.environ['TOKENCHECKER_HOME'] = str(self.root / 'home $(touch injected)')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tc.cmd_install_global(self.args), 0)
        wrapper = Path(tc.tc_home()) / 'bin/tokenchecker'
        result = subprocess.run([str(wrapper), '--version'], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(tc.__version__, result.stdout)
        self.assertFalse((self.repo / 'injected').exists())

    def test_install_upgrades_stock_workflow_and_preserves_custom_workflow(self):
        self.args.vendor = False
        self.args.claude_hook = False
        workflow = self.repo / tc.WORKFLOW_PATH
        workflow.parent.mkdir(parents=True)
        old = tc.WORKFLOW_YAML.replace(
            '    if: github.event.pull_request.head.repo.full_name == github.repository\n', '')
        workflow.write_text(old)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tc.cmd_install(self.args), 0)
        self.assertEqual(workflow.read_text(), tc.WORKFLOW_YAML)
        custom = '# user customization\n' + old
        workflow.write_text(custom)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tc.cmd_install(self.args), 0)
        self.assertEqual(workflow.read_text(), custom)

    def test_install_vendor_upgrades_stock_workflow(self):
        self.args.vendor = True
        self.args.claude_hook = False
        workflow = self.repo / tc.WORKFLOW_PATH
        workflow.parent.mkdir(parents=True)
        old = tc.WORKFLOW_YAML_VENDORED.replace(
            '    if: github.event.pull_request.head.repo.full_name == github.repository\n', '')
        old = old.replace('        with:\n          ref: ${{ github.event.pull_request.base.sha }}\n', '')
        old = old.replace('        env:\n          TOKENCHECKER_BRANCH: ${{ github.head_ref }}\n', '')
        old = old.replace('$TOKENCHECKER_BRANCH', '${{ github.head_ref }}')
        old = old.replace('const comments = await github.paginate(github.rest.issues.listComments, {',
                          'const { data: comments } = await github.rest.issues.listComments({')
        workflow.write_text(old)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tc.cmd_install(self.args), 0)
        self.assertEqual(workflow.read_text(), tc.WORKFLOW_YAML_VENDORED)
        self.assertTrue((self.repo / 'scripts/tokenchecker.py').exists())

    def test_concurrent_collectors_preserve_both_results(self):
        import sys
        import time
        code = """
import argparse, time, sys
from pathlib import Path
from unittest import mock
import tokenchecker as tc
repo, rid, signal = sys.argv[1:]
args = argparse.Namespace(repo=repo, remote='origin', since=90, quiet=True, hook=False, dry_run=False)
original = tc.load_local
def slow_load(repo):
    rows = original(repo)
    if rid == 'first':
        Path(signal).touch()
        time.sleep(0.5)
    return rows
record = tc.make_record(rid, 'test', 'model', 1700000000, 'main', 's', input_t=10)
with mock.patch.object(tc, 'collect_claude', return_value=[record]), mock.patch.object(tc, 'load_local', slow_load):
    sys.exit(tc.cmd_collect(args))
"""
        signal = self.root / 'loaded'
        first = subprocess.Popen([sys.executable, '-c', code, str(self.repo), 'first', str(signal)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: first.poll() is None and first.kill())
        deadline = time.monotonic() + 10
        while not signal.exists() and first.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(signal.exists())
        second = subprocess.run([sys.executable, '-c', code, str(self.repo), 'second', str(signal)],
                                capture_output=True, timeout=15)
        _, errors = first.communicate(timeout=15)
        self.assertEqual(first.returncode, 0, errors)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual({r['id'] for r in tc.load_local(str(self.repo))}, {'first', 'second'})

    def test_comment_lookup_failure_does_not_create_duplicate(self):
        calls = []
        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if kwargs.get('check', True):
                raise RuntimeError('temporary API error')
            return ''
        with mock.patch.object(tc, 'run', side_effect=fake_run):
            with self.assertRaises(RuntimeError):
                tc.upsert_pr_comment(str(self.repo), 1, 'report')
        self.assertEqual(len(calls), 1)

    def test_unchanged_prices_do_not_rewrite_table_just_to_change_date(self):
        from scripts import update_prices
        prices = {f'gpt-5-model-{i}': {'input': 1, 'output': 2} for i in range(50)}
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{}'
        with mock.patch.object(update_prices.urllib.request, 'urlopen', return_value=response), \
             mock.patch.object(tc, 'parse_litellm_prices', return_value=prices), \
             mock.patch.object(tc, 'EMBEDDED_PRICES', prices), \
             mock.patch('builtins.open') as file_open, \
             contextlib.redirect_stdout(io.StringIO()):
            update_prices.main()
        file_open.assert_not_called()

    def test_vendored_workflow_branch_text_is_not_executed(self):
        script = tc.WORKFLOW_YAML_VENDORED.split('        run: |\n', 1)[1].split(
            '\n      - name: Upsert PR comment', 1)[0]
        script = '\n'.join(line[10:] for line in script.splitlines())
        branch = 'feature/$(touch injected)'
        script = script.replace('${{ github.head_ref }}', branch)
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        stub = bin_dir / 'python3'
        stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        stub.chmod(0o755)
        env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'],
                   TOKENCHECKER_BRANCH=branch, GITHUB_STEP_SUMMARY=str(self.root / 'summary'))
        result = subprocess.run(['sh', '-c', script], cwd=self.repo, env=env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.repo / 'injected').exists())
        self.assertIn(branch, (self.repo / 'tokenchecker-report.md').read_text())

    def test_action_branch_text_is_not_executed_as_shell(self):
        action = (Path(__file__).resolve().parents[1] / 'action.yml').read_text()
        script = action.split('      run: |\n', 1)[1]
        script = '\n'.join(line[8:] for line in script.splitlines())
        branch = 'feature/$(touch injected)'
        script = script.replace('${{ github.head_ref }}', branch)
        script = script.replace('${{ inputs.version }}', 'tokenchecker')
        script = script.replace('${{ github.event.pull_request.number }}', '1')
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        for name in ('python3', 'tokenchecker'):
            stub = bin_dir / name
            stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> arguments\n')
            stub.chmod(0o755)
        env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'],
                   TOKENCHECKER_BRANCH=branch, TOKENCHECKER_REQUIREMENT='tokenchecker', TOKENCHECKER_PR='1')
        result = subprocess.run(['sh', '-c', script], cwd=self.repo, env=env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.repo / 'injected').exists())
        self.assertIn(branch, (self.repo / 'arguments').read_text())


if __name__ == '__main__':
    unittest.main()
