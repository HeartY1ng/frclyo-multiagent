#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线测试用本机 socketpair 假目标；本机实测另开 TCP 测试，生产代理没有测试后门。"""

import importlib.util
import errno
import io
import json
import os
from pathlib import Path
import select
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock


# 按绝对文件路径导入代理模块，运行时不依赖 PYTHONPATH 或第三方包。
SCRIPT = Path(__file__).resolve().parents[1] / "allowlist-proxy.py"
spec = importlib.util.spec_from_file_location("allowlist_proxy", SCRIPT)
proxy_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy_module)


class Echo(socketserver.BaseRequestHandler):
    # 用字节回显代替真实 TLS 服务；验证 CONNECT 之后的双向字节流和半关闭。
    def handle(self):
        while True:
            block = self.request.recv(65536)
            if not block:
                try:
                    self.request.sendall(b"<EOF>")
                except BrokenPipeError:
                    pass  # 测试客户端主动关闭而非半关闭时，无需再回传结束哨兵。
                return
            self.request.sendall(block)


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "代理拒绝.log"
        # 执行环境连 TCP bind 都禁止；只替换监听层，用本机 socketpair 驱动真实处理器。
        # 这样仍实际测试分包、双向流和半关闭，不把 HTTP 判定本身模拟掉。
        with mock.patch.object(proxy_module.Proxy, "server_bind"), mock.patch.object(proxy_module.Proxy, "server_activate"):
            self.proxy = proxy_module.Proxy(["api.deepseek.com:443"], self.log)
        self.threads = []
        # 仅在测试进程里替换拨号函数，白名单解析/HTTP 处理/数据转发均运行生产代码。
        self.connect = mock.patch.object(proxy_module, "direct_connect", side_effect=self.fake_connect)
        self.dial = self.connect.start()

    def fake_connect(self, host, port):
        self.assertEqual((host, port), ("api.deepseek.com", 443))
        client, target = socket.socketpair()
        def echo():
            with target:
                Echo(target, ("local", 0), None)
        thread = threading.Thread(target=echo, daemon=True)
        thread.start()
        self.threads.append(thread)
        return client

    def tearDown(self):
        self.proxy.close_all()
        for thread in self.threads:
            thread.join(2)
        self.connect.stop()
        self.proxy.log.close()
        self.tmp.cleanup()

    def connection(self):
        conn, handler = socket.socketpair()
        conn.settimeout(2)
        # 直接调用真实 Handler；收尾关闭服务端 FD，客户端才能观测 EOF。
        def serve():
            with handler:
                self.proxy.finish_request(handler, ("local", 0))
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.threads.append(thread)
        return conn

    def request(self, raw):
        conn = self.connection()
        conn.sendall(raw)
        data = b""
        while b"\r\n\r\n" not in data:
            block = conn.recv(4096)
            if not block:
                break
            data += block
        return conn, data

    def reject(self, raw):
        conn, reply = self.request(raw)
        with conn:
            self.assertTrue(reply.startswith(b"HTTP/1.1 403 "), reply)
        self.dial.assert_not_called()

    def test_allowed_host_tunnel_and_half_close(self):
        conn, reply = self.request(b"CONNECT api.deepseek.com:443 HTTP/1.1\r\nHost: api.deepseek.com:443\r\n\r\n")
        with conn:
            self.assertIn(b"200 Connection Established", reply)
            payload = bytes(range(256)) * 100
            # macOS socketpair 缓冲可能小于负载，收发同时进行，避免测试自身互等。
            sent = []
            def send_payload():
                try:
                    conn.sendall(payload)
                    conn.shutdown(socket.SHUT_WR)
                except OSError as exc:
                    sent.append(exc)
            sender = threading.Thread(target=send_payload, daemon=True)
            sender.start()
            self.threads.append(sender)
            data = b""
            while True:
                block = conn.recv(65536)
                if not block:
                    break
                data += block
            self.assertEqual(data, payload + b"<EOF>")
            sender.join(2)
            self.assertFalse(sent)
        self.dial.assert_called_once_with("api.deepseek.com", 443)

    def test_pipelined_bytes_are_not_lost(self):
        conn, reply = self.request(b"CONNECT API.DEEPSEEK.COM:443 HTTP/1.0\r\n\r\nTLS-sentinel")
        with conn:
            data = reply.split(b"\r\n\r\n", 1)[1]
            while len(data) < len(b"TLS-sentinel"):
                data += conn.recv(1024)
            self.assertEqual(data, b"TLS-sentinel")

    def test_other_host_is_403_and_logged(self):
        self.reject(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        records = [json.loads(x) for x in self.log.read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["目标"], "example.com:443")
        self.assertIn("时间", records[0])
        self.assertEqual(self.proxy.denials, 1)

    def test_non_connect_methods_are_403(self):
        for method in ("GET", "POST", "HEAD", "OPTIONS", "connect"):
            with self.subTest(method=method):
                self.reject((method + " https://api.deepseek.com/ HTTP/1.1\r\n\r\n").encode())
        self.assertEqual(self.proxy.denials, 5)

    def test_authority_confusion_and_other_providers_are_403(self):
        for target in ("api.deepseek.com:80", "api.deepseek.com:0443", "api.deepseek.com.:443",
                       "api.deepseek.com.evil.com:443", "user@api.deepseek.com:443",
                       "https://api.deepseek.com:443", "api.deepseek.com:443/path",
                       "127.0.0.1:443", "[::1]:443", "api.moonshot.cn:443", "api.xiaomimimo.com:443"):
            with self.subTest(target=target):
                self.reject(("CONNECT " + target + " HTTP/1.1\r\n\r\n").encode())

    def test_malformed_headers_and_body_are_403(self):
        for header in (b"broken", b" Host: api.deepseek.com", b"Content-Length: 1",
                       b"Transfer-Encoding: chunked", b"Host : ignored", b"Host: bad\x00value"):
            with self.subTest(header=header):
                self.reject(b"CONNECT api.deepseek.com:443 HTTP/1.1\r\n" + header + b"\r\n\r\n")

    def test_oversized_and_incomplete_headers_are_403(self):
        self.reject(b"X" * proxy_module.MAX_HEADER)
        conn = self.connection()
        with conn:
            conn.sendall(b"CONNECT example.com:443")
            conn.shutdown(socket.SHUT_WR)
            self.assertIn(b"403 Forbidden", conn.recv(4096))
        self.dial.assert_not_called()

    def test_log_injection_stays_one_record(self):
        self.reject(b"CONNECT bad\nname:443 HTTP/1.1\r\n\r\n")
        self.assertEqual(len(self.log.read_text().splitlines()), 1)
        self.assertEqual(json.loads(self.log.read_text())["目标"], "bad\nname:443")

    def test_failed_upstream_is_502(self):
        self.dial.side_effect = OSError("fake offline failure")
        conn, reply = self.request(b"CONNECT api.deepseek.com:443 HTTP/1.1\r\n\r\n")
        with conn:
            self.assertIn(b"502 Bad Gateway", reply)
        self.assertEqual(self.proxy.denials, 0)


class DialTests(unittest.TestCase):
    def test_first_stdout_line_is_listener_port(self):
        # 无监听权限时仍覆盖 CLI 输出协议；端口来自 listener，第一行前不能打印其他信息。
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sys, "argv", [str(SCRIPT),
                "--allow", "api.deepseek.com:443", "--log", str(Path(tmp) / "log")]), \
                mock.patch.object(proxy_module.Proxy, "server_bind", lambda server: setattr(server, "server_address", ("127.0.0.1", 43210))), \
                mock.patch.object(proxy_module.Proxy, "server_activate"), \
                mock.patch.object(proxy_module.Proxy, "handle_request", lambda server: server.stopping.set()), \
                mock.patch.object(proxy_module.signal, "signal"), mock.patch.object(sys, "stdout", new_callable=io.StringIO) as output:
            proxy_module.main()
            lines = output.getvalue().splitlines()
            self.assertEqual(lines[0], "43210")
            self.assertEqual(json.loads(lines[1]), {"拒绝次数": 0})

    def test_private_dns_results_never_dial(self):
        # 不发真实 DNS；保护代理不能被 DNS 意外解析结果引到本机或内网。
        for ip in ("127.0.0.1", "10.0.0.1", "169.254.1.1", "::1", "::ffff:127.0.0.1"):
            with self.subTest(ip=ip), mock.patch.object(proxy_module.socket, "getaddrinfo", return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]), mock.patch.object(proxy_module.socket, "socket") as dial:
                with self.assertRaises(OSError):
                    proxy_module.direct_connect("api.deepseek.com", 443)
                dial.assert_not_called()

    def test_direct_dial_ignores_system_proxy_and_uses_checked_ip(self):
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:12345"}), \
                mock.patch.object(proxy_module.socket, "getaddrinfo", return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]) as dns, \
                mock.patch.object(proxy_module.socket, "socket") as factory:
            conn = proxy_module.direct_connect("api.deepseek.com", 443)
            dns.assert_called_once_with("api.deepseek.com", 443, type=socket.SOCK_STREAM)
            conn.connect.assert_called_once_with(("8.8.8.8", 443))


