"""主机专用 TCP 生命周期测试；受限工人运行离线套件，本机验收必须运行此文件。"""
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
SCRIPT = Path(__file__).resolve().parents[2] / 'bin/allowlist-proxy.py'

class ProcessTests(unittest.TestCase):
    def test_first_stdout_line_is_port_and_shutdown_reports_count(self):
        # 真正启动 CLI，给它一个本地拒绝请求，再用 TERM 验证可退出且输出可信计数。
        with tempfile.TemporaryDirectory() as tmp:
            child = subprocess.Popen([sys.executable, "-I", str(SCRIPT), "--allow", "api.deepseek.com:443",
                                      "--log", str(Path(tmp) / "拒绝.log"), "--parent-pid", str(os.getpid())],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertTrue(select.select([child.stdout], [], [], 5)[0], "代理启动超时")
                first = child.stdout.readline().strip()
                self.assertTrue(first.isdigit(), first)
                port = int(first)
                self.assertGreater(port, 0)
                self.assertLessEqual(port, 65535)
                with socket.create_connection(("127.0.0.1", port), timeout=2) as conn:
                    conn.sendall(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
                    self.assertIn(b"403 Forbidden", conn.recv(4096))
                child.terminate()
                out, err = child.communicate(timeout=5)
                self.assertEqual(child.returncode, 0, err)
                self.assertEqual(json.loads(out)["拒绝次数"], 1)
                with self.assertRaises(OSError):
                    socket.create_connection(("127.0.0.1", port), timeout=1)
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate()

    def test_killed_parent_does_not_leave_proxy(self):
        # 用中间父进程启动代理，再强杀父进程；只能依靠代理自身的父进程检查退出。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = """
import os, subprocess, sys, time
with open(sys.argv[2], 'w') as output:
    child = subprocess.Popen([sys.executable, '-I', sys.argv[1], '--allow', 'api.deepseek.com:443',
                              '--log', sys.argv[3]], stdout=output)
    print(child.pid, flush=True)
    time.sleep(60)
"""
            parent = subprocess.Popen([sys.executable, "-I", "-c", source, str(SCRIPT),
                                       str(root / "stdout"), str(root / "log")], stdout=subprocess.PIPE, text=True)
            proxy_pid = None
            try:
                self.assertTrue(select.select([parent.stdout], [], [], 5)[0])
                proxy_pid = int(parent.stdout.readline())
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if (root / "stdout").exists() and (root / "stdout").read_text().strip():
                        break
                    time.sleep(0.02)
                self.assertTrue((root / "stdout").read_text().splitlines()[0].isdigit())
                parent.kill()
                parent.wait(timeout=3)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if len((root / "stdout").read_text().splitlines()) == 2:
                        break
                    time.sleep(0.05)
                lines = (root / "stdout").read_text().splitlines()
                self.assertEqual(json.loads(lines[1]), {"拒绝次数": 0})
                with self.assertRaises(OSError):
                    socket.create_connection(("127.0.0.1", int(lines[0])), timeout=1)
            finally:
                if parent.poll() is None:
                    parent.kill()
                parent.communicate()
                if proxy_pid:
                    try:
                        os.kill(proxy_pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass



if __name__ == '__main__':
    unittest.main()
