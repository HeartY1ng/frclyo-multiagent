"""v1.2 公共接口回归：原子写、并发追加、模型白名单和可撤销凭据。"""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

BIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BIN))
from frclyo import audit, config, keys, models, perf


class Interfaces(unittest.TestCase):
    def setUp(self):
        # 每次切换假 HOME 和唯一配置路径，任何测试不读取本机账号目录。
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.cfg = self.home / 'settings/config.json'
        env = mock.patch.dict(os.environ, HOME=str(self.home), FRCLYO_CONFIG=str(self.cfg))
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('CODEX_HOME', None)

    def test_save_atomic_permissions_and_complete_replacement(self):
        config.save({'work_dir': '~/one', 'providers': {}, 'custom': 7})
        self.assertEqual(self.cfg.stat().st_mode & 0o777, 0o600)
        before = self.cfg.read_bytes()
        with mock.patch.object(config.os, 'replace', side_effect=OSError('模拟失败')):
            with self.assertRaises(OSError):
                config.save({'work_dir': '~/two'})
        self.assertEqual(self.cfg.read_bytes(), before)
        self.assertEqual(list(self.cfg.parent.glob('.config-*')), [])
        with self.assertRaises(ValueError):
            config.save({'board_port': 0})
        self.assertEqual(self.cfg.read_bytes(), before)
        config.save({'work_dir': '~/two'})
        self.assertNotIn('custom', json.loads(self.cfg.read_text()))
        self.assertEqual(config.path_of('work_dir'), str(self.home / 'two'))

    def test_init_defaults_does_not_overwrite_even_concurrently(self):
        # 真正的两个子进程竞争创建，文件只能等于其中一个完整配置。
        command = [sys.executable, '-B', '-c',
                   'from frclyo import config; import sys; config.init_default({"work_dir":sys.argv[1]})']
        env = dict(os.environ, PYTHONPATH=str(BIN))
        children = [subprocess.Popen(command + [name], env=env) for name in ('~/one', '~/two')]
        for child in children:
            self.assertEqual(child.wait(timeout=10), 0)
        value = config.load()
        self.assertIn(value['work_dir'], [str(self.home / name) for name in ('one', 'two')])
        self.assertEqual(value['board_port'], 8789)
        original = self.cfg.read_bytes()
        self.assertEqual(config.init_default({'work_dir': '~/three'}), value)
        self.assertEqual(self.cfg.read_bytes(), original)
        self.assertEqual(self.cfg.stat().st_mode & 0o777, 0o600)

    def test_init_merges_partial_defaults(self):
        value = config.init_default({'codex': {'default_fast': False}, 'providers': {'deepseek': {}}})
        self.assertEqual(value['codex'], {'proxy': '', 'default_fast': False})
        self.assertEqual(list(value['providers']), ['deepseek'])
        self.assertEqual(value['providers']['deepseek']['api_host'], 'api.deepseek.com')

    def test_remove_key_is_scoped_and_keeps_env_fallback(self):
        fallback = self.home / 'legacy.env'
        fallback.write_text('DEEPSEEK_API_KEY=OFFLINE-SENTINEL\n')
        config.save({'key_env_file': str(fallback)})
        with mock.patch.object(keys.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertTrue(keys.remove_key('deepseek'))
            self.assertEqual(run.call_args.args[0], ['security', 'delete-generic-password', '-s', 'frclyo-deepseek', '-a', 'frclyo'])
        with mock.patch.object(keys.subprocess, 'run', return_value=subprocess.CompletedProcess([], 44, stdout='')):
            self.assertFalse(keys.remove_key('deepseek'))
            self.assertEqual(keys.get_key('deepseek'), 'OFFLINE-SENTINEL')
        with mock.patch.object(keys.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaises(RuntimeError):
                keys.remove_key('deepseek')
        self.assertEqual(fallback.read_text(), 'DEEPSEEK_API_KEY=OFFLINE-SENTINEL\n')
        with self.assertRaises(ValueError):
            keys.remove_key('../bad')

    def test_model_discovery_filters_candidates_and_disabled_variants(self):
        # 候选严格来自本机缓存和工人白名单；隐藏模型、不允许的家不可流入清单。
        cache = self.home / '.codex/models_cache.json'
        cache.parent.mkdir()
        cache.write_text(json.dumps({'models': [
            {'slug': 'candidate', 'visibility': 'list', 'supported_reasoning_levels': [{'effort': 'high'}]},
            {'slug': 'hidden', 'visibility': 'hide'}]}))
        config.save({'providers': {'xiaomi': {}, 'custom': {'opencode_family': 'deepseek', 'api_host': 'api.example.com'}}})
        with mock.patch.object(models, 'cli_version', return_value='test'), mock.patch.object(models.shutil, 'which', return_value='/fake/cli'), mock.patch.object(keys, 'get_key', side_effect=lambda provider: 'OFFLINE-SENTINEL' if provider == 'custom' else None):
            rows = models.discover()
        self.assertEqual(rows[0], {'worker': 'codex', 'model': 'candidate', 'levels': ['high'], 'provider': None,
                                   'key_ok': True, 'source': 'models_cache', 'cli_version': 'test', 'availability': 'unknown'})
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(r['provider'] in (None, 'xiaomi', 'custom') for r in rows))
        xiaomi = next(r for r in rows if r['provider'] == 'xiaomi')
        self.assertEqual(xiaomi['levels'], ['', 'think', 'nothink'])
        self.assertFalse(xiaomi['key_ok'])
        self.assertTrue(all(r['key_ok'] for r in rows if r['provider'] == 'custom'))
        with mock.patch.object(models.shutil, 'which', return_value=None):
            self.assertEqual(models.discover(), [])

    def test_model_missing_and_corrupt_cache(self):
        with mock.patch.object(models.shutil, 'which', side_effect=lambda name: '/fake' if name == 'codex' else None):
            self.assertEqual(models.discover(), [])
            cache = self.home / '.codex/models_cache.json'
            cache.parent.mkdir()
            cache.write_text('{broken')
            with self.assertRaises(ValueError):
                models.discover()

    def test_discovery_codex_home_overrides_home_without_fallback(self):
        default = self.home / '.codex/models_cache.json'
        default.parent.mkdir()
        default.write_text(json.dumps({'models': [{'slug': 'wrong-home', 'visibility': 'list'}]}))
        selected = self.home / 'custom Codex 中文'
        selected.mkdir()
        with mock.patch.dict(os.environ, CODEX_HOME=str(selected)), mock.patch.object(
                models.shutil, 'which', side_effect=lambda name: '/fake' if name == 'codex' else None):
            self.assertEqual(models.discover(), [])
            (selected / 'models_cache.json').write_text(json.dumps({'models': [
                {'slug': 'selected-home', 'visibility': 'list', 'supported_reasoning_levels': [{'effort': 'high'}]}]}))
            self.assertEqual([row['model'] for row in models.discover()], ['selected-home'])

    def test_discovery_key_ok_uses_env_fallback_without_leaking_key(self):
        fallback = self.home / 'legacy.env'
        fallback.write_text('DEEPSEEK_API_KEY=ENV-SENTINEL\nXIAOMI_API_KEY=\n')
        config.save({'key_env_file': str(fallback)})
        with mock.patch.object(keys.subprocess, 'run', side_effect=FileNotFoundError()), mock.patch.object(
                models.shutil, 'which', side_effect=lambda name: '/fake' if name == 'opencode' else None):
            rows = models.discover()
        self.assertTrue(any(row['provider'] == 'deepseek' for row in rows))
        self.assertTrue(all(row['key_ok'] == (row['provider'] == 'deepseek') for row in rows))
        self.assertNotIn('ENV-SENTINEL', json.dumps(rows))

    def test_performance_schema_and_concurrent_append(self):
        config.save({'work_dir': '~/work'})
        original = {'input_tokens': 123, 'nested': {'cached': 5}}
        perf.append({'任务号': 'sample', 'token用量': original, '通过验收': True, '费用美元': 0.01, 'ignored': 4})
        # 多进程写同一文件验证 JSONL 不交错、不丢记录，真实复用账本锁。
        source = 'from frclyo import perf; import sys; [perf.append({"任务号":sys.argv[1]}) for _ in range(8)]'
        children = [subprocess.Popen([sys.executable, '-B', '-c', source, str(n)], env=dict(os.environ, PYTHONPATH=str(BIN))) for n in range(5)]
        for child in children:
            self.assertEqual(child.wait(timeout=15), 0)
        path = Path(config.path_of('perf_file'))
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len(rows), 41)
        self.assertEqual(set(rows[0]), set(perf.FIELDS))
        self.assertEqual(rows[0]['token用量'], original)
        self.assertIsNone(rows[0]['时间'])
        self.assertTrue(all(row['费用美元'] is None for row in rows[1:]))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        before = path.read_bytes()
        for bad in ({'通过验收': 1}, {'费用美元': float('nan')}, {'token用量': []}):
            with self.assertRaises(ValueError):
                perf.append(bad)
        self.assertEqual(path.read_bytes(), before)

    def test_audit_contract_and_cli_empty_and_bad_ledger(self):
        result = audit.check()
        self.assertEqual(set(result), {'时间', '核对卡数', '问题', '提醒'})
        self.assertIsInstance(result['时间'], float)
        self.assertEqual(result['核对卡数'], 0)
        self.assertEqual(result['问题'], [])
        self.assertFalse((self.home / 'AgentWork').exists())
        path = Path(config.path_of('ledger_file'))
        path.parent.mkdir()
        path.write_text('broken\n')
        proc = subprocess.run([sys.executable, '-B', '-m', 'frclyo.audit'], env=dict(os.environ, PYTHONPATH=str(BIN)), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(len(json.loads(proc.stdout)['问题']), 1)


if __name__ == '__main__':
    unittest.main()
