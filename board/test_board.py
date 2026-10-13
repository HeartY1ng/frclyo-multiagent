#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
看板的自动测试：用临时造的假数据，检查每种情况看板都判断对了，并且安全限制有效。
运行：python3 test_board.py
"""
import importlib
import io
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class BoardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # ---------- 造一套假的交付目录、任务卡和账本 ----------
        cls.tmp = tempfile.mkdtemp(prefix="board-test-", dir=HERE)
        cls.deliver = os.path.join(cls.tmp, "交付")
        cls.runs = os.path.join(cls.tmp, "runs")
        os.makedirs(cls.runs)
        t = time.time()

        def round_dir(task, rnd, files):
            d = os.path.join(cls.deliver, task, rnd)
            os.makedirs(d, exist_ok=True)
            for name, content in files.items():
                mode = "wb" if isinstance(content, bytes) else "w"
                with open(os.path.join(d, name), mode, **({} if mode == "wb" else {"encoding": "utf-8"})) as f:
                    f.write(content)
            return d

        # 假工人进程：名字里带 worker.sh，看板才认它是工人
        cls.fake = os.path.join(cls.tmp, "fake-worker.sh")
        with open(cls.fake, "w") as f:
            f.write("#!/bin/bash\nsleep 120\n")
        os.chmod(cls.fake, 0o755)
        cls.proc = subprocess.Popen([cls.fake], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

        # 1) 正在跑
        round_dir("CL-20991231-01", "r1", {
            "任务.txt": "项目：测试项目\n# 正在跑的活\n",
            "运行信息.json": json.dumps({"工人": "opencode", "模型": "deepseek/deepseek-v4-pro", "档位": "high",
                                     "开始": iso(t - 60), "超时秒": 600, "进程号": cls.proc.pid}),
            "events.jsonl": json.dumps({"type": "tool_use", "timestamp": int(t * 1000), "part": {"tool": "bash", "state": {
                "status": "completed", "input": {"command": "pytest -q"}, "output": "3 passed"}}}) + "\n",
        })
        # 2) 中断：进程号不存在
        round_dir("CL-20991231-02", "r1", {
            "任务.txt": "项目：测试项目\n# 中断的活\n",
            "运行信息.json": json.dumps({"工人": "opencode", "模型": "xiaomi/mimo-v2.6-pro", "开始": iso(t - 600), "超时秒": 600, "进程号": 999999}),
        })
        # 3) 需要输入，结果里有决策披露
        round_dir("CL-20991231-03", "r1", {
            "任务.txt": "项目：测试项目\n# 等输入的活\n",
            "回执.json": json.dumps({"工人": "opencode", "模型": "deepseek/deepseek-flash", "开始": iso(t - 300), "结束": iso(t - 200),
                                  "退出码": 0, "状态": "执行完成", "用量": {"input": 100, "output": 20, "reasoning": 5}}),
            "结果.txt": "## 1. 结论\n没做完\n## 4. 决策披露\n- 空行跳过\n## 5. 状态\n状态：需要输入 —— 标点怎么切没写\n",
        })
        # 4) 做完并验收通过（表现记录里写了通过），里面放一个二进制文件和一个带密钥的文件
        round_dir("CL-20991231-04", "r1", {
            "任务.txt": "项目：别的项目\n# 通过的活\n",
            "回执.json": json.dumps({"工人": "codex", "模型": "gpt-6.1-sol", "思考档位": "high", "开始": iso(t - 900), "结束": iso(t - 800),
                                  "退出码": 0, "状态": "执行完成", "用量": {"input_tokens": 1000, "output_tokens": 50}}),
            "结果.txt": "全部完成\n",
            "图.png": b"\x89PNG\x00\x00\x00binary",
            "泄露.txt": "key=sk-abcdefghijklmnopqrstuvwxyz123456\nsha=" + "a" * 64 + "\n",
        })
        # 5) 测试目录（不是正式任务号）
        round_dir("GATE-20991231", "x", {"回执.json": json.dumps({"状态": "超时", "退出码": 124, "开始": iso(t - 50), "结束": iso(t - 40)})})
        with open(os.path.join(cls.runs, "表现记录.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"任务号": "CL-20991231-04", "轮次": "r1", "通过验收": True}, ensure_ascii=False) + "\n")

        # 全部路径与端口走临时配置；保留原四块规则的用例，登记数据改用账本。
        cls.ledger_path = os.path.join(cls.tmp, "账本.jsonl")
        cls.config_path = os.path.join(cls.tmp, "config.json")
        cls.cfg = {"version": 1, "work_dir": cls.tmp, "board_port": 18799,
                   "cards_dir": cls.runs, "perf_file": os.path.join(cls.runs, "表现记录.jsonl"),
                   "codex": {"proxy": "", "default_fast": True}}
        with open(cls.config_path, "w", encoding="utf-8") as fh:
            json.dump(cls.cfg, fh)
        cls.env_patch = patch.dict(os.environ, {"FRCLYO_CONFIG": cls.config_path,
                                               "BOARD_NO_BACKGROUND": "1", "BOARD_BALANCE": "0"})
        cls.env_patch.start()
        # 直接加载仓库公共库，测试不再寻找上游交付或旧账本。
        cls.old_sys_path = sys.path[:]
        sys.path.insert(0, os.path.join(os.path.dirname(HERE), "bin"))
        sys.path.insert(0, HERE)
        import board
        cls.b = importlib.reload(board)

    @classmethod
    def tearDownClass(cls):
        # 连同 sleep 子进程一起结束，不然它会占着输出管道
        os.killpg(cls.proc.pid, 9)
        cls.proc.wait()
        shutil.rmtree(cls.tmp)
        cls.env_patch.stop()
        sys.path[:] = cls.old_sys_path

    def setUp(self):
        # 每个测试重建登记基线；不改变原有状态规则和断言。
        if os.path.exists(self.ledger_path):
            os.unlink(self.ledger_path)
        self.b._state_cache.clear()
        self.b.HEALTH.update(codex=None, 余额=[], 日审=None)
        self.ledger_events("CL-20991231-05", ("卡住", {"原因": "契约冲突，需要拍板"}), title="卡住的活", project="测试项目")
        self.ledger_events("CL-20991231-03", title="看板标题", project="测试项目")
        self.ledger_events("CL-20991231-06", title="刚开卡", project="测试项目")

    def ledger_events(self, key, *actions, title="账本标题", project="账本项目"):
        # 通过真实账本格式造事件，不伪造看板内部状态，检验整条读取链路。
        events = [self.b.ledger.event("开卡", key, 标题=title, 项目=project, 来源=key[:2], 派活人="Codex 甲", 父任务="")]
        for action, fields in actions:
            events.append(self.b.ledger.event(action, key, **fields))
        with open(self.ledger_path, "a", encoding="utf-8") as fh:
            for e in events:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        return events

    # ---------- 1. 四块内容判断对不对 ----------
    def test_sections(self):
        s = self.b.build_state()
        running = {x["任务"] for x in s["正在干活"]}
        self.assertIn("CL-20991231-01", running)
        self.assertIn("CL-20991231-06", running)        # 刚认领：显示"准备中"
        self.assertNotIn("CL-20991231-04", running)     # 跑完没收卡：不算在干活
        self.assertIn("GATE-20991231", {x["任务"] for x in s["最近完成"]} | {"GATE-20991231"})  # 测试目录不报错
        attn = {(x.get("任务"), x["事情"]) for x in s["要你处理"]}
        self.assertIn(("CL-20991231-02", "工人中断了，没有交回执"), attn)
        self.assertIn(("CL-20991231-03", "工人停下来等输入"), attn)
        self.assertIn(("CL-20991231-05", "卡住了"), attn)
        blocked = next(x for x in s["要你处理"] if x.get("任务") == "CL-20991231-05")
        self.assertIn("契约冲突", blocked["细节"])
        # 红色排在前面
        levels = [x["级别"] for x in s["要你处理"]]
        self.assertEqual(levels, sorted(levels, key=lambda v: {"red": 0, "amber": 1, "gray": 2}[v]))
        recent = {x["任务"]: x for x in s["最近完成"]}
        self.assertEqual(recent["CL-20991231-04"]["结论"], "通过")
        self.assertEqual(recent["CL-20991231-03"]["结论"], "等输入")
        self.assertEqual(recent["CL-20991231-03"]["标题"], "看板标题")   # 看板上的标题优先
        self.assertEqual(recent["CL-20991231-03"]["tokens"], 125)
        # "全部"不含测试目录
        self.assertNotIn("GATE-20991231", recent)

    def test_project_filter(self):
        s = self.b.build_state("别的项目")
        self.assertEqual([x["任务"] for x in s["最近完成"]], ["CL-20991231-04"])
        self.assertEqual(s["正在干活"], [])      # 在跑的那件属于"测试项目"
        self.assertTrue(all(x.get("项目") in (None, "别的项目") for x in s["要你处理"]))
        s2 = self.b.build_state("测试与验收")
        self.assertIn("GATE-20991231", [x["任务"] for x in s2["最近完成"]])

    # ---------- 2. 详情 ----------
    def test_task_detail(self):
        d = self.b.build_task("CL-20991231-03")
        r = d["轮次"][0]
        self.assertEqual(r["决策披露"], "- 空行跳过")
        self.assertIn("需要输入", r["需输入"])
        self.assertTrue(any(f["路径"] == "结果.txt" for f in r["文件"]))
        self.assertIsNone(self.b.build_task("不存在"))

    def test_proc_alive_not_fooled(self):
        # 系统进程 mdworker.shared 的名字里有 "worker.sh"，不能被当成工人
        import subprocess as sp
        out = sp.run(["pgrep", "-f", "mdworker"], capture_output=True, text=True).stdout.split()
        for pid in out[:3]:
            self.assertFalse(self.b.proc_alive(pid))
        self.assertTrue(self.b.proc_alive(self.proc.pid))

    def test_needs_input_forms(self):
        f = self.b.needs_input
        self.assertTrue(f("1. 结论：需要输入（契约没写标点）"))
        self.assertTrue(f("**状态**：需要输入"))
        self.assertIsNone(f("1. 结论：完成\n4. 决策披露：没有需要输入的地方"))

    # ---------- 3. 安全：文件只能看本轮目录里的，二进制不显示，密钥打码 ----------
    def test_file_guard(self):
        f = self.b.read_round_file
        self.assertIsNone(f("CL-20991231-04", "r1", "../../CL-20991231-03/r1/结果.txt")[0])
        self.assertIsNone(f("CL-20991231-04", "..", "r1/结果.txt")[0])
        self.assertIsNone(f("../x", "r1", "结果.txt")[0])
        self.assertIsNone(f("CL-20991231-04", "r1", "/etc/passwd")[0])
        self.assertIsNone(f("CL-20991231-04", "r1", "图.png")[0])
        text, err = f("CL-20991231-04", "r1", "泄露.txt")
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", text)
        self.assertIn("a" * 64, text)       # 校验码不打码

    def test_file_guard_rejects_directory_and_file_symlinks(self):
        # 文件、轮次或任务目录的链接均不能绕过本轮边界。
        base = os.path.join(self.deliver, "CL-20991231-04", "r1")
        target = os.path.join(self.deliver, "CL-20991231-03", "r1")
        links = [(os.path.join(base, "outside.txt"), os.path.join(target, "结果.txt")),
                 (os.path.join(self.deliver, "CL-20991231-04", "linked"), target),
                 (os.path.join(self.deliver, "LINK-TEST"), self.tmp)]
        try:
            for path, dest in links:
                os.symlink(dest, path)
            for task, rnd, rel in [("CL-20991231-04", "r1", "outside.txt"),
                                   ("CL-20991231-04", "linked", "结果.txt"),
                                   ("LINK-TEST", "runs", "表现记录.jsonl"),
                                   ("CL-20991231-04", ".", "r1/结果.txt"),
                                   ("CL-20991231-04", "..", "CL-20991231-03/r1/结果.txt")]:
                self.assertIsNone(self.b.read_round_file(task, rnd, rel)[0])
        finally:
            for path, _ in links:
                os.unlink(path)

    # ---------- 4. 事件流翻译 ----------
    def test_events(self):
        p = os.path.join(self.tmp, "ev.jsonl")
        with open(p, "w", encoding="utf-8") as fh:
            for e in [
                {"type": "thread.started"},
                {"type": "item.started", "item": {"type": "command_execution", "command": "/bin/zsh -lc \"ls -la\""}},
                {"type": "item.completed", "item": {"type": "command_execution", "command": "x", "exit_code": 1, "aggregated_output": "boom"}},
                {"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "/w/a.py", "kind": "add"}]}},
                {"type": "step_finish", "part": {"cost": 0.002}},
                {"type": "step_finish", "part": {"cost": 0.001}},
                {"type": "error", "error": {"name": "X", "data": {"message": "server down"}}},
            ]:
                fh.write(json.dumps(e) + "\n")
            fh.write("这一行不是 JSON\n")
        ev = self.b.parse_events(p, "/w")
        kinds = [x["k"] for x in ev["条目"]]
        self.assertEqual(kinds, ["start", "tool", "warn", "edit", "error"])
        self.assertEqual(ev["条目"][1]["s"], "运行命令：ls -la")
        self.assertIn("a.py（新建）", ev["条目"][3]["s"])
        self.assertAlmostEqual(ev["费用"], 0.003)
        self.assertEqual(ev["步数"], 2)

    # ---------- 5. 读不到交付目录时不崩，给出说明 ----------
    def test_missing_deliver(self):
        old = self.b.DELIVER
        try:
            self.b.DELIVER = os.path.join(self.tmp, "没有这个目录")
            self.b._probe["时间"] = 0
            s = self.b.build_state()
            self.assertTrue(any(x["事情"] == "看板有数据读不到" for x in s["要你处理"]))
        finally:
            self.b.DELIVER = old
            self.b._probe["时间"] = 0

    # ---------- 6. 网页服务：只认本机地址，只接受 GET ----------
    def memory_http(self, request):
        """把原始 HTTP 字节送进同一个 Handler，并用标准库解析真实响应字节。"""
        req = request if isinstance(request, urllib.request.Request) else urllib.request.Request(request)
        url = urllib.parse.urlsplit(req.full_url)
        headers = dict(req.header_items())
        headers.setdefault("Host", url.netloc)
        headers["Connection"] = "close"
        data = req.data or b""
        if data:
            headers["Content-Length"] = str(len(data))
        raw = (req.get_method() + " " + (url.path or "/") + ("?" + url.query if url.query else "") +
               " HTTP/1.1\r\n" + "".join(k + ": " + v + "\r\n" for k, v in headers.items()) + "\r\n").encode("utf-8") + data

        class MemorySocket:
            # Handler 的读写接口与 socket 相同，但无网络权限需求。
            def __init__(self, value):
                self.incoming = value
                self.outgoing = bytearray()

            def makefile(self, *args):
                return io.BytesIO(self.incoming)

            def sendall(self, value):
                self.outgoing.extend(value)

        sock = MemorySocket(raw)
        self.b.Handler(sock, ("127.0.0.1", 12345), object())
        response = http.client.HTTPResponse(MemorySocket(bytes(sock.outgoing)))
        response.begin()
        if response.status >= 400:
            error = urllib.error.HTTPError(req.full_url, response.status, response.reason, response.headers, response)
            response.close()
            raise error
        return response

    def test_http(self):
        # 正常环境仍走真实 TCP；沙箱禁止 bind 时仅替换传输，不跳过原安全断言。
        srv = None
        open_http = urllib.request.urlopen
        try:
            srv = self.b.ThreadingHTTPServer(("127.0.0.1", self.b.PORT), self.b.Handler)
        except PermissionError:
            print("HTTP 自检：沙箱禁止监听端口，改用内存传输运行真实 Handler", file=sys.stderr)
            open_http = self.memory_http
        else:
            th = threading.Thread(target=srv.serve_forever, daemon=True)
            th.start()
        try:
            base = "http://127.0.0.1:%d" % self.b.PORT
            with open_http(base + "/") as r:
                self.assertIn("工作看板", r.read().decode("utf-8"))
            with open_http(base + "/api/state") as r:
                self.assertIn("要你处理", json.loads(r.read().decode("utf-8")))
            with open_http(base + "/api/task?id=CL-20991231-04") as r:
                self.assertEqual(json.loads(r.read().decode("utf-8"))["结论"], "通过")
            # 伪造的 Host（DNS 重绑定攻击的样子）要被拒绝
            req = urllib.request.Request(base + "/api/state", headers={"Host": "evil.example:%d" % self.b.PORT})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                open_http(req)
            self.assertEqual(cm.exception.code, 403)
            # 不接受 POST
            with self.assertRaises(urllib.error.HTTPError) as cm:
                open_http(urllib.request.Request(base + "/api/state", data=b"x", method="POST"))
            self.assertEqual(cm.exception.code, 501)
            # 按任务卡只接受 GET，HEAD 也不能触发查询或日志写入。
            with self.assertRaises(urllib.error.HTTPError) as cm:
                open_http(urllib.request.Request(base + "/api/log?m=x", method="HEAD"))
            self.assertEqual(cm.exception.code, 501)
            # 错误日志接口：别的网站发来的拒绝；自己页面发来的接受，但每分钟最多 20 条
            with self.assertRaises(urllib.error.HTTPError) as cm:
                open_http(urllib.request.Request(base + "/api/log?m=x", headers={"Sec-Fetch-Site": "cross-site"}))
            self.assertEqual(cm.exception.code, 403)
            before = len(self.b._log_times)
            for _ in range(30):
                open_http(urllib.request.Request(base + "/api/log?m=test", headers={"Sec-Fetch-Site": "same-origin"}))
            self.assertLessEqual(len(self.b._log_times), 20)
            # 文件接口拒绝跳出本轮目录
            with self.assertRaises(urllib.error.HTTPError) as cm:
                open_http(base + "/api/file?id=CL-20991231-04&round=r1&path=..%2F..%2FCL-20991231-03%2Fr1%2F结果.txt".replace("结果", "%E7%BB%93%E6%9E%9C"))
            self.assertEqual(cm.exception.code, 400)
        finally:
            if srv is not None:
                srv.shutdown()
                srv.server_close()


    def test_ledger_ready_and_custom_source(self):
        """CX 和未来的两字母来源都属于正式任务；开卡无轮次显示准备中。"""
        for key in ("CX-20991231-01", "ZZ-20991231-01"):
            self.ledger_events(key)
            state = self.b.build_state("账本项目")
            self.assertIn(key, {x["任务"] for x in state["正在干活"]})
            self.assertEqual(self.b.build_task(key)["结论"], "准备中")
            self.assertNotIn(key, {x["任务"] for x in state["最近完成"]})

    def test_ledger_block_reason_and_unblock(self):
        """卡住进入要你处理并显示原因；恢复后重回准备中。"""
        key = "CX-20991231-02"
        self.ledger_events(key, ("卡住", {"原因": "需要确认接口"}))
        row = next(x for x in self.b.build_state()["要你处理"] if x.get("任务") == key)
        self.assertIn("需要确认接口", row["细节"])
        self.assertIn("Codex 甲", row["怎么办"])
        with open(self.ledger_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(self.b.ledger.event("恢复", key), ensure_ascii=False) + "\n")
        self.assertEqual(self.b.build_task(key)["结论"], "准备中")

    def test_ledger_done_verdict_overrides_perf(self):
        """旧表现记录是通过，账本为未通过时，以账本结论为准。"""
        key = "CL-20991231-04"
        self.ledger_events(key, ("完成", {"结论": "未通过", "摘要": "验收发现问题"}))
        row = next(x for x in self.b.build_state()["最近完成"] if x["任务"] == key)
        self.assertEqual(row["结论"], "未通过")
        detail = self.b.build_task(key)
        self.assertEqual(detail["轮次"][-1]["验收"], "未通过")
        self.assertEqual(detail["标题"], "账本标题")

    def test_ledger_done_without_round(self):
        """没有轮次的通过卡也显示账本结论，证据缺失由日审负责报告。"""
        key = "CX-20991231-03"
        self.ledger_events(key, ("完成", {"结论": "通过", "摘要": "人工验收"}))
        row = next(x for x in self.b.build_state()["最近完成"] if x["任务"] == key)
        self.assertEqual((row["结论"], row["结论级别"]), ("通过", "ok"))

    def test_ledger_drop_gray_suppresses_old_interrupt(self):
        """放弃显示灰色，旧轮次的中断提醒不会继续要求处理。"""
        for key in ("CL-20991231-02", "CX-20991231-04"):
            self.ledger_events(key, ("放弃", {"原因": "方案变更"}))
            state = self.b.build_state()
            row = next(x for x in state["最近完成"] if x["任务"] == key)
            self.assertEqual((row["结论"], row["结论级别"]), ("已放弃", "gray"))
            self.assertNotIn(key, {x.get("任务") for x in state["要你处理"]})

    def test_ledger_detail_all_events_and_dispatcher(self):
        """抽屉包含完整中文事件和派活人，前端仍使用原刷新接口。"""
        key = "CX-20991231-05"
        events = self.ledger_events(key, ("备注", {"内容": "补充契约"}), ("卡住", {"原因": "缺输入"}),
                                    ("恢复", {}), ("完成", {"结论": "通过", "摘要": "核对完成"}))
        detail = self.b.build_task(key)
        self.assertEqual(detail["看板"]["派活人"], "Codex 甲")
        self.assertEqual([x["内容"] for x in detail["看板"]["事件"]], events)
        html = self.b.read_text(os.path.join(HERE, "index.html"))
        self.assertIn("账本记录", html)
        self.assertNotIn("看板记录", html)
        self.assertIn(".badge.gray", html)

    def test_ledger_corrupt_is_visible(self):
        """坏账本显式出现在数据错误提醒，不假装账本为空。"""
        with open(self.ledger_path, "w", encoding="utf-8") as fh:
            fh.write("坏行\n")
        self.assertTrue(any("读账本失败" in x["细节"] for x in self.b.build_state()["要你处理"]))

    # ---------- 7. 配置与公共库：临时配置、默认值和优先导入 ----------
    def test_config_paths_and_port(self):
        self.assertEqual(self.b.PORT, self.cfg["board_port"])
        self.assertEqual(self.b.DELIVER, self.deliver)
        self.assertEqual(self.b.RUNS, self.runs)
        self.assertEqual(self.b.PERF, os.path.join(self.runs, "表现记录.jsonl"))
        self.assertEqual(self.b.LEDGER, self.ledger_path)
        # 原环境变量不能盖过契约规定的唯一配置来源。
        with patch.dict(os.environ, {"BOARD_PORT": "1", "BOARD_DELIVER": "/wrong"}):
            self.assertEqual(self.b.config.load()["board_port"], self.cfg["board_port"])
            self.assertEqual(self.b.config.path_of("deliver_dir"), self.deliver)

    def test_config_defaults_and_tilde(self):
        path = os.path.join(self.tmp, "partial.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"work_dir": "~/isolated-work", "cards_dir": "~/cards", "perf_file": "~/perf.jsonl",
                       "codex": {"proxy": "http://127.0.0.1:1234"},
                       "providers": {"deepseek": {"api_host": "balance.example"}}}, fh)
        with patch.dict(os.environ, {"FRCLYO_CONFIG": path}):
            cfg = self.b.config.load()
            self.assertEqual(cfg["board_port"], 8789)
            self.assertTrue(cfg["codex"]["default_fast"])
            self.assertEqual(cfg["providers"]["deepseek"]["balance_api"], "deepseek")
            self.assertNotIn("moonshotai-cn", cfg["providers"])
            self.assertEqual(self.b.config.path_of("cards_dir"), os.path.expanduser("~/cards"))
            self.assertEqual(self.b.config.path_of("perf_file"), os.path.expanduser("~/perf.jsonl"))
            self.assertEqual(self.b.config.path_of("deliver_dir"), os.path.expanduser("~/isolated-work/交付"))
        with patch.dict(os.environ, {"FRCLYO_CONFIG": os.path.join(self.tmp, "absent.json")}):
            self.assertEqual(self.b.config.path_of("cards_dir"), os.path.expanduser("~/AgentWork/任务卡"))
            self.assertEqual(self.b.config.load()["codex"]["proxy"], "")

    def test_runtime_modules_are_real(self):
        # 确认四个公共库就是生产包，避免回归到独立配置解析。
        from frclyo import config, keys, ledger, audit
        for name, module in (("config", config), ("keys", keys), ("ledger", ledger), ("audit", audit)):
            self.assertIs(getattr(self.b, name), module)

    def test_audit_populates_health_and_attention(self):
        # 通过卡缺回执由真实日审报告，并同步到健康条和提醒。
        self.ledger_events("CL-20991231-09", ("完成", {"结论": "通过", "摘要": "测试"}), title="缺回执")
        self.b.refresh_health("audit")
        self.assertEqual(self.b.HEALTH["日审"]["核对卡数"], 1)
        self.assertEqual(len(self.b.HEALTH["日审"]["问题"]), 1)
        self.assertEqual(self.b.AUDIT_INTERVAL, 3600)

    # ---------- 8. 健康条：启动真实的假 stdio 进程验证协议、超时和回收 ----------
    def fake_app_server(self, mode="ok"):
        # 不碰真实账户；替换命令后仍由操作系统建立管道、进程组，执行完整握手。
        from contextlib import contextmanager
        script = os.path.join(self.tmp, "fake_app_server.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write('''import json, sys, time
mode = sys.argv[1]
def receive():
    return json.loads(sys.stdin.readline())
def send(value):
    print(json.dumps(value), flush=True)
first = receive()
assert first["id"] == 1 and first["method"] == "initialize"
assert first["params"]["clientInfo"]["name"] == "board-probe"
if mode == "init_timeout":
    time.sleep(30)
send({"method": "notice"})
send({"id": 1, "result": {"userAgent": "fake"}})
assert receive() == {"method": "initialized"}
assert receive() == {"id": 2, "method": "account/rateLimits/read"}
if mode == "timeout":
    time.sleep(30)
elif mode == "partial":
    sys.stdout.write('{"id": 2,')
    sys.stdout.flush()
    time.sleep(30)
elif mode == "error":
    send({"id": 2, "error": {"code": -32000, "message": "private-error-must-not-escape"}})
elif mode == "invalid":
    send({"id": 2, "result": {"rateLimits": {"primary": {}}}})
elif mode == "exit":
    sys.exit(0)
else:
    send({"id": 999, "result": {}})
    send({"id": 2, "result": {"rateLimits": {"primary": {
        "usedPercent": 16.5, "windowDurationMins": 10080, "resetsAt": 1792487478}, "planType": "plus"}}})
time.sleep(30)
''')
        real_popen = subprocess.Popen
        processes, environments = [], []

        def start(command, **kwargs):
            self.assertEqual(command, ["codex", "app-server"])
            environments.append(kwargs["env"])
            process = real_popen([sys.executable, script, mode], **kwargs)
            processes.append(process)
            return process

        @contextmanager
        def fixture():
            with patch.object(self.b.subprocess, "Popen", side_effect=start):
                try:
                    yield environments
                finally:
                    for process in processes:
                        self.assertIsNotNone(process.poll(), "查询后必须回收进程")
                        self.assertTrue(process.stdin.closed)
                        self.assertTrue(process.stdout.closed)
        return fixture()

    def test_quota_protocol_proxy_and_cleanup(self):
        with patch.dict(self.b.CONFIG, {"codex": {"proxy": "http://127.0.0.1:1234"}}), self.fake_app_server() as envs:
            quota = self.b.check_codex_quota()
        self.assertEqual(quota["已用"], 16.5)
        self.assertEqual(quota["窗口分钟"], 10080)
        self.assertEqual(quota["重置"], 1792487478)
        self.assertLess(abs(time.time() - quota["更新时间"]), 2)
        self.assertEqual(envs[0]["HTTPS_PROXY"], "http://127.0.0.1:1234")
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://unwanted.invalid"}), self.fake_app_server() as envs:
            self.b.check_codex_quota()
        self.assertNotIn("HTTPS_PROXY", envs[0])

    def test_quota_timeouts_including_partial_line(self):
        for mode in ("init_timeout", "timeout", "partial"):
            with self.subTest(mode=mode), patch.object(self.b, "QUERY_TIMEOUT", 0.25), self.fake_app_server(mode):
                started = time.monotonic()
                self.b.refresh_health("codex")
                self.assertLess(time.monotonic() - started, 2)
                self.assertEqual(self.b.HEALTH["codex"]["错误"], "查询超时")
                self.assertNotIn("更新时间", self.b.HEALTH["codex"])

    def test_quota_failure_keeps_value_and_timestamp_then_recovers(self):
        with self.fake_app_server():
            self.b.refresh_health("codex")
        previous = dict(self.b.HEALTH["codex"])
        for mode in ("error", "invalid", "exit", "timeout"):
            with self.subTest(mode=mode), patch.object(self.b, "QUERY_TIMEOUT", 0.25), self.fake_app_server(mode):
                self.b.refresh_health("codex")
            current = self.b.HEALTH["codex"]
            self.assertEqual(current["已用"], previous["已用"])
            self.assertEqual(current["更新时间"], previous["更新时间"])
            self.assertIn("错误", current)
            self.assertNotIn("private-error", json.dumps(current))
        with self.fake_app_server():
            self.b.refresh_health("codex")
        self.assertNotIn("错误", self.b.HEALTH["codex"])
        self.assertGreater(self.b.HEALTH["codex"]["更新时间"], previous["更新时间"])

    def test_balances_success_uses_keys_hosts_and_timeout(self):
        # 模拟 HTTP opener，覆盖真实请求构造和超时参数，同时不进行网络访问。
        from unittest.mock import Mock
        def opened(request, timeout):
            self.assertEqual(timeout, 10)
            self.assertEqual(request.get_header("Authorization"), "Bearer unit-test-key")
            if request.full_url == "https://balance.example/user/balance":
                data = {"balance_infos": [{"total_balance": "12.34", "currency": "CNY"}], "is_available": True}
            else:
                self.assertEqual(request.full_url, "https://api.moonshot.cn/v1/users/me/balance")
                data = {"data": {"available_balance": 56.78}}
            return io.BytesIO(json.dumps(data).encode())
        opener = Mock()
        opener.open.side_effect = opened
        cfg = json.loads(json.dumps(self.b.CONFIG))
        cfg["providers"]["deepseek"]["api_host"] = "balance.example"
        with patch.object(self.b, "CONFIG", cfg), patch.object(self.b.keys, "get_key", return_value="unit-test-key") as get_key, \
                patch.object(self.b.urllib.request, "build_opener", return_value=opener):
            self.b.refresh_health("balances")
        rows = {row["名字"]: row for row in self.b.HEALTH["余额"]}
        self.assertEqual(rows["DeepSeek"]["余额"], 12.34)
        self.assertEqual(rows["Kimi"]["余额"], 56.78)
        self.assertIn("更新时间", rows["Kimi"])
        self.assertEqual(rows["MiMo"]["说明"], "没有余额接口")
        self.assertEqual({call.args[0] for call in get_key.call_args_list}, {"deepseek", "moonshotai-cn", "xiaomi"})

    def test_balances_failures_preserve_each_provider_and_recover(self):
        def response(url, key):
            return {"balance_infos": [{"total_balance": "0", "currency": "CNY"}]} if "/user/" in url else {"data": {"available_balance": 8}}
        with patch.object(self.b.keys, "get_key", side_effect=lambda provider: "unit-test-key" if provider != "xiaomi" else None):
            with patch.object(self.b, "_get_json", side_effect=response):
                self.b.refresh_health("balances")
            old = {row["名字"]: dict(row) for row in self.b.HEALTH["余额"]}
            for failure in (TimeoutError(), urllib.error.HTTPError("https://fake", 503, "private-key", {}, None),
                            ValueError("private-key")):
                with patch.object(self.b, "_get_json", side_effect=failure):
                    self.b.refresh_health("balances")
                for row in self.b.HEALTH["余额"]:
                    self.assertEqual(row["余额"], old[row["名字"]]["余额"])
                    self.assertEqual(row["更新时间"], old[row["名字"]]["更新时间"])
                    self.assertIn("错误", row)
                    self.assertNotIn("private-key", json.dumps(row))
            # 一个 provider 失败不阻止另一家拿到新值；缺字段不能被误报成零余额。
            with patch.object(self.b, "_get_json", side_effect=[{}, {"data": {"available_balance": 9}}]):
                self.b.refresh_health("balances")
            ds, kimi = self.b.HEALTH["余额"]
            self.assertEqual(ds["余额"], 0)
            self.assertIn("错误", ds)
            self.assertEqual(kimi["余额"], 9)
            self.assertNotIn("错误", kimi)
            self.assertGreater(kimi["更新时间"], old["Kimi"]["更新时间"])

    def test_missing_keys_are_visible_without_erasing_balance(self):
        self.b.HEALTH["余额"] = [{"provider": "deepseek", "名字": "DeepSeek", "余额": 20, "更新时间": 123}]
        with patch.object(self.b.keys, "get_key", return_value=None), patch.object(self.b, "_get_json") as http:
            self.b.refresh_health("balances")
        http.assert_not_called()
        ds = self.b.HEALTH["余额"][0]
        self.assertEqual(ds["余额"], 20)
        self.assertEqual(ds["更新时间"], 123)
        self.assertEqual(ds["错误"], "未配置 API key")

    def test_health_interval_is_sixty_seconds_for_both_channels(self):
        # 虚拟单调时钟检查两通道真实调度循环，测试无需等待一分钟。
        for kind in ("codex", "balances", "audit"):
            monitor = self.b.HealthMonitor()
            clock, calls, waits = [0.0], [], []
            class Timer:
                def wait(self, seconds):
                    waits.append(seconds)
                    clock[0] += seconds
                def clear(self):
                    pass
            monitor.events[kind] = Timer()
            def refresh(channel):
                calls.append((channel, clock[0]))
                if len(calls) == 3:
                    monitor.stop_event.set()
            with patch.object(self.b.time, "monotonic", side_effect=lambda: clock[0]), \
                    patch.object(self.b, "refresh_health", side_effect=refresh):
                monitor.query_loop(kind)
            interval = 3600 if kind == "audit" else 60
            self.assertEqual(calls, [(kind, 0), (kind, interval), (kind, interval * 2)])
            self.assertEqual(waits, [0, interval, interval])

    def test_new_receipts_wake_correct_channel_immediately(self):
        # 使用真实目录和真实等待线程；新回执出现后无需等 60 秒，也无需请求网页。
        monitor = self.b.HealthMonitor()
        counts = {"codex": 0, "balances": 0, "audit": 0}
        notified = {kind: threading.Event() for kind in counts}
        def refresh(kind):
            counts[kind] += 1
            notified[kind].set()
        with patch.object(self.b, "CHECK_BALANCE", True), patch.object(self.b, "refresh_health", side_effect=refresh):
            monitor.start()
            try:
                for event in notified.values():
                    self.assertTrue(event.wait(2))
                    event.clear()
                for worker, channel in (("codex", "codex"), ("opencode", "balances")):
                    directory = os.path.join(self.deliver, "CX-20991231-99", worker)
                    os.makedirs(directory, exist_ok=True)
                    path = os.path.join(directory, "回执.json")
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write('{"工人":')
                    monitor.poll_receipts()
                    self.assertFalse(notified[channel].is_set())
                    with open(path, "w", encoding="utf-8") as fh:
                        json.dump({"工人": worker, "状态": "执行完成", "退出码": 0}, fh)
                    monitor.poll_receipts()
                    self.assertTrue(notified[channel].wait(2))
                    notified[channel].clear()
                    monitor.poll_receipts()
                    self.assertEqual(counts[channel], 2)
                self.assertEqual(counts, {"codex": 2, "balances": 2, "audit": 1})
            finally:
                monitor.stop()
                shutil.rmtree(os.path.join(self.deliver, "CX-20991231-99"), ignore_errors=True)

    def test_receipt_during_query_is_not_lost(self):
        monitor = self.b.HealthMonitor()
        calls = []
        def refresh(kind):
            calls.append(kind)
            if len(calls) == 1:
                monitor.events[kind].set()
            else:
                monitor.stop_event.set()
        with patch.object(self.b, "refresh_health", side_effect=refresh):
            thread = threading.Thread(target=monitor.query_loop, args=("codex",), daemon=True)
            thread.start()
            thread.join(timeout=2)
            try:
                self.assertFalse(thread.is_alive())
                self.assertEqual(calls, ["codex", "codex"])
            finally:
                monitor.stop_event.set()
                monitor.events["codex"].set()
                thread.join(timeout=2)

    def test_health_html_has_live_age_and_error_for_retained_values(self):
        html = self.b.read_text(os.path.join(HERE, "index.html"))
        self.assertIn('data-health-updated', html)
        self.assertIn('秒前更新', html)
        self.assertIn('查询失败（', html)
        self.assertIn('healthAge(q)', html)
        self.assertIn('healthError(q)', html)
        self.assertIn('healthAge(b)', html)
        self.assertIn('healthError(b)', html)



if __name__ == "__main__":
    unittest.main(verbosity=2)