# 直接提取交付 shell 中实际执行的 Python，避免测试副本正确、生产监督器仍旧出错。
WORKER_SOURCE = (Path(__file__).resolve().parents[1] / "opencode-worker.sh").read_text(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from frclyo import worker
SUPERVISOR_SOURCE = WORKER_SOURCE.split("exec python3 -I -c '\n", 1)[1].split("\n' \"$BIN_DIR\"", 1)[0]


class SupervisorTests(unittest.TestCase):
    def supervise(self, mode="timeout", denied=(), persistent=False, survivor=False,
                  proxy_denied=False, ps_fails=False, late_term=False, normal_rc=0):
        # 虚拟时钟和进程只替代系统边界；执行完整生产监督器（含信号、升级、wait、退出码）。
        now = [0.0]
        parent, proxy_pid, pgid = 101, 102, 103
        state = {"rc": normal_rc if mode == "normal" else None, "reaped": False,
                 "alive": mode != "normal" or survivor, "kills": [], "polls": 0, "ps": 0}
        handlers = {}
        child = mock.Mock(pid=pgid)

        def poll():
            state["polls"] += 1
            if state["rc"] is not None:
                state["reaped"] = True
            return state["rc"]

        def wait(timeout):
            rc = poll()
            if rc is None:
                raise subprocess.TimeoutExpired("fake-worker", timeout)
            return rc

        child.poll.side_effect = poll
        child.wait.side_effect = wait
        rejected = set()

        def killpg(pid, sig):
            self.assertEqual(pid, pgid)
            state["kills"].append((sig, state["polls"]))
            if sig in denied and (persistent or sig not in rejected):
                rejected.add(sig)
                # 单次 EPERM 也可能是暂时权限拒绝，工人仍活着，必须继续发信号。
                raise PermissionError(errno.EPERM, "mock killpg")
            if not state["alive"]:
                if not state["reaped"]:
                    raise PermissionError(errno.EPERM, "zombie")
                raise ProcessLookupError(errno.ESRCH, "gone")
            if sig == signal.SIGKILL or (sig == signal.SIGTERM and not survivor):
                state["alive"] = False
                if state["rc"] is None:
                    state["rc"] = -sig
            if late_term and sig == signal.SIGTERM:
                handlers[signal.SIGTERM](signal.SIGTERM, None)

        def kill(pid, sig):
            self.assertEqual((pid, sig), (proxy_pid, 0))
            if proxy_denied:
                raise PermissionError(errno.EPERM, "mock proxy probe")

        def snapshot(*args, **kwargs):
            state["ps"] += 1
            if ps_fails:
                raise PermissionError(errno.EPERM, "mock ps")
            return types.SimpleNamespace(stdout="%s %s\n999 S\n" % (pgid, "S" if state["alive"] else "Z"))

        def sleep(seconds):
            now[0] += seconds
            if mode == "term" and not state["kills"]:
                handlers[signal.SIGTERM](signal.SIGTERM, None)
            self.assertLess(now[0], 6, "清理不能无限等待")

        modules = {
            "os": types.SimpleNamespace(getppid=lambda: 1 if mode == "orphan" and now[0] else parent,
                                        kill=kill, killpg=killpg),
            "signal": types.SimpleNamespace(SIGTERM=signal.SIGTERM, SIGINT=signal.SIGINT,
                                             SIGHUP=signal.SIGHUP, SIGKILL=signal.SIGKILL,
                                             signal=lambda sig, fn: handlers.__setitem__(sig, fn)),
            "subprocess": types.SimpleNamespace(Popen=mock.Mock(return_value=child), run=snapshot,
                                                 SubprocessError=subprocess.SubprocessError,
                                                 TimeoutExpired=subprocess.TimeoutExpired),
            "time": types.SimpleNamespace(monotonic=lambda: now[0], sleep=sleep),
        }
        argv = ["supervisor", str(Path(__file__).resolve().parents[1]), "deepseek", "1", str(parent), str(proxy_pid), "task", "fake-worker"]
        with mock.patch.object(worker, "inject_key"), mock.patch.dict(sys.modules, modules), mock.patch.object(sys, "argv", argv), \
                mock.patch("builtins.open", mock.mock_open(read_data=b"task")), \
                mock.patch.object(sys, "stderr", new_callable=io.StringIO) as errors:
            with self.assertRaises(SystemExit) as caught:
                exec(compile(SUPERVISOR_SOURCE, "worker-supervisor", "exec"), {})
        child.wait.assert_called_once()
        return caught.exception.code, state, errors.getvalue()

    def test_timeout_eperm_each_group_operation_reaps_and_retries(self):
        for sig in (0, signal.SIGTERM, signal.SIGKILL):
            with self.subTest(sig=sig):
                rc, state, errors = self.supervise(denied=(sig,), survivor=True)
                self.assertEqual(rc, 142)
                self.assertFalse(state["alive"])
                self.assertTrue(state["reaped"])
                calls = [polls for signal_sent, polls in state["kills"] if signal_sent == sig]
                self.assertGreaterEqual(len(calls), 2)
                self.assertGreater(calls[1], calls[0], "EPERM 后必须先 poll 再重试")
                self.assertEqual(errors, "")

    def test_eperm_with_reaped_leader_still_kills_live_descendant(self):
        rc, state, errors = self.supervise(mode="normal", denied=(0,), persistent=True, survivor=True)
        self.assertEqual(rc, 0)
        self.assertGreater(state["ps"], 0)
        self.assertIn(signal.SIGKILL, [sig for sig, _ in state["kills"]])
        self.assertFalse(state["alive"])
        self.assertEqual(errors, "")

    def test_persistent_eperm_zombie_group_is_verified(self):
        rc, state, errors = self.supervise(mode="normal", denied=(0, signal.SIGTERM), persistent=True)
        self.assertEqual(rc, 0)
        self.assertGreater(state["ps"], 0)
        self.assertTrue(state["reaped"])
        self.assertEqual(errors, "")

    def test_persistent_eperm_live_group_reports_failure_and_preserves_timeout(self):
        for mode, expected in (("timeout", 142), ("normal", 125)):
            with self.subTest(mode=mode):
                rc, state, errors = self.supervise(mode=mode, denied=(0, signal.SIGTERM, signal.SIGKILL),
                                                   persistent=True, survivor=True)
                self.assertEqual(rc, expected)
                self.assertTrue(state["alive"])
                self.assertIn(signal.SIGKILL, [sig for sig, _ in state["kills"]])
                self.assertIn("无法确认进程组已退出", errors)

    def test_failed_state_query_never_counts_as_group_exit(self):
        rc, state, errors = self.supervise(mode="normal", denied=(0,), persistent=True, ps_fails=True)
        self.assertEqual(rc, 125)
        self.assertIn(signal.SIGKILL, [sig for sig, _ in state["kills"]])
        self.assertIn("无法确认进程组已退出", errors)

    def test_proxy_kill_eperm_stops_worker(self):
        rc, state, errors = self.supervise(proxy_denied=True)
        self.assertEqual(rc, 125)
        self.assertTrue(state["reaped"])
        self.assertFalse(state["alive"])
        self.assertEqual(errors, "")

    def test_normal_term_and_killed_shell_cleanup(self):
        for mode, expected in (("normal", 0), ("term", 143), ("orphan", 143)):
            with self.subTest(mode=mode):
                rc, state, errors = self.supervise(mode=mode, survivor=True)
                self.assertEqual(rc, expected)
                self.assertFalse(state["alive"])
                self.assertTrue(state["reaped"])
                self.assertEqual(errors, "")

    def test_term_during_timeout_cleanup_cannot_replace_142(self):
        rc, state, errors = self.supervise(late_term=True)
        self.assertEqual(rc, 142)
        self.assertFalse(state["alive"])
        self.assertEqual(errors, "")

    def test_worker_exit_code_and_signal_are_preserved(self):
        for worker_rc, expected in ((7, 7), (-signal.SIGKILL, 137)):
            with self.subTest(worker_rc=worker_rc):
                rc, state, errors = self.supervise(mode="normal", normal_rc=worker_rc)
                self.assertEqual(rc, expected)
                self.assertTrue(state["reaped"])
                self.assertEqual(errors, "")

    def test_timeout_eperm_receipt_keeps_status_and_network(self):
        rc, _, _ = self.supervise(denied=(0, signal.SIGTERM, signal.SIGKILL), survivor=True)
        # 执行生产回执生成代码，断言超时码确实映射到“超时”，网络字段保持原格式。
        receipt_source = WORKER_SOURCE.split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            stats = Path(tmp) / "proxy.stdout"
            stats.write_text('12345\n{"拒绝次数": 0}\n', encoding="utf-8")
            argv = ["receipt", tmp, "TEST", "timeout", str(rc), "start", "end",
                    "deepseek/deepseek-flash", "nothink", "api.deepseek.com:443", str(stats)]
            result = subprocess.run([sys.executable, "-I", "-c", receipt_source] + argv[1:],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads((Path(tmp) / "回执.json").read_text())
            self.assertEqual(receipt["退出码"], 142)
            self.assertEqual(receipt["状态"], "超时")
            self.assertEqual(receipt["网络"], {"放行": ["api.deepseek.com:443"], "拒绝次数": 0})


if __name__ == "__main__":
    unittest.main()
