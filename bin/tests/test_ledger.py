#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实进程验证 CLI、并发与证据；所有测试文件都隔离在本文件所在目录。"""
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from unittest.mock import patch

# 不加载正式配置；每项测试明确设置账本和另外两个查重来源。
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
ledger = importlib.import_module("frclyo.ledger")


class LedgerTest(unittest.TestCase):
    def setUp(self):
        """隔离环境、路径和本地日期，让每个测试可单独复跑。"""
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-test-", dir=str(HERE))
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.book = self.base / "账本.jsonl"
        self.runs, self.deliver = self.base / "runs", self.base / "交付"
        self.runs.mkdir()
        self.deliver.mkdir()
        cfg = self.base / "config.json"
        cfg.write_text(json.dumps({"work_dir": str(self.base), "cards_dir": str(self.runs)}))
        self.env = dict(os.environ, FRCLYO_CONFIG=str(cfg))
        self.env.pop("AGENT_BRAIN", None)
        self.prefix = "CL-" + datetime.now().strftime("%Y%m%d") + "-"
        self.new_args = ["new", "--项目", "测试项目", "--标题", "测试标题", "--来源", "CL"]

    def cli(self, *args, ok=True, env=None):
        """走可执行 shell 入口，连参数转发和退出码一起检查。"""
        r = subprocess.run([str(HERE / "ledger")] + list(args), env=env or self.env,
                           capture_output=True, text=True, timeout=45)
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr)
        else:
            self.assertNotEqual(r.returncode, 0, r.stdout)
            self.assertTrue(r.stderr.strip())
        return r

    def new(self, *extra):
        """测试只从标准输出取号，避免依赖隐藏返回数据。"""
        return self.cli(*(self.new_args + list(extra))).stdout.strip()

    def cards(self):
        """读取临时账本最终状态，用于核对 CLI 实际写入。"""
        return ledger.fold(ledger.read_events(self.book))

    def paths(self):
        """库函数也使用与子进程一致的临时路径。"""
        with patch.dict(os.environ, self.env):
            return ledger.Paths()

    def evidence(self, key, rnd, status="执行完成", digest=True, receipt=True):
        """造可校验结果，日审必须自己算哈希而不能相信文字自述。"""
        d = self.deliver / key / rnd
        d.mkdir(parents=True, exist_ok=True)
        data = "真实结果\n".encode()
        (d / "结果.txt").write_bytes(data)
        if receipt:
            r = {"状态": status, "结果sha256": hashlib.sha256(data).hexdigest() if digest else "错误"}
            (d / "回执.json").write_text(json.dumps(r), encoding="utf-8")
        return d

    def test_source_priority_utf8_stdout(self):
        """参数胜过环境变量，中文与空格完整保留，stdout 只有号。"""
        env = dict(self.env, AGENT_BRAIN="CX")
        r = self.cli("开卡", "--项目", "项目 空格", "--标题", "中文标题", "--来源", "ZZ",
                     "--派活人", "大脑 甲", "--卡片", "/某处/任务 卡.txt", env=env)
        self.assertRegex(r.stdout, r"^ZZ-\d{8}-01\n$")
        self.assertEqual(r.stderr, "")
        c = self.cards()[r.stdout.strip()]
        self.assertEqual(c["来源"], "ZZ")
        self.assertEqual(c["派活人"], "大脑 甲")
        self.assertIn("中文标题", self.book.read_text())
        self.assertNotIn("\\u", self.book.read_text())
        self.assertTrue(self.cli("new", "--项目", "X", "--标题", "Y", env=env).stdout.startswith("CX-"))

    def test_ten_processes_allocate_unique_ids(self):
        """十个真实进程在 stdin 屏障后一起执行开卡。"""
        code = "import os,sys; sys.stdin.read(1); os.execv(sys.argv[1], sys.argv[1:])"
        procs = [subprocess.Popen([sys.executable, "-c", code, str(HERE / "ledger")] + self.new_args,
                                  env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for _ in range(10)]
        try:
            for p in procs:
                p.stdin.write("x")
                p.stdin.flush()
            ids = []
            for p in procs:
                out, err = p.communicate(timeout=45)
                self.assertEqual(p.returncode, 0, err)
                ids.append(out.strip())
            self.assertEqual(set(ids), {self.prefix + "%02d" % n for n in range(1, 11)})
            self.assertEqual(len(self.cards()), 10)
            self.assertFalse((self.base / ".账本.lock").exists())
        finally:
            for p in procs:
                if p.poll() is None:
                    p.kill()
                p.communicate()

    def test_hierarchy_and_maximum_depth(self):
        """子任务按字母，孙任务按数字，第三层明确拒绝。"""
        parent = self.new()
        a = self.new("--父", parent)
        self.assertEqual(a, parent + "-A")
        self.assertEqual(self.new("--父", parent), parent + "-B")
        self.assertEqual(self.new("--父", a), a + "1")
        self.assertEqual(self.new("--父", a), a + "2")
        self.cli(*(self.new_args + ["--父", a + "1"]), ok=False)
        self.assertEqual(self.cards()[a]["父任务"], parent)

    def test_three_sources_and_descendant_root_reservation(self):
        """三个来源轮流成为最大号，缺任何一个都不能通过测试。"""
        self.assertEqual(self.new(), self.prefix + "01")
        self.assertEqual(self.new(), self.prefix + "02")
        (self.runs / (self.prefix + "08-r2.txt")).touch()
        self.assertEqual(self.new(), self.prefix + "09")
        (self.deliver / (self.prefix + "20-A1")).mkdir()
        self.assertEqual(self.new(), self.prefix + "21")
        (self.runs / ("CX-" + datetime.now().strftime("%Y%m%d") + "-99-r1.txt")).touch()
        self.assertEqual(self.new(), self.prefix + "22")

    def test_hierarchy_external_sources(self):
        """子孙号同样查三处，孙号也预留其字母父号。"""
        parent = self.new()
        (self.runs / (parent + "-A-r1.txt")).touch()
        (self.deliver / (parent + "-B1")).mkdir()
        (self.runs / (parent + "-C-r1.txt")).touch()
        (self.deliver / (parent + "-C9")).mkdir()
        self.assertEqual(self.new("--父", parent), parent + "-D")
        self.assertEqual(self.new("--父", parent + "-C"), parent + "-C10")

    def test_next_round_numeric_and_read_only(self):
        """r10 大于 r9；同名普通文件和无关目录不算轮次。"""
        key = self.new()
        self.assertEqual(self.cli("下一轮", key).stdout, "r1\n")
        for rnd in ("r1", "r9", "r10", "rubbish"):
            (self.deliver / key / rnd).mkdir(parents=True)
        (self.deliver / key / "r99").touch()
        before = self.book.read_bytes()
        self.assertEqual(self.cli("next-round", key).stdout, "r11\n")
        self.assertEqual(self.book.read_bytes(), before)
        self.assertFalse((self.deliver / key / "r11").exists())

    def test_lifecycle_append_only_and_queries(self):
        """历史字节保持不变；备注不改变卡住状态，列表过滤已结束卡。"""
        key = self.new("--派活人", "Claude")
        before = self.book.read_bytes()
        self.cli("block", key, "需要确认")
        self.cli("note", key, "补充说明")
        self.assertEqual(self.cards()[key]["状态"], "卡住")
        self.cli("unblock", key)
        self.cli("done", key, "--结论", "未通过", "--摘要", "测试未过")
        self.assertTrue(self.book.read_bytes().startswith(before))
        self.assertIn("未通过", self.cli("show", key).stdout)
        self.assertIn("Claude", self.cli("查", key).stdout)
        self.assertIn("测试未过", self.cli("list", "--项目", "测试项目").stdout)
        self.assertNotIn(key, self.cli("列表", "--在途").stdout)
        self.assertNotIn(key, self.cli("list", "--项目", "别的项目").stdout)

    def test_drop_and_terminal_rejections(self):
        """完成和放弃都是终态，查询可用，所有状态修改都返回非零。"""
        for ending in ("完成", "放弃"):
            key = self.new()
            self.cli(ending, key, *(["--结论", "通过"] if ending == "完成" else ["不再需要"]))
            before = self.book.read_bytes()
            for args in (("note", key, "x"), ("block", key, "x"), ("unblock", key),
                         ("done", key, "--结论", "通过"), ("drop", key, "x")):
                self.cli(*args, ok=False)
            self.cli(*(self.new_args + ["--父", key]), ok=False)
            self.assertEqual(self.book.read_bytes(), before)
            self.assertIn(key, self.cli("show", key).stdout)

    def test_argument_and_nonexistent_errors(self):
        """缺参数、空文字、错来源、错结论、不存在号均不可成功。"""
        cases = [(), ("wat",), ("new",), ("new", "--项目", "x", "--标题", "y"),
                 ("new", "--项目", "", "--标题", "y", "--来源", "CL"),
                 tuple(self.new_args + ["--来源", "cl"]), tuple(self.new_args + ["--父", "CL-20000101-01"]),
                 ("show", "不存在"), ("next-round", "不存在"), ("note", "不存在", "x"),
                 ("block", "不存在", "x"), ("unblock", "不存在"), ("drop", "不存在", "x"),
                 ("done", "不存在", "--结论", "通过"), ("done", "x", "--结论", "也许")]
        for args in cases:
            with self.subTest(args=args):
                self.cli(*args, ok=False)
        key = self.new()
        self.cli("恢复", key, ok=False)
        self.cli("卡住", key, " ", ok=False)
        self.cli("卡住", key, "原因")
        self.cli("卡住", key, "再次", ok=False)

    def test_stale_lock_recovery_and_lost_owner(self):
        """死锁可回收；旧持有者恢复后不能写入，也不能删除新锁。"""
        directory = self.base / ".账本.lock"
        directory.mkdir()
        (directory / "持有者").write_text("已经死亡")
        os.utime(directory, (time.time() - 31,) * 2)
        self.assertEqual(self.new(), self.prefix + "01")
        old = ledger.LedgerLock(self.book).__enter__()
        os.utime(directory, (time.time() - 31,) * 2)
        with ledger.LedgerLock(self.book) as current:
            with self.assertRaises(ledger.LedgerError):
                old.append([ledger.event("备注", self.prefix + "01", 内容="不该写入")])
            old.__exit__()
            self.assertTrue(current.owns())

    def test_ten_processes_recover_stale_lock(self):
        """并发清死锁不能把某个进程刚获得的新锁清掉。"""
        directory = self.base / ".账本.lock"
        directory.mkdir()
        (directory / "持有者").write_text("死锁")
        os.utime(directory, (time.time() - 31,) * 2)
        self.test_ten_processes_allocate_unique_ids()

    def test_bad_ledger_does_not_append(self):
        """坏行和断尾明确报错并保留原字节，不静默跳过。"""
        key = self.new()
        clean = self.book.read_bytes()
        for bad in ("坏行\n", "\n", '{"动作":"未知"}\n'):
            before = clean + bad.encode()
            self.book.write_bytes(before)
            self.assertIn("行", self.cli("note", key, "x", ok=False).stderr)
            self.assertEqual(self.book.read_bytes(), before)
        self.book.write_bytes(clean.rstrip(b"\n"))
        self.cli(*self.new_args, ok=False)

    def test_audit_latest_numeric_round_and_checksum(self):
        """最新 r10 的坏校验码不能被旧 r9 的好回执掩盖。"""
        key = self.new()
        self.cli("done", key, "--结论", "通过")
        self.evidence(key, "r9")
        self.evidence(key, "r10", digest=False)
        lines, bad = ledger.audit(self.paths())
        self.assertEqual(bad, 1)
        self.assertIn("校验码不一致", "\n".join(lines))
        self.evidence(key, "r10")
        lines, bad = ledger.audit(self.paths())
        self.assertEqual(bad, 0)
        self.assertIn("r10", "\n".join(lines))

    def test_audit_missing_receipt_result_status(self):
        """缺轮次、缺回执、失败状态、缺结果、坏 JSON 都必须发现。"""
        key = self.new()
        self.cli("done", key, "--结论", "通过")
        self.assertEqual(ledger.audit(self.paths())[1], 1)
        d = self.evidence(key, "r1", receipt=False)
        self.assertEqual(ledger.audit(self.paths())[1], 1)
        self.evidence(key, "r1", status="失败")
        self.assertEqual(ledger.audit(self.paths())[1], 1)
        self.evidence(key, "r1")
        (d / "结果.txt").unlink()
        self.assertEqual(ledger.audit(self.paths())[1], 1)
        (d / "回执.json").write_text("坏回执")
        self.assertEqual(ledger.audit(self.paths())[1], 1)

    def test_audit_module_exit_code(self):
        """运行真实模块入口，检查失败和通过都保留退出码。"""
        key = self.new()
        self.cli("done", key, "--结论", "通过")
        for expected in (1, 0):
            proc = subprocess.run([sys.executable, "-m", "frclyo.audit"], cwd=HERE,
                                  env=self.env, capture_output=True, text=True, timeout=10)
            self.assertEqual(proc.returncode, expected, proc.stderr)
            self.assertEqual(len(json.loads(proc.stdout)["问题"]), expected)
            self.evidence(key, "r1")

    def test_sequence_over_99_and_other_day(self):
        """不同日期不串号，序号超过两位时自然增长，不截断已有历史。"""
        (self.runs / (self.prefix + "99-r1.txt")).touch()
        (self.runs / "CL-19990101-999-r1.txt").touch()
        self.assertEqual(self.new(), self.prefix + "100")

    def test_source_required_for_children_and_unknown_parent_format(self):
        """指定父任务也不猜来源，格式错误父号明确返回失败。"""
        key = self.new()
        self.cli("new", "--项目", "x", "--标题", "y", "--父", key, ok=False)
        self.cli(*(self.new_args + ["--父", key + "-bad"]), ok=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
