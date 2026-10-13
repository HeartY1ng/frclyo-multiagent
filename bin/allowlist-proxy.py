#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每轮独立的 HTTPS CONNECT 代理；仅用 Python 3.9 标准库，不读取系统代理。"""

import argparse
import datetime
import ipaddress
import json
import os
import re
import select
import signal
import socket
import socketserver
import threading
import time


# 只接受明确的 DNS 主机名和 443；不接受 URL、账号、IP、尾点或宽泛的后缀匹配。
AUTHORITY = re.compile(r"(?=.{1,253}:443\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}:443\Z", re.I)
MAX_HEADER = 16384


def authority(value):
    if not AUTHORITY.fullmatch(value):
        raise ValueError("目标必须为完整 DNS 主机名:443")
    return value.lower()


def direct_connect(host, port):
    """直接解析和拨号，不使用 urllib，所以不会绕到系统代理。"""
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    # DNS 即使意外返回内网地址也不能放行；连接用已校验的数字地址，避免第二次解析。
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise OSError("官方 API 解析到了非公网地址")
    last_error = OSError("官方 API 没有可连接地址")
    for family, kind, proto, _, endpoint in addresses:
        conn = socket.socket(family, kind, proto)
        try:
            conn.settimeout(10)
            conn.connect(endpoint)
            return conn
        except OSError as exc:
            last_error = exc
            conn.close()
    raise last_error


class Proxy(socketserver.ThreadingTCPServer):
    # 线程数设上限，连接必须定期有流量；关闭时集中切断所有连接，不遗留隧道。
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False

    def __init__(self, allowed, log_path):
        self.allowed = frozenset(authority(x) for x in allowed)
        if not self.allowed:
            raise ValueError("白名单不能为空")
        self.lock = threading.Lock()
        self.connections = set()
        self.slots = threading.BoundedSemaphore(64)
        self.denials = 0
        self.stopping = threading.Event()
        # 先打开日志再宣布就绪。若日志无法写入，整个代理不得启动。
        self.log = open(log_path, "a", encoding="utf-8", buffering=1)
        try:
            super().__init__(("127.0.0.1", 0), Handler)
        except BaseException:
            self.log.close()
            raise

    def reject(self, target):
        # JSONL 自动转义换行和控制符，恶意目标不能伪造第二条日志。
        with self.lock:
            if self.stopping.is_set():
                return
            self.log.write(json.dumps({"时间": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                       "目标": target}, ensure_ascii=True) + "\n")
            self.denials += 1

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                self.reject("<并发连接超限>")
                request.settimeout(1)
                request.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        with self.lock:
            self.connections.add(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.release(request)
            raise

    def release(self, request):
        with self.lock:
            self.connections.discard(request)
        self.slots.release()

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.release(request)

    def close_all(self):
        self.stopping.set()
        self.server_close()
        with self.lock:
            for conn in self.connections:
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                conn.close()
            self.log.close()
        # 停止后的拒绝处理不会再写日志；计数到这里已冻结，可安全交给回执。


class Handler(socketserver.BaseRequestHandler):
    def reply(self, status):
        self.request.sendall(("HTTP/1.1 " + status +
                              "\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").encode("ascii"))

    def handle(self):
        target = "<无完整请求>"
        try:
            # 逐块收请求头，限大小、限总耗时；不把缓冲区吞掉的 TLS 数据丢掉。
            data = b""
            deadline = time.monotonic() + 10
            while b"\r\n\r\n" not in data:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or len(data) >= MAX_HEADER:
                    raise ValueError("请求头过长或超时")
                self.request.settimeout(remaining)
                chunk = self.request.recv(min(4096, MAX_HEADER - len(data)))
                if not chunk:
                    raise ValueError("请求头不完整")
                data += chunk
            header, pending = data.split(b"\r\n\r\n", 1)
            lines = header.decode("ascii").split("\r\n")
            parts = lines[0].split(" ")
            target = parts[1] if len(parts) > 1 else lines[0]
            if len(parts) != 3 or parts[0] != "CONNECT" or parts[2] not in ("HTTP/1.0", "HTTP/1.1"):
                raise ValueError("仅支持 CONNECT")
            dest = authority(target)
            if dest not in self.server.allowed:
                raise ValueError("目标不在本轮白名单")
            for line in lines[1:]:
                # CONNECT 不能附带 HTTP 请求体，也不支持折叠头部或转发目标头。
                if not line or line[0].isspace() or ":" not in line:
                    raise ValueError("请求头格式错误")
                name, value = line.split(":", 1)
                if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                    raise ValueError("请求头名称错误")
                if any(ord(c) < 32 and c != "\t" for c in value):
                    raise ValueError("请求头含控制符")
                if name.lower() == "transfer-encoding" or (name.lower() == "content-length" and value.strip() != "0"):
                    raise ValueError("CONNECT 不能携带请求体")
        except (ValueError, OSError):
            self.server.reject(target)
            try:
                self.reply("403 Forbidden")
            except OSError:
                pass
            return

        # 只有验证完成才在沙箱外拨号；拨号失败是 502，不计入策略拒绝次数。
        try:
            upstream = direct_connect(dest[:-4], 443)
        except OSError:
            try:
                self.reply("502 Bad Gateway")
            except OSError:
                pass
            return
        with upstream:
            with self.server.lock:
                self.server.connections.add(upstream)
            try:
                self.request.settimeout(10)
                upstream.settimeout(10)
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if pending:
                    upstream.sendall(pending)
                # HTTPS 内容保持端到端加密；这里只转发字节，不截取或记录 key。
                readers = [self.request, upstream]
                while readers and not self.server.stopping.is_set():
                    ready, _, _ = select.select(readers, [], [], 120)
                    if not ready:
                        break
                    for source in ready:
                        sink = upstream if source is self.request else self.request
                        block = source.recv(65536)
                        if block:
                            sink.sendall(block)
                        else:
                            readers.remove(source)
                            sink.shutdown(socket.SHUT_WR)
            except (OSError, ValueError):
                pass
            finally:
                with self.server.lock:
                    self.server.connections.discard(upstream)


def main():
    # 第一行 stdout 只写随机端口；正常退出时第二行给出可信的内存计数，供回执读取。
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow", action="append", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--parent-pid", type=int, default=os.getppid())
    args = parser.parse_args()
    if args.parent_pid <= 1 or os.getppid() != args.parent_pid:
        parser.error("启动父进程已经退出")
    try:
        proxy = Proxy(args.allow, args.log)
    except (ValueError, OSError) as exc:
        parser.exit(2, "代理启动失败：%s\n" % exc)

    # trap 处理不了 SIGKILL；每 0.2 秒检查是否被重新托管，父进程消失就自行退出。
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: proxy.stopping.set())
    proxy.timeout = 0.2
    print(proxy.server_address[1], flush=True)
    try:
        while not proxy.stopping.is_set() and os.getppid() == args.parent_pid:
            proxy.handle_request()
    finally:
        proxy.close_all()
        print(json.dumps({"拒绝次数": proxy.denials}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
