"""模型可用性的版本隔离、并发写入与错误分类；全部使用虚构模型。"""
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
from frclyo import models


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        cfg = self.root / 'config.json'
        cfg.write_text(json.dumps({'work_dir': str(self.root / 'work'), 'providers': {}}))
        patch = mock.patch.dict(os.environ, HOME=str(self.root), FRCLYO_CONFIG=str(cfg),
                               CODEX_HOME=str(self.root / 'codex'))
        patch.start()
        self.addCleanup(patch.stop)
        cache = self.root / 'codex/models_cache.json'
        cache.parent.mkdir()
        cache.write_text(json.dumps({'client_version': 'app-version', 'models': [
            {'slug': name, 'visibility': 'list'} for name in ('first', 'second', 'third', 'fourth')]}))
        for patch in (mock.patch.object(models, 'cli_version', return_value='cli-v1'),
                      mock.patch.object(models.shutil, 'which', side_effect=lambda w: '/fake' if w == 'codex' else None)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_discovery_is_read_only_then_stably_ranks_and_filters(self):
        self.assertEqual([r['availability'] for r in models.discover()], ['unknown'] * 4)
        self.assertFalse((self.root / 'work').exists())
        models.record('codex', 'first', 'cli-v1', 'unusable', '拒绝')
        models.record('codex', 'third', 'cli-v1', 'ok', '完成')
        rows = models.discover()
        self.assertEqual([r['model'] for r in rows], ['third', 'second', 'fourth'])
        self.assertEqual([r['availability'] for r in rows], ['ok', 'unknown', 'unknown'])
        self.assertEqual([r['model'] for r in models.discover(include_unusable=True)],
                         ['third', 'second', 'fourth', 'first'])
        self.assertTrue(all(r['time'] and r['reason'] for r in models.records()))

    def test_actual_cli_version_change_invalidates_previous_conclusions(self):
        models.record('codex', 'first', 'cli-v1', 'unusable', '拒绝')
        models.record('opencode', 'first', 'cli-v2', 'unusable', '另一工人')
        with mock.patch.object(models, 'cli_version', return_value='cli-v2'):
            rows = models.discover()
        self.assertEqual(rows[0]['model'], 'first')
        self.assertEqual(rows[0]['availability'], 'unknown')
        self.assertEqual(rows[0]['cli_version'], 'cli-v2')
        self.assertEqual(len(models.records()), 2)
        with mock.patch.object(models, 'cli_version', return_value='unknown'):
            self.assertEqual(models.discover()[0]['availability'], 'unknown')

    def test_observation_recovers_same_version_and_ignores_other_failures(self):
        models.record('codex', 'first', 'cli-v1', 'unusable', '拒绝')
        self.assertEqual(models.observe('codex', 'first', 'cli-v1', self.root), 'unknown')
        self.assertEqual(models.availability('codex', 'first', 'cli-v1'), 'unusable')
        models.observe('codex', 'first', 'cli-v1', self.root, succeeded=True)
        self.assertEqual(models.availability('codex', 'first', 'cli-v1'), 'ok')
        self.assertEqual(len(models.records()), 1)

    def test_nested_fatal_errors_match_but_tier_and_other_errors_do_not(self):
        accepted = [
            {'type': 'error', 'message': json.dumps({'status': 400, 'error': {'message':
             "The 'arbitrary-name' model is not supported when using Codex with a ChatGPT account."}})},
            {'type': 'turn.failed', 'error': {'message': 'HTTP 400: Model `arbitrary-name` is not supported by this CLI.'}},
            {'type': 'error', 'error': {'code': 'model_not_supported'}},
            {'type': 'item.completed', 'item': {'type': 'error', 'message': 'HTTP 400: Unsupported model for this account'}},
            {'type': 'error', 'message': 'The requested model is not available for this account'},
        ]
        rejected = [
            {'type': 'item.completed', 'item': {'type': 'error', 'message': 'Configured service tier priority is not advertised as supported for model arbitrary-name'}},
            {'type': 'error', 'message': 'HTTP 400: reasoning effort is not supported for this model'},
            {'type': 'error', 'message': 'HTTP 400: model is not supported for this service tier'},
            {'type': 'error', 'message': 'HTTP 400: invalid input'},
            {'type': 'error', 'message': 'HTTP 429: rate limit'},
            {'type': 'error', 'message': 'authentication failed'},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': accepted[0]['message']}},
        ]
        for event in accepted + rejected:
            with self.subTest(event=event):
                (self.root / 'events.jsonl').write_text('invalid-json\n' + json.dumps(event) + '\n')
                self.assertEqual(bool(models.unsupported_reason(self.root)), event in accepted)

    def test_atomic_replace_failure_preserves_existing_records(self):
        models.record('codex', 'first', 'cli-v1', 'ok', '完成')
        before = models.records()
        with mock.patch.object(models.os, 'replace', side_effect=OSError('故障')):
            with self.assertRaises(OSError):
                models.record('codex', 'second', 'cli-v1', 'ok', '完成')
        self.assertEqual(models.records(), before)
        self.assertEqual(list((self.root / 'work').glob('.availability-*')), [])

    def test_concurrent_processes_do_not_lose_records(self):
        command = [sys.executable, '-B', '-c',
                   'from frclyo import models; import sys; '
                   'models.record("codex",sys.argv[1],"v1","ok","完成"); models.records()']
        env = dict(os.environ, PYTHONPATH=str(BIN))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: subprocess.run(command + [str(i)], env=env,
                                capture_output=True, timeout=10), range(16)))
        self.assertTrue(all(r.returncode == 0 for r in results), [r.stderr for r in results])
        self.assertEqual(len(models.records()), 16)


if __name__ == '__main__':
    unittest.main()
