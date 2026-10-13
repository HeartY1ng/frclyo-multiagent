# 跨模块离线测试：假 HOME、假 CLI、假工人，不调用模型、钥匙串或 launchd。
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
CHOICE = {'worker': 'codex', 'model': 'offline-model', 'level': 'medium'}


def load_aw(path):
    """加载 bash/Python 双入口；不运行 main，方便在公共边界注入离线替身。"""
    loader = importlib.machinery.SourceFileLoader('aw_test_module', str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def snapshot(home):
    """同时比较目录、普通文件字节和链接目标，不能只数文件个数。"""
    result = {}
    for path in home.rglob('*'):
        relative = str(path.relative_to(home))
        result[relative] = ('link', os.readlink(path)) if path.is_symlink() else ('directory',) if path.is_dir() else ('file', path.read_bytes())
    return result


class AwTests(unittest.TestCase):
    """真实进程用于工人执行；公共库使用真实实现，系统命令和模型执行使用离线替身。"""
    def setUp(self):
        # 临时文件都留在本轮交付树，测试完成自动清理。
        self.temp = tempfile.TemporaryDirectory(prefix='.aw-test-', dir=SOURCE)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home, self.repo = self.base / 'home', self.base / 'repo space & 中文'
        self.home.mkdir()
        self.repo.mkdir()
        for name in ('install.sh', 'uninstall.sh', 'bin', 'demo', 'tests'):
            source, target = SOURCE / name, self.repo / name
            shutil.copytree(source, target) if source.is_dir() else shutil.copy2(source, target)
        fixture = self.repo / 'tests/fixtures'
        for name in ('codex-worker.sh', 'opencode-worker.sh'):
            shutil.copy2(fixture / 'fake_worker.py', self.repo / 'bin' / name)
            (self.repo / 'bin' / name).chmod(0o755)
        shutil.copytree(SOURCE / 'workers', self.repo / 'workers')
        for name in ('install.sh', 'uninstall.sh', 'bin/aw'):
            (self.repo / name).chmod(0o755)
        skill = self.repo / 'adapters/claude/SKILL.md'
        skill.parent.mkdir(parents=True)
        shutil.copy2(SOURCE / 'adapters/claude/SKILL.md', skill)
        # 看板替身只监听临时端口；开浏览器用假 open，不能操作真实桌面。
        self.port = 18789
        board = self.repo / 'board/board.py'
        board.parent.mkdir()
        board.write_text('# 离线替身不绑定 socket，端口状态由测试注入。\n')
        self.fakebin = self.base / 'fakebin'
        self.fakebin.mkdir()
        for name, body in (('codex', 'echo "codex-cli 0.0-test"\n'), ('open', 'exit 0\n'), ('launchctl', 'exit 99\n')):
            script = self.fakebin / name
            script.write_text('#!/bin/bash\n' + body)
            script.chmod(0o755)
        shutil.copy2(fixture / 'security.py', self.fakebin / 'security')
        (self.fakebin / 'security').chmod(0o755)
        self.cfg = self.base / 'config.json'
        self.cfg.write_text(json.dumps({'work_dir': str(self.home / 'work'), 'board_port': self.port}), encoding='utf-8')
        self.env = mock.patch.dict(os.environ, {'HOME': str(self.home), 'FRCLYO_CONFIG': str(self.cfg),
                                   'FRCLYO_LAUNCHD_DRY_RUN': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                                   'PATH': str(self.fakebin) + ':/usr/bin:/bin:/usr/sbin:/sbin'})
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop('CODEX_HOME', None)
        self.old_path = list(sys.path)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), self.old_path))
        for name in list(sys.modules):
            if name == 'frclyo' or name.startswith('frclyo.'):
                del sys.modules[name]
        self.aw = load_aw(self.repo / 'bin/aw')
        port_patch = mock.patch.object(self.aw, 'port_open', return_value=True)
        port_patch.start()
        self.addCleanup(port_patch.stop)
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(lambda: self.redirect.__exit__(None, None, None))

    def terminal(self, *args, input=''):
        """从真实 bash 启动，检查符号链接、参数及 stdin 都没有被入口破坏。"""
        return subprocess.run(['/bin/bash', str(self.repo / 'bin/aw'), *args], input=input,
                              text=True, capture_output=True, timeout=20)

    def events(self):
        path = self.home / 'work/账本.jsonl'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]

    def run_demo(self, bad=''):
        """模型候选在内存中固定，三关、派活和账本都跑真实实现。"""
        with mock.patch.object(self.aw, 'model_inventory', return_value=[CHOICE]), mock.patch.dict(os.environ, {'FAKE_BAD': bad, 'FAKE_REQUIRE_PARALLEL': '1'}):
            return self.aw.demo(['--yes'])

    def test_bash_entry_and_chinese_help(self):
        result = self.terminal('帮助')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('三关验收', result.stdout)

    def test_install_doctor_uninstall_existing_config(self):
        """已有配置场景完整撤销，账本与配置使用生产实现。"""
        before = snapshot(self.home)
        self.assertEqual(self.aw.install(['--yes']), 0)
        self.assertTrue((self.home / '.local/bin/aw').is_symlink())
        skill = self.home / '.claude/skills/frclyo-brain/SKILL.md'
        self.assertIn(str(self.repo), skill.read_text())
        self.assertNotIn('{{REPO}}', skill.read_text())
        with mock.patch.object(self.aw, 'model_inventory', return_value=[CHOICE]):
            self.assertEqual(self.aw.doctor([]), 0)
        self.assertEqual(self.aw.uninstall(['--yes']), 0)
        self.assertFalse(self.cfg.exists())
        self.assertEqual(snapshot(self.home), before)

    def test_clean_install_and_uninstall_real_config(self):
        self.cfg.unlink()
        before = snapshot(self.home)
        self.assertEqual(self.aw.install(['--yes']), 0)
        self.assertEqual(self.cfg.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.aw.uninstall(['--yes']), 0)
        self.assertFalse(self.cfg.exists())
        self.assertEqual(snapshot(self.home), before)

    def test_clean_home_install_discover_demo_board_and_uninstall(self):
        """全链路使用真实公共库，只有钥匙串、模型 CLI 与系统服务替换为离线边界。"""
        self.cfg.unlink()
        opencode = self.fakebin / 'opencode'
        opencode.write_text('#!/bin/bash\n# 离线版本查询，不启动模型。\necho opencode-0.0-test\n')
        opencode.chmod(0o755)
        # 预置的候选模型属于用户已有 CLI；卸载不能删这些用户资源。
        cache = self.home / '.codex/models_cache.json'
        cache.parent.mkdir()
        cache.write_text(json.dumps({'models': [{'slug': 'offline-model', 'visibility': 'list',
                           'supported_reasoning_levels': [{'effort': 'medium'}]}]}))
        before = snapshot(self.home)
        self.assertEqual(self.aw.install(['--yes']), 0)
        result = self.terminal('key', 'add', 'deepseek', input='y\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.aw.doctor([]), 0)
        pool = self.aw.model_inventory()
        self.assertEqual(pool[0]['source'], 'models_cache')
        self.assertTrue(any(row['source'] == 'worker_config' and row['provider'] == 'deepseek' for row in pool))
        self.assertEqual(self.aw.demo(['--yes']), 0)
        # 加载真实看板的数据汇总，检查完成卡和健康信息，不需要监听端口。
        with mock.patch.dict(os.environ, {'BOARD_NO_BACKGROUND': '1', 'BOARD_BALANCE': '0'}):
            spec = importlib.util.spec_from_file_location('integrated_board', SOURCE / 'board/board.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.refresh_health('audit')
            state = module.build_state()
        self.assertEqual(len(state['最近完成']), 3)
        self.assertEqual(state['健康']['日审']['核对卡数'], 3)
        self.assertEqual(state['健康']['日审']['问题'], [])
        self.assertEqual(self.aw.uninstall(['--yes']), 0)
        self.assertFalse(self.cfg.exists())
        self.assertEqual(self.aw.runtime('keys').list_providers(), [])
        self.assertEqual(snapshot(self.home), before)

    def test_no_input_does_not_install_optional_files(self):
        self.cfg.unlink()
        self.assertEqual(self.aw.install(['--no-input']), 0)
        self.assertFalse((self.home / '.local').exists())
        self.assertFalse((self.home / 'work').exists())
        self.assertFalse(self.cfg.exists())

    def test_fake_home_codex_home_fallback_key_demo_and_uninstall(self):
        """复现本机六条问题：独立 Codex HOME、回退 key、DM、B 工人和配置清理。"""
        self.cfg = self.home / '.frclyo/config.json'
        os.environ['FRCLYO_CONFIG'] = str(self.cfg)
        codex_home = self.base / 'real Codex 中文'
        codex_home.mkdir()
        cache = codex_home / 'models_cache.json'
        cache.write_text(json.dumps({'models': [{'slug': 'offline-model', 'visibility': 'list',
                         'supported_reasoning_levels': [{'effort': 'medium'}]}]}))
        os.environ['CODEX_HOME'] = str(codex_home)
        fallback = self.base / 'legacy.env'
        fallback.write_text('DEEPSEEK_API_KEY=FALLBACK-SECRET-SENTINEL\n')
        for name in ('opencode', 'claude'):
            script = self.fakebin / name
            script.write_text('#!/bin/bash\necho offline-cli\n')
            script.chmod(0o755)
        before = snapshot(self.home)
        self.assertEqual(self.aw.install(['--yes']), 0)
        plist = self.home / 'Library/LaunchAgents/com.frclyo.board.plist'
        self.assertEqual(self.aw.plistlib.loads(plist.read_bytes())['EnvironmentVariables']['CODEX_HOME'], str(codex_home))
        current = self.aw.config()
        current.update(key_env_file=str(fallback), board_port=19789)
        self.aw.runtime('config').save(current)
        with mock.patch.object(self.aw, 'port_open', return_value=False):
            self.assertEqual(self.aw.doctor([]), 0)
        self.assertIn('⚠️ 看板端口 19789', self.output.getvalue())
        self.assertIn('✅ 已登记 key：deepseek', self.output.getvalue())
        self.assertNotIn('❌', self.output.getvalue())
        listed = self.terminal('key', 'list')
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(listed.stdout.strip(), 'deepseek')
        self.assertEqual(self.aw.demo(['--yes']), 0)
        events = [json.loads(line) for line in self.aw.path_of('ledger_file').read_text().splitlines()]
        opened = [row for row in events if row['动作'] == '开卡']
        self.assertEqual(len(opened), 3)
        self.assertTrue(all(row['任务'].startswith('DM-') for row in opened))
        performance = [json.loads(line) for line in self.aw.path_of('perf_file').read_text().splitlines()]
        self.assertEqual(performance[1]['工人'], 'opencode')
        self.assertTrue(performance[1]['模型'].startswith('deepseek/'))
        self.assertTrue(all(row['通过验收'] for row in performance))
        self.assertEqual(self.aw.uninstall(['--yes']), 0)
        self.assertFalse((self.home / '.frclyo').exists())
        self.assertEqual(snapshot(self.home), before)
        self.assertTrue(cache.exists())
        self.assertTrue(fallback.exists())
        self.assertNotIn('FALLBACK-SECRET-SENTINEL', self.output.getvalue() + listed.stdout + listed.stderr)

    def test_install_is_idempotent(self):
        self.aw.install(['--yes'])
        first = snapshot(self.home)
        self.aw.install(['--yes'])
        second = snapshot(self.home)
        first.pop('.frclyo/安装记录.jsonl')
        second.pop('.frclyo/安装记录.jsonl')
        self.assertEqual(first, second)

    def test_dry_run_has_no_effect(self):
        self.aw.install(['--yes'])
        before = snapshot(self.home)
        self.assertEqual(self.aw.uninstall(['--dry-run']), 0)
        self.assertEqual(before, snapshot(self.home))

    def test_default_uninstall_preserves_data(self):
        self.aw.install(['--yes'])
        user_data = self.home / 'work/keep.txt'
        user_data.write_text('user data')
        with mock.patch('builtins.input', side_effect=['y', '', '']):
            self.assertEqual(self.aw.uninstall([]), 0)
        self.assertTrue(user_data.exists())
        self.assertTrue(self.cfg.exists())
        self.assertFalse((self.home / '.local/bin/aw').exists())

    def test_modified_skill_is_preserved(self):
        self.aw.install(['--yes'])
        skill = self.home / '.claude/skills/frclyo-brain/SKILL.md'
        skill.write_text('user changes')
        self.assertEqual(self.aw.uninstall(['--yes']), 1)
        self.assertEqual(skill.read_text(), 'user changes')
        self.assertTrue(self.aw.JOURNAL.exists())

    def test_uninstall_deletes_modified_config_and_journal(self):
        self.cfg = self.home / '.frclyo/config.json'
        os.environ['FRCLYO_CONFIG'] = str(self.cfg)
        self.aw.install(['--yes'])
        current = self.aw.config()
        current['board_port'] += 1
        self.aw.runtime('config').save(current)
        self.assertEqual(self.aw.uninstall(['--yes']), 0)
        self.assertFalse(self.cfg.exists())
        self.assertFalse(self.aw.JOURNAL.exists())
        self.assertFalse(self.aw.STATE.exists())

    def test_uninstall_deletes_preexisting_config_but_keeps_unowned_files(self):
        self.cfg = self.home / '.frclyo/config.json'
        self.cfg.parent.mkdir()
        self.cfg.write_text(json.dumps({'work_dir': str(self.home / 'work')}))
        os.environ['FRCLYO_CONFIG'] = str(self.cfg)
        unowned = self.cfg.parent / 'user-notes.txt'
        unowned.write_text('keep-user-notes')
        cache = self.home / '.cache/opencode/models.json'
        cache.parent.mkdir(parents=True)
        cache.write_text('keep-opencode-cache')
        self.aw.install(['--yes'])
        self.assertEqual(self.aw.uninstall(['--yes']), 0)
        self.assertFalse(self.cfg.exists())
        self.assertFalse(self.aw.JOURNAL.exists())
        self.assertEqual(unowned.read_text(), 'keep-user-notes')
        self.assertEqual(cache.read_text(), 'keep-opencode-cache')

    def test_uninstall_keeps_modified_config_without_delete_confirmation(self):
        self.cfg.unlink()
        self.aw.install(['--yes'])
        current = self.aw.config()
        current['board_port'] += 1
        self.aw.runtime('config').save(current)
        before = self.cfg.read_bytes()
        with mock.patch('builtins.input', side_effect=['y', '', '']):
            self.assertEqual(self.aw.uninstall([]), 0)
        self.assertEqual(self.cfg.read_bytes(), before)
        self.assertTrue(self.aw.JOURNAL.exists())

    def test_preexisting_link_is_not_overwritten(self):
        link = self.home / '.local/bin/aw'
        link.parent.mkdir(parents=True)
        link.symlink_to('elsewhere')
        with self.assertRaisesRegex(self.aw.UserError, '未覆盖'):
            self.aw.install(['--yes'])
        self.assertEqual(os.readlink(link), 'elsewhere')

    def test_symlink_parent_outside_home_rejected(self):
        (self.home / '.local').symlink_to(self.base)
        with self.assertRaisesRegex(self.aw.UserError, 'HOME 外'):
            self.aw.install_file(self.home / '.local/bin/aw', target=self.repo / 'bin/aw')

    def test_broken_journal_stops_uninstall(self):
        self.aw.journal_begin()
        self.aw.JOURNAL.write_text('{broken')
        before = snapshot(self.home)
        with self.assertRaisesRegex(self.aw.UserError, '记录损坏'):
            self.aw.uninstall(['--yes'])
        self.assertEqual(before, snapshot(self.home))

    def test_demo_two_codex_workers_pass(self):
        self.assertEqual(self.run_demo(), 0)
        rows = self.events()
        self.assertEqual(sum(row['动作'] == '开卡' for row in rows), 3)
        self.assertEqual(sum(row['动作'] == '完成' for row in rows), 3)
        self.assertEqual(sum(bool(row.get('父任务')) for row in rows), 2)
        self.assertIn('第三关', self.output.getvalue())
        self.assertEqual(self.aw.runtime('audit').check()['问题'], [])
        self.assertEqual(len(self.aw.path_of('perf_file').read_text().splitlines()), 2)

    def test_demo_prefers_opencode_for_b(self):
        other = {'worker': 'opencode', 'model': 'deepseek/offline', 'level': ''}
        self.assertEqual(self.aw.choose_pair([CHOICE, other]), (CHOICE, other))
        with mock.patch.object(self.aw, 'model_inventory', return_value=[CHOICE, other]):
            self.assertEqual(self.aw.demo(['--yes']), 0)

    def test_demo_uses_dm_without_consuming_cx_sequence(self):
        first = self.aw.ledger('开卡', '--项目', 'test', '--标题', 'before demo', '--来源', 'CX')
        self.assertEqual(self.run_demo(), 0)
        opened = [row for row in self.events() if row['动作'] == '开卡' and row['任务'] != first]
        self.assertEqual(len(opened), 3)
        self.assertTrue(all(row['任务'].startswith('DM-') for row in opened))
        last = self.aw.ledger('开卡', '--项目', 'test', '--标题', 'after demo', '--来源', 'CX')
        self.assertEqual(int(last.rsplit('-', 1)[1]), int(first.rsplit('-', 1)[1]) + 1)

    def test_demo_board_reuses_configured_listener_without_stopping_it(self):
        with mock.patch.object(self.aw, 'port_open', return_value=True) as port, \
                mock.patch.object(self.aw.subprocess, 'Popen') as start, \
                mock.patch.object(self.aw.os, 'killpg') as kill, \
                mock.patch.object(self.aw, 'board') as board:
            with self.aw.demo_board():
                pass
        start.assert_not_called()
        kill.assert_not_called()
        port.assert_called_once_with(self.port)
        board.assert_called_once_with(['open'])
        self.assertIn('复用已有看板：http://127.0.0.1:' + str(self.port), self.output.getvalue())
        self.assertNotIn('临时看板已结束', self.output.getvalue())

    def test_demo_board_starts_and_cleans_temporary_board_even_on_failure(self):
        child = mock.Mock(pid=12345)
        child.poll.return_value = None
        with mock.patch.object(self.aw, 'port_open', side_effect=[False, True]), \
                mock.patch.object(self.aw.subprocess, 'Popen', return_value=child) as start, \
                mock.patch.object(self.aw.os, 'killpg') as kill, \
                mock.patch.object(self.aw, 'board') as board:
            with self.assertRaisesRegex(RuntimeError, 'demo failed'):
                with self.aw.demo_board():
                    raise RuntimeError('demo failed')
        self.assertEqual(start.call_count, 1)
        self.assertEqual(start.call_args.args[0], [sys.executable, str(self.repo / 'board/board.py')])
        board.assert_called_once_with(['open'])
        kill.assert_called_once_with(child.pid, self.aw.signal.SIGTERM)
        child.wait.assert_called_once_with(timeout=3)
        self.assertIn('aw board start', self.output.getvalue())

    def test_demo_board_already_exited_still_prints_start_hint(self):
        child = mock.Mock()
        child.poll.return_value = 1
        with mock.patch.object(self.aw, 'port_open', return_value=False), \
                mock.patch.object(self.aw.subprocess, 'Popen', return_value=child), \
                mock.patch.object(self.aw.os, 'killpg') as kill, \
                mock.patch.object(self.aw, 'board'):
            with self.aw.demo_board():
                pass
        kill.assert_not_called()
        self.assertIn('aw board start', self.output.getvalue())

    def test_demo_has_clear_missing_worker_message(self):
        with self.assertRaisesRegex(self.aw.UserError, '没有可用工人'):
            self.aw.choose_pair([])

    def test_demo_declined_has_no_ledger(self):
        with mock.patch.object(self.aw, 'model_inventory', return_value=[CHOICE]):
            self.assertEqual(self.aw.demo(['--no-input']), 0)
        self.assertFalse((self.home / 'work').exists())

    def test_demo_bad_sample_blocks_all_cards(self):
        self.assert_demo_failure('sample')

    def test_demo_failed_tests_blocks_all_cards(self):
        self.assert_demo_failure('tests')

    def test_demo_zero_tests_is_failure(self):
        self.assert_demo_failure('zero-tests')

    def test_demo_bad_e2e_blocks_all_cards(self):
        self.assert_demo_failure('e2e')

    def test_demo_receipt_hash_failure(self):
        self.assert_demo_failure('receipt')

    def test_demo_missing_receipt_failure(self):
        self.assert_demo_failure('missing-receipt')

    def test_demo_worker_exit_failure(self):
        self.assert_demo_failure('exit')

    def assert_demo_failure(self, kind):
        with self.assertRaises(self.aw.UserError):
            self.run_demo(kind)
        rows = self.events()
        self.assertEqual(sum(row['动作'] == '卡住' for row in rows), 3)
        self.assertFalse(any(row['动作'] == '完成' for row in rows))

    def test_key_names_only(self):
        result = self.terminal('密钥', '列表')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('尚未登记', result.stdout)

    def test_key_add_requires_confirmation(self):
        result = self.terminal('key', 'add', 'deepseek', input='n\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.aw.JOURNAL.exists())

    def test_unknown_flags_rejected(self):
        result = self.terminal('__install', '--yes', '--no-input')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.aw.JOURNAL.exists())

    def test_doctor_reports_empty_cache_as_no_candidates(self):
        result = self.terminal('doctor')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('无可用模型', result.stdout)

    def test_doctor_unstarted_board_is_warning_and_success(self):
        with mock.patch.object(self.aw, 'port_open', return_value=False), \
                mock.patch.object(self.aw, 'model_inventory', return_value=[CHOICE]):
            self.assertEqual(self.aw.doctor([]), 0)
        self.assertIn('⚠️ 看板端口', self.output.getvalue())
        self.assertNotIn('❌ 看板端口', self.output.getvalue())

    def test_doctor_board_warning_does_not_hide_model_failure(self):
        with mock.patch.object(self.aw, 'port_open', return_value=False), \
                mock.patch.object(self.aw, 'model_inventory', return_value=[]):
            self.assertEqual(self.aw.doctor([]), 1)
        self.assertIn('⚠️ 看板端口', self.output.getvalue())
        self.assertIn('❌ 无可用模型', self.output.getvalue())

    def test_calibrate_records_real_performance(self):
        with mock.patch.object(self.aw, 'model_inventory', return_value=[CHOICE]):
            self.assertEqual(self.aw.calibrate(['--yes']), 0)
        rows = [json.loads(line) for line in self.aw.path_of('perf_file').read_text().splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row['通过验收'] for row in rows))
        self.assertTrue(all(row['费用美元'] is None for row in rows))
        self.assertEqual(self.aw.runtime('audit').check()['问题'], [])

    def model_cache(self, *names):
        """给真实命令入口提供候选，排序与过滤使用生产公共库。"""
        cache = self.home / '.codex/models_cache.json'
        cache.parent.mkdir(exist_ok=True)
        cache.write_text(json.dumps({'client_version': 'app-other-version', 'models': [
            {'slug': name, 'visibility': 'list', 'supported_reasoning_levels': [{'effort': 'medium'}]}
            for name in names]}))

    def availability_rows(self):
        return json.loads((self.home / 'work/模型可用性.json').read_text())['records']

    def test_terminal_demo_unsupported_first_candidate_switches_and_passes(self):
        self.model_cache('candidate-one', 'candidate-two')
        with mock.patch.dict(os.environ, {'FAKE_UNUSABLE': 'candidate-one'}):
            result = self.terminal('demo', '--yes')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('candidate-one 用不了，已换成 candidate-two', result.stdout)
        self.assertIn('三关全部通过', result.stdout)
        records = {r['model']: r['status'] for r in self.availability_rows()}
        self.assertEqual(records, {'candidate-one': 'unusable', 'candidate-two': 'ok'})
        runs = list(self.aw.path_of('deliver_dir').glob('*-*/r2/回执.json'))
        self.assertEqual(len(runs), 2)
        self.assertTrue(all(json.loads(p.read_text())['模型'] == 'candidate-two' for p in runs))
        rows = [json.loads(line) for line in self.aw.path_of('perf_file').read_text().splitlines()]
        self.assertEqual(len(rows), 4)
        self.assertEqual(sum(r['通过验收'] for r in rows), 2)
        # 下一次直接避开已拒绝候选；旧轮次的证据仍存在。
        self.assertEqual([r['model'] for r in self.aw.model_inventory()], ['candidate-two'])
        self.assertTrue(all(p.parent.parent.joinpath('r1/events.jsonl').exists() for p in runs))

    def test_terminal_demo_failure_is_nonzero_and_other_errors_do_not_retry(self):
        self.model_cache('candidate-one', 'candidate-two')
        with mock.patch.dict(os.environ, {'FAKE_BAD': 'exit'}):
            result = self.terminal('demo', '--yes')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('演示未通过', result.stderr)
        self.assertNotIn('已换成', result.stdout)
        self.assertFalse(list(self.aw.path_of('deliver_dir').glob('*/r2')))
        self.assertFalse((self.home / 'work/模型可用性.json').exists())

    def test_terminal_demo_gate_failure_is_nonzero_but_model_is_ok(self):
        self.model_cache('candidate-one')
        with mock.patch.dict(os.environ, {'FAKE_BAD': 'sample'}):
            result = self.terminal('demo', '--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('已换成', result.stdout)
        self.assertEqual(self.availability_rows()[0]['status'], 'ok')

    def test_fallback_is_limited_to_three_replacements(self):
        names = ['candidate-%d' % i for i in range(6)]
        self.model_cache(*names)
        pool = self.aw.model_inventory()
        task = self.aw.new_task('校准上限测试')
        job = self.aw.prepare_worker(task, pool[0], '# 校准任务 sort', SOURCE / 'demo/calibrate')
        with mock.patch.dict(os.environ, {'FAKE_UNUSABLE': ','.join(names)}):
            self.aw.invoke_with_fallback(job, pool)
        self.assertEqual(job['round'], 'r4')
        self.assertEqual(len(job['attempts']), 3)
        self.assertIn('上限', job['failure'])
        with self.assertRaises(self.aw.UserError):
            self.aw.verify_receipt(job)
        self.assertEqual(len(self.availability_rows()), 4)

    def test_terminal_demo_all_candidates_unusable_fails(self):
        self.model_cache('candidate-one')
        with mock.patch.dict(os.environ, {'FAKE_UNUSABLE': 'candidate-one'}):
            result = self.terminal('demo', '--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('无剩余候选', result.stderr)
        self.assertFalse(list(self.aw.path_of('deliver_dir').glob('*/r2')))

    def test_calibrate_fallback_report_retains_failed_combo_without_duplicate_questions(self):
        self.model_cache('candidate-one', 'candidate-two')
        with mock.patch.dict(os.environ, {'FAKE_UNUSABLE': 'candidate-one'}):
            result = self.terminal('calibrate', '--yes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('candidate-one 用不了，已换成 candidate-two', result.stdout)
        report = (self.home / 'work/路由建议.md').read_text()
        self.assertIn('candidate-one | 0/1', report)
        self.assertIn('candidate-two | 3/3', report)
        self.assertIn('模型不被当前账号或 CLI 支持', report)
        self.assertIn('/r1', report)
        rows = [json.loads(line) for line in self.aw.path_of('perf_file').read_text().splitlines()]
        self.assertEqual(len(rows), 4)
        self.assertEqual([r['status'] for r in sorted(self.availability_rows(), key=lambda r: r['model'])], ['unusable', 'ok'])

    def test_calibrate_failed_grades_report_and_exit_zero_without_retry(self):
        self.model_cache('candidate-one')
        with mock.patch.dict(os.environ, {'FAKE_CALIBRATE_BAD': 'grade'}):
            result = self.terminal('calibrate', '--yes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('已换成', result.stdout)
        report = (self.home / 'work/路由建议.md').read_text()
        self.assertIn('candidate-one | 0/3', report)
        self.assertIn('失败轮次：3', report)
        self.assertIn('评分或执行失败', report)
        self.assertEqual(self.availability_rows()[0]['status'], 'ok')

    def test_doctor_live_records_availability_and_retries_with_remaining_candidates(self):
        self.model_cache('candidate-one', 'candidate-two')
        with mock.patch.dict(os.environ, {'FAKE_UNUSABLE': 'candidate-one'}):
            result = self.terminal('doctor', '--live', '--yes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('已换成 candidate-two', result.stdout)
        self.assertEqual({r['model']: r['status'] for r in self.availability_rows()},
                         {'candidate-one': 'unusable', 'candidate-two': 'ok'})
        self.assertTrue((self.home / 'work/自检报告.md').exists())

    def test_open_code_pair_uses_availability_order(self):
        pool = [dict(worker='opencode', model='provider-b/good', level='', availability='ok'),
                dict(worker='opencode', model='deepseek/unknown', level='', availability='unknown')]
        self.assertEqual(self.aw.choose_pair(pool), tuple(pool))

    def test_opencode_fallback_can_change_provider_without_colliding_with_peer(self):
        choices = [dict(worker='opencode', model=name, level='') for name in
                   ('provider-a/first', 'provider-b/second', 'provider-c/third')]
        task = self.aw.new_task('OpenCode 候选切换')
        job = self.aw.prepare_worker(task, choices[0], '# 校准任务 sort', SOURCE / 'demo/calibrate')
        providers = {task: 'provider-a', 'peer': 'provider-b'}
        with mock.patch.dict(os.environ, {'FAKE_UNUSABLE': 'provider-a/first'}):
            self.aw.invoke_with_fallback(job, choices, providers)
        self.assertEqual(job['choice']['model'], 'provider-c/third')
        self.assertEqual(job['round'], 'r2')
        self.aw.verify_receipt(job)
        self.assertEqual(providers, {'peer': 'provider-b'})

    def test_key_add_remove_and_uninstall_real_interfaces(self):
        self.aw.install(['--yes'])
        with mock.patch('builtins.input', return_value='y'):
            self.assertEqual(self.aw.key_command(['add', 'deepseek']), 0)
        plans = [row for row in self.aw.read_journal() if row.get('kind') == 'key']
        self.assertEqual(len(plans), 1)
        self.assertEqual(self.aw.runtime('keys').list_providers(), ['deepseek'])
        with mock.patch('builtins.input', return_value='y'):
            self.aw.key_command(['remove', 'deepseek'])
        self.assertEqual(self.aw.runtime('keys').list_providers(), [])
        with mock.patch('builtins.input', return_value='y'):
            self.aw.key_command(['add', 'deepseek'])
        self.assertEqual(self.aw.uninstall(['--yes']), 0)
        self.assertEqual(self.aw.runtime('keys').list_providers(), [])

    def test_calibrate_decline_has_no_side_effect(self):
        with mock.patch.object(self.aw, 'model_inventory', return_value=[CHOICE]):
            self.assertEqual(self.aw.calibrate(['--no-input']), 0)
        self.assertFalse((self.home / 'work').exists())

    def test_run_forwards_exact_arguments(self):
        arguments = ['任务一', 'r1', '带 空格.txt', 'model', '', '240', '$(never-run)']
        with mock.patch.object(self.aw.os, 'execv', side_effect=SystemExit) as execute:
            with self.assertRaises(SystemExit):
                self.aw.main(['运行', 'opencode', *arguments])
        self.assertEqual(execute.call_args.args[1][1:], arguments)

    def test_ledger_forwards_exact_arguments(self):
        arguments = ['备注', 'CX-20261013-01', '中文 空格 ; $(never-run)']
        with mock.patch.object(self.aw.os, 'execv', side_effect=SystemExit) as execute:
            with self.assertRaises(SystemExit):
                self.aw.main(['账本', *arguments])
        self.assertEqual(execute.call_args.args[1][1:], arguments)


if __name__ == '__main__':
    unittest.main()
