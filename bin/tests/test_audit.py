"""证据核对只看账本；测试坏数据、过滤规则和只读性质。"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

# 使用项目公共库，不读取用户账本。
BIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BIN))
from frclyo import audit, ledger


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="audit-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = self.root / "config.json"
        self.cfg.write_text(json.dumps({"work_dir": str(self.root / "work")}))
        self.env = dict(os.environ, FRCLYO_CONFIG=str(self.cfg))
        with patch.dict(os.environ, self.env):
            self.paths = ledger.Paths()

    def record(self, verdict="通过"):
        """在临时账本构造符合既有事件格式的完成卡。"""
        events = [ledger.event("开卡", "CL-20000101-01", 标题="证据", 项目="测试", 来源="CL", 派活人="测试", 父任务=""),
                  ledger.event("完成", "CL-20000101-01", 结论=verdict, 摘要="测试")]
        with ledger.LedgerLock(self.paths.ledger) as lock:
            lock.append(events)

    def test_missing_ledger_is_read_only_empty(self):
        lines, bad = audit.audit(self.paths)
        self.assertEqual((lines, bad), (["已核对完成卡 0 张，问题 0 个"], 0))
        self.assertFalse(self.paths.ledger.parent.exists())

    def test_failed_cards_and_unregistered_delivery_not_audited(self):
        self.record("未通过")
        (self.paths.deliver / "UNREGISTERED" / "r1").mkdir(parents=True)
        before = self.paths.ledger.read_bytes()
        lines, bad = audit.audit(self.paths)
        self.assertEqual((lines[-1], bad), ("已核对完成卡 0 张，问题 0 个", 0))
        self.assertEqual(self.paths.ledger.read_bytes(), before)
        self.assertFalse((self.paths.ledger.parent / ".账本.lock").exists())

    def test_receipt_wrong_shape_and_multiple_latest_round_failures(self):
        self.record()
        for rnd in ("r2", "r10"):
            directory = self.paths.deliver / "CL-20000101-01" / rnd
            directory.mkdir(parents=True)
            (directory / "结果.txt").write_bytes(b"evidence")
            (directory / "回执.json").write_text(json.dumps({"状态": "执行完成", "结果sha256": hashlib.sha256(b"evidence").hexdigest()}))
        before = self.paths.ledger.read_bytes()
        self.assertEqual(audit.audit(self.paths)[1], 0)
        for invalid in ("[]", "null", "{broken", '{"状态":"执行完成"}'):
            (directory / "回执.json").write_text(invalid)
            self.assertEqual(audit.audit(self.paths)[1], 1)
        self.assertEqual(self.paths.ledger.read_bytes(), before)

    def test_bad_ledger_and_bad_config_cli_nonzero(self):
        self.paths.ledger.parent.mkdir()
        self.paths.ledger.write_text("坏行\n")
        command = [sys.executable, "-m", "frclyo.audit"]
        proc = subprocess.run(command, cwd=BIN, env=self.env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("第 1 行", proc.stdout)
        self.cfg.write_text("null")
        proc = subprocess.run(command, cwd=BIN, env=self.env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("配置", proc.stdout)


if __name__ == "__main__":
    unittest.main()
