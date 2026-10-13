"""真实 bash 入口配合假 Codex CLI，验证参数、配置代理、开关与回执。"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

# 假模型只记录调用参数并交固定结果；不运行真实 agent，也不访问网络。
BIN = Path(__file__).resolve().parents[1]
FAKE = '''#!/usr/bin/env python3
# 测试专用模型程序，保留事件和结果协议。
import json, os, pathlib, sys, time
if '--version' in sys.argv:
    print('codex-cli test')
    sys.exit(0)
args = sys.argv[1:]
run = pathlib.Path(args[args.index('-C') + 1])
(run / 'invocation.json').write_text(json.dumps({'args': args, 'proxy': {k: v for k,v in os.environ.items() if 'proxy' in k.lower()}}))
if sys.stdin.read().strip() == 'timeout':
    time.sleep(60)
pathlib.Path(args[args.index('-o') + 1]).write_text('MODEL-SENTINEL\\n')
print(json.dumps({'type': 'thread.started', 'thread_id': 'fake-thread'}))
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 3, 'output_tokens': 2}}))
'''


class CodexWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-worker-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        commands = self.root / "commands"
        commands.mkdir()
        fake = commands / "codex"
        fake.write_text(FAKE)
        fake.chmod(0o700)
        (self.root / ".codex").mkdir()
        (self.root / ".codex/models_cache.json").write_text(json.dumps({"client_version": "test", "models": [
            {"slug": "test-model", "visibility": "list", "supported_reasoning_levels": [{"effort": "high"}, {"effort": "ultra"}],
             "service_tiers": [{"id": "priority"}]}]}))
        self.cfg = self.root / "config.json"
        self.configure()
        self.task = self.root / "task.txt"
        self.task.write_text("normal")
        self.env = dict(os.environ, HOME=str(self.root), FRCLYO_CONFIG=str(self.cfg),
                        PATH=str(commands) + os.pathsep + os.environ.get("PATH", ""),
                        HTTPS_PROXY="http://inherited.invalid", all_proxy="http://inherited.invalid")
        for name in ("CODEX_HOME", "CODEX_WORKER_WEB", "CODEX_WORKER_FAST", "CODEX_WORKER_SUBAGENTS", "WORKER_INPUT_DIR"):
            self.env.pop(name, None)

    def configure(self, **codex):
        """配置使用含空格路径，检查 shell 引号和 Python 路径转义。"""
        self.cfg.write_text(json.dumps({"work_dir": str(self.root / "work space"), "codex": codex}))

    def launch(self, run="r1", effort="high", timeout="10", **environment):
        proc = subprocess.run(["/bin/bash", str(BIN / "codex-worker.sh"), "TEST-01", run,
                               str(self.task), "test-model", effort, timeout],
                              env=dict(self.env, **environment), capture_output=True, text=True, timeout=20)
        return proc, self.root / "work space/交付/TEST-01" / run

    def test_default_security_flags_receipt_and_input(self):
        source = self.root / "input"
        source.mkdir()
        (source / "sample.txt").write_text("input-sentinel")
        proc, run = self.launch(WORKER_INPUT_DIR=str(source))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        invocation = json.loads((run / "invocation.json").read_text())
        args = invocation["args"]
        self.assertEqual(args[0], "exec")
        for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules", "--json", "--skip-git-repo-check"):
            self.assertIn(flag, args)
        disabled = [args[i + 1] for i, x in enumerate(args[:-1]) if x == "--disable"]
        for feature in ("hooks", "memories", "apps", "plugins", "computer_use", "browser_use", "browser_use_external", "multi_agent"):
            self.assertIn(feature, disabled)
        for setting in ('default_permissions="frclyo_worker"', 'approvals_reviewer="user"', 'approval_policy="never"',
                        'shell_environment_policy.inherit="core"', 'shell_environment_policy.ignore_default_excludes=false',
                        'history.persistence="none"', 'web_search="disabled"', 'service_tier="priority"'):
            self.assertIn(setting, args)
        permission = next(x for x in args if x.startswith("permissions.frclyo_worker="))
        self.assertIn('"~/.frclyo"="deny"', permission)
        self.assertIn('"~/.local/share/opencode"="deny"', permission)
        self.assertIn(str(self.root / "work space/.runtime/opencode"), permission)
        self.assertNotIn("-s", args)
        self.assertEqual(invocation["proxy"], {})
        self.assertEqual((run / "输入/sample.txt").read_text(), "input-sentinel")
        receipt = json.loads((run / "回执.json").read_text())
        self.assertEqual(receipt["状态"], "执行完成")
        self.assertEqual(receipt["退出码"], 0)
        self.assertTrue(receipt["快速档"])
        self.assertFalse(receipt["网页搜索"])
        self.assertEqual(receipt["结果sha256"], hashlib.sha256((run / "结果.txt").read_bytes()).hexdigest())
        again, _ = self.launch()
        self.assertEqual(again.returncode, 3)

    def test_config_proxy_fast_and_explicit_switches(self):
        self.configure(proxy="http://127.0.0.1:12345", default_fast=False)
        proc, run = self.launch(effort="ultra", CODEX_WORKER_WEB="1", CODEX_WORKER_SUBAGENTS="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        invocation = json.loads((run / "invocation.json").read_text())
        args = invocation["args"]
        self.assertEqual(args[:2], ["--search", "exec"])
        self.assertNotIn("multi_agent", args)
        self.assertNotIn('service_tier="priority"', args)
        self.assertEqual(invocation["proxy"]["HTTPS_PROXY"], "http://127.0.0.1:12345")
        self.assertEqual(invocation["proxy"]["https_proxy"], "http://127.0.0.1:12345")
        proc, run = self.launch("r2", CODEX_WORKER_FAST="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads((run / "回执.json").read_text())["快速档"])
        proc, run = self.launch("r3", CODEX_WORKER_FAST="0")
        self.assertFalse(json.loads((run / "回执.json").read_text())["快速档"])

    def test_invalid_effort_ultra_and_zero_timeout_fail(self):
        for effort, timeout in (("invalid", "10"), ("ultra", "10"), ("high", "0")):
            proc, run = self.launch(effort=effort, timeout=timeout)
            self.assertEqual(proc.returncode, 2)
            self.assertFalse(run.exists())

    def test_custom_codex_home_controls_model_effort_and_fast(self):
        selected = self.root / 'custom Codex 中文'
        selected.mkdir()
        default = self.root / '.codex/models_cache.json'
        catalog = json.loads(default.read_text())
        (selected / 'models_cache.json').write_text(json.dumps(catalog))
        default.write_text('{broken-default-cache')
        proc, run = self.launch(CODEX_HOME=str(selected))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads((run / '回执.json').read_text())['快速档'])
        args = json.loads((run / 'invocation.json').read_text())['args']
        self.assertIn(str(selected), next(arg for arg in args if arg.startswith('permissions.frclyo_worker=')))
        catalog['models'][0]['service_tiers'] = []
        (selected / 'models_cache.json').write_text(json.dumps(catalog))
        proc, run = self.launch('r2', CODEX_HOME=str(selected))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(json.loads((run / '回执.json').read_text())['快速档'])
        catalog['models'][0]['supported_reasoning_levels'] = [{'effort': 'medium'}]
        (selected / 'models_cache.json').write_text(json.dumps(catalog))
        proc, run = self.launch('r3', CODEX_HOME=str(selected))
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(run.exists())

    def test_missing_custom_codex_cache_never_uses_default(self):
        proc, run = self.launch(CODEX_HOME=str(self.root / 'missing-codex'))
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(run.exists())

    def test_timeout_receipt(self):
        self.task.write_text("timeout")
        proc, run = self.launch(timeout="1")
        self.assertEqual(proc.returncode, 124, proc.stderr)
        receipt = json.loads((run / "回执.json").read_text())
        self.assertEqual(receipt["状态"], "超时")
        self.assertEqual(receipt["退出码"], 124)
        self.assertIsNone(receipt["结果sha256"])

    def test_worker_records_success_and_unsupported_model(self):
        proc, run = self.launch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        path = self.root / 'work space/模型可用性.json'
        row = json.loads(path.read_text())['records'][0]
        self.assertEqual((row['worker'], row['model'], row['cli_version'], row['status']),
                         ('codex', 'test-model', 'test', 'ok'))
        fake = self.root / 'commands/codex'
        error = {'type': 'error', 'message': json.dumps({'status': 400, 'error': {'message':
                 "The 'test-model' model is not supported when using Codex with a ChatGPT account."}})}
        fake.write_text(FAKE.replace("args = sys.argv[1:]", "print(" + repr(json.dumps(error)) + "); sys.exit(1)\nargs = sys.argv[1:]"))
        proc, run = self.launch('r2')
        self.assertNotEqual(proc.returncode, 0)
        row = json.loads(path.read_text())['records'][0]
        self.assertEqual(row['status'], 'unusable')
        self.assertIn(str(run / 'events.jsonl'), row['reason'])
        self.assertEqual(json.loads((run / '回执.json').read_text())['模型可用性'], 'unusable')

    def test_zero_cli_exit_with_failed_turn_is_not_success(self):
        fake = self.root / 'commands/codex'
        fake.write_text(FAKE.replace("'turn.completed'", "'turn.failed'"))
        proc, run = self.launch()
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(json.loads((run / '回执.json').read_text())['状态'], '失败')
        self.assertFalse((self.root / 'work space/模型可用性.json').exists())


if __name__ == "__main__":
    unittest.main()
