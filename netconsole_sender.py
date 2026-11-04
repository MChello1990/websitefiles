#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""netconsole 发送端（Ubuntu 24.04 / Python 3.12，无第三方依赖）。

把本机的日志主动"推"到接收端（Windows 那个 netconsole_receiver.py）：
默认走 TCP，由本机主动 connect 接收端；也可以用 --udp 走无连接的 UDP。

数据源（四选一，默认标准输入）：
  --journal [单元...]   journalctl -f -o short-iso，可指定 systemd 单元
  -f, --file PATH       跟踪文件（tail -F 语义，能处理轮转和截断）
  -c, --command CMD     执行命令，把 stdout+stderr 逐行转发
  --dmesg               跟踪内核日志（dmesg -w，需要 root）
  不给任何源时读 stdin：  journalctl -f | netconsole_sender.py --host 1.2.3.4

特性：
  * 断线自动重连（指数退避 1→30 秒），断线期间缓存最近的日志行，连上后补发
  * 行级转发，原样透传字节（UTF-8/GBK/二进制都不会崩），自动补换行
  * --level / --tag 可给每行加 <N> syslog 前缀和来源标签
  * --dry-run 只打印不发送，用来确认数据源选对了
  * 收到 SIGTERM / Ctrl+C 时优雅退出，尽量把缓存发完

示例：
  python3 netconsole_sender.py --host 192.168.50.164 --journal
  python3 netconsole_sender.py --host 192.168.50.164 -f /var/log/syslog
  python3 netconsole_sender.py --host 192.168.50.164 -c "ping -c 100 8.8.8.8"
  journalctl -f | python3 netconsole_sender.py --host 192.168.50.164
"""

from __future__ import annotations

import argparse
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Deque, Iterator, List, Optional

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 52221
UDP_CHUNK = 65000  # 单个 UDP 数据报最多 65507 字节

_EMPTY = object()   # 队列暂时没数据
_EOF = object()     # 数据源结束


def _short(exc: BaseException) -> str:
    text = str(exc)
    return text if text else exc.__class__.__name__


# --------------------------------------------------------------------------- #
# 数据源
# --------------------------------------------------------------------------- #
def follow_file(path: str, from_start: bool) -> Iterator[bytes]:
    """tail -F：文件被轮转/截断/替换后自动重新打开。"""
    fh = None
    stat = None
    buffer = b""
    warned = False

    while True:
        if fh is None:
            try:
                fh = open(path, "rb")
            except OSError:
                if not warned:
                    print("[!] 等待文件出现：%s" % path, file=sys.stderr, flush=True)
                    warned = True
                time.sleep(1.0)
                continue
            warned = False
            if not from_start:
                fh.seek(0, os.SEEK_END)
                from_start = True  # 只有第一次生效
            stat = os.fstat(fh.fileno())
            buffer = b""

        chunk = fh.read(65536)
        if chunk:
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                yield line.rstrip(b"\r")
            continue

        # 空闲：看看文件是不是被轮转 / 截断 / 删除
        time.sleep(0.3)
        try:
            current = os.stat(path)
        except OSError:
            current = None
        try:
            position = fh.tell()
        except OSError:
            position = 0

        rotated = (current is None or stat is None
                   or (current.st_ino, current.st_dev) != (stat.st_ino, stat.st_dev))
        truncated = current is not None and current.st_size < position
        if rotated or truncated:
            if buffer:
                yield buffer
                buffer = b""
            try:
                fh.close()
            except OSError:
                pass
            fh = None
            if current is None:
                time.sleep(1.0)


def command_lines(command, shell: bool) -> Iterator[bytes]:
    """执行命令，逐行产出 stdout+stderr；命令退出即数据源结束。"""
    proc = subprocess.Popen(
        command,
        shell=shell,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    try:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            yield raw.rstrip(b"\r\n")
    finally:
        try:
            proc.terminate()
        except OSError:
            pass


def journalctl_lines(units: List[str]) -> Iterator[bytes]:
    exe = shutil.which("journalctl")
    if not exe:
        raise SystemExit("[!] 找不到 journalctl：本机似乎没有 systemd")
    command = [exe, "-f", "-o", "short-iso", "--no-pager"]
    for unit in units:
        command += ["-u", unit]
    return command_lines(command, shell=False)


def make_source(args: argparse.Namespace) -> Iterator[bytes]:
    if args.file:
        return follow_file(args.file, args.from_start)
    if args.command:
        return command_lines(args.command, shell=True)
    if args.journal:
        return journalctl_lines(args.units)
    if args.dmesg:
        return command_lines(["dmesg", "-w"], shell=False)
    return _stdin_lines()


def _stdin_lines() -> Iterator[bytes]:
    stdin = sys.stdin.buffer
    for raw in stdin:
        yield raw.rstrip(b"\r\n")


# --------------------------------------------------------------------------- #
# 读线程：把数据源的行丢进队列，主线程负责发送/重连
# --------------------------------------------------------------------------- #
class _Reader(threading.Thread):
    def __init__(self, lines: Iterator[bytes], out: "queue.Queue[object]") -> None:
        super().__init__(name="reader", daemon=True)
        self._lines = lines
        self._out = out

    def run(self) -> None:
        try:
            for line in self._lines:
                self._out.put(line)
        except Exception as exc:  # 数据源出错也要让主循环知道
            print("[!] 读取数据源失败：%s" % _short(exc), file=sys.stderr, flush=True)
        finally:
            self._out.put(_EOF)


# --------------------------------------------------------------------------- #
# 发送端
# --------------------------------------------------------------------------- #
class Sender:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.queue: "queue.Queue[object]" = queue.Queue(maxsize=args.queue_size)
        self.pending: Deque[bytes] = deque(maxlen=args.backlog)
        self.sock: Optional[socket.socket] = None
        self.sent_lines = 0
        self.sent_bytes = 0
        self.dropped = 0
        self.delay = 1.0
        self.stop = False
        self._eof = False
        self._eof_at = 0.0
        self._next_connect = 0.0
        self._next_stats = 0.0

    # ------------------------------------------------------------ 输出
    def _note(self, message: str) -> None:
        if not self.args.quiet:
            print("[*] %s" % message, file=sys.stderr, flush=True)

    def _warn(self, message: str) -> None:
        print("[!] %s" % message, file=sys.stderr, flush=True)

    def _banner(self) -> None:
        proto = "UDP" if self.args.udp else "TCP"
        self._note("netconsole 发送端 -> %s:%d（%s）"
                   % (self.args.host, self.args.port, proto))
        self._note("数据源：%s" % describe_source(self.args))
        self._note("断线缓存 %d 行，重连退避 1-30 秒" % self.args.backlog)

    # ------------------------------------------------------------ 连接
    def _connect(self) -> bool:
        host, port = self.args.host, self.args.port
        try:
            sock = socket.create_connection((host, port), timeout=self.args.timeout)
        except OSError as exc:
            self._warn("连接 %s:%d 失败：%s" % (host, port, _short(exc)))
            return False
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if hasattr(socket, "TCP_USER_TIMEOUT"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, 15000)
        except OSError:
            pass
        sock.settimeout(10.0)
        self.sock = sock
        self._note("已连接 %s:%d，开始发送" % (host, port))
        return True

    def _close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    # ------------------------------------------------------------ 发送
    def _transform(self, line: bytes) -> bytes:
        prefix = b""
        if self.args.level is not None:
            prefix += ("<%d>" % self.args.level).encode("ascii")
        if self.args.tag:
            prefix += ("[%s] " % self.args.tag).encode("utf-8", "replace")
        return prefix + line + b"\n"

    def _enqueue(self, line: bytes) -> None:
        payload = self._transform(line)
        if self.args.dry_run:
            try:
                sys.stdout.write(payload.decode("utf-8", "replace"))
                sys.stdout.flush()
            except OSError:
                pass
            self.sent_lines += 1
            self.sent_bytes += len(payload)
            return
        if self.pending.maxlen is not None and len(self.pending) >= self.pending.maxlen:
            self.dropped += 1
        self.pending.append(payload)

    def _flush(self) -> None:
        sent = 0
        while self.pending and self.sock is not None:
            payload = self.pending[0]
            try:
                self.sock.sendall(payload)
            except OSError as exc:
                self._warn("发送失败：%s（稍后重连重发）" % _short(exc))
                self._close()
                # 连接被对端立刻断开时也要退避，避免疯狂重连
                self._next_connect = time.time() + self.delay
                self.delay = min(self.delay * 2.0, 30.0)
                return
            self.pending.popleft()
            self.sent_lines += 1
            self.sent_bytes += len(payload)
            sent += 1
        if sent:
            self.delay = 1.0

    def _send_udp(self, payload: bytes) -> bool:
        if self.sock is None:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._note("UDP 发送到 %s:%d" % (self.args.host, self.args.port))
        try:
            if len(payload) <= UDP_CHUNK:
                self.sock.sendto(payload, (self.args.host, self.args.port))
            else:
                for offset in range(0, len(payload), UDP_CHUNK):
                    self.sock.sendto(payload[offset:offset + UDP_CHUNK],
                                     (self.args.host, self.args.port))
        except OSError as exc:
            self._warn("UDP 发送失败：%s" % _short(exc))
            self._close()
            return False
        self.sent_lines += 1
        self.sent_bytes += len(payload)
        return True

    # ------------------------------------------------------------ 主循环
    def _pump(self) -> None:
        now = time.time()
        if self.args.udp:
            while self.pending:
                if not self._send_udp(self.pending.popleft()):
                    break
            return

        if self.sock is None:
            if not self.pending or now < self._next_connect:
                return
            if not self._connect():
                self._next_connect = now + self.delay
                self.delay = min(self.delay * 2.0, 30.0)
                return
            self._next_connect = 0.0

        self._flush()

    def _stats(self) -> None:
        if self.args.stats <= 0:
            return
        now = time.time()
        if now < self._next_stats:
            return
        self._next_stats = now + self.args.stats
        self._note("已发送 %d 行 / %.1f KB，待发 %d 行，丢弃 %d 行"
                   % (self.sent_lines, self.sent_bytes / 1024.0,
                      len(self.pending), self.dropped))

    def run(self) -> int:
        self._banner()
        reader = _Reader(make_source(self.args), self.queue)
        reader.start()
        self._next_stats = time.time() + self.args.stats if self.args.stats > 0 else float("inf")

        try:
            while True:
                try:
                    item = self.queue.get(timeout=0.2)
                except queue.Empty:
                    item = _EMPTY

                if item is _EOF:
                    self._eof = True
                    self._eof_at = time.time()
                elif item is not _EMPTY:
                    self._enqueue(item)  # type: ignore[arg-type]

                if self._eof and not self.pending and self.queue.empty():
                    break
                if self.stop:
                    break
                if (self._eof and self.pending and self.args.drain_timeout > 0
                        and time.time() - self._eof_at > self.args.drain_timeout):
                    self._warn("数据源已结束但还有 %d 行没发出去，放弃"
                               % len(self.pending))
                    break

                self._pump()
                self._stats()
        except KeyboardInterrupt:
            self._note("收到 Ctrl+C，正在退出")
        finally:
            self.stop = True
            if self.pending and not self.args.dry_run:
                self._note("尝试发送剩余的 %d 行 ..." % len(self.pending))
                self._pump()
            self._close()

        if self.pending:
            self._warn("仍有 %d 行未能发送" % len(self.pending))
        self._note("结束：共发送 %d 行 / %.1f KB，丢弃 %d 行"
                   % (self.sent_lines, self.sent_bytes / 1024.0, self.dropped))
        return 0


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def describe_source(args: argparse.Namespace) -> str:
    if args.file:
        return "跟踪文件 %s" % args.file
    if args.command:
        return "命令：%s" % args.command
    if args.journal:
        return "journalctl -f -o short-iso" + (
            " " + " ".join("--unit %s" % u for u in args.units) if args.units else "（全部日志）")
    if args.dmesg:
        return "dmesg -w（内核日志）"
    return "标准输入"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="netconsole_sender.py",
        description="把本机日志主动发送到 netconsole 接收端（默认 TCP 主动连接）。",
        epilog=(
            "示例：\n"
            "  python3 netconsole_sender.py --host 192.168.50.164 --journal\n"
            "  python3 netconsole_sender.py --host 192.168.50.164 -f /var/log/syslog\n"
            "  python3 netconsole_sender.py --host 192.168.50.164 -c 'ping -c 100 8.8.8.8'\n"
            "  journalctl -f | python3 netconsole_sender.py --host 192.168.50.164\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    net = parser.add_argument_group("接收端")
    net.add_argument("--host", default=DEFAULT_HOST,
                     help="接收端 IP / 主机名（默认 %(default)s）")
    net.add_argument("--port", type=int, default=DEFAULT_PORT,
                     help="接收端端口（默认 %(default)s）")
    net.add_argument("--udp", action="store_true",
                     help="改用 UDP（不建连接，适合模拟内核 netconsole；默认 TCP）")
    net.add_argument("--timeout", type=float, default=5.0,
                     help="连接超时秒数（默认 %(default)s）")

    source = parser.add_argument_group("数据源（四选一，默认标准输入）")
    source.add_argument("-f", "--file", metavar="PATH", help="跟踪该文件（tail -F）")
    source.add_argument("--from-start", action="store_true",
                        help="配合 --file：从文件开头读，而不是从末尾")
    source.add_argument("-c", "--command", metavar="CMD",
                        help="执行命令并转发其输出（用 shell 解释）")
    source.add_argument("--journal", action="store_true",
                        help="跟随 journalctl；后面的位置参数当作 systemd 单元名")
    source.add_argument("--dmesg", action="store_true", help="跟随内核日志 dmesg -w")
    source.add_argument("units", nargs="*", metavar="UNIT",
                        help="配合 --journal：只转发这些 systemd 单元的日志")

    fmt = parser.add_argument_group("内容与可靠性")
    fmt.add_argument("--level", type=int, choices=range(0, 8), metavar="N",
                     help="给每行加 <N> syslog 级别前缀（0=EMERG 7=DEBUG），"
                          "接收端会显示成彩色级别")
    fmt.add_argument("--tag", metavar="NAME", help="给每行加 [NAME] 来源标签")
    fmt.add_argument("--backlog", type=int, default=2000, metavar="N",
                     help="断线时最多缓存多少行（默认 %(default)s）")
    fmt.add_argument("--queue-size", type=int, default=10000, metavar="N",
                     help="内部队列长度（默认 %(default)s，满了会给数据源施加反压）")
    fmt.add_argument("--drain-timeout", type=float, default=120.0, metavar="SEC",
                     help="数据源结束后最多再等多少秒把缓存发完（默认 %(default)s，0=一直等）")
    fmt.add_argument("--stats", type=float, default=60.0, metavar="SEC",
                     help="每隔多少秒打印一次统计（默认 %(default)s，0=关闭）")
    fmt.add_argument("--dry-run", action="store_true", help="不发送，只打印到标准输出")
    fmt.add_argument("--quiet", action="store_true", help="不打印状态提示")
    fmt.add_argument("-V", "--version", action="version", version="netconsole_sender 1.0")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        pass

    args = build_parser().parse_args(argv)
    if not 0 < args.port < 65536:
        print("端口必须在 1-65535 之间", file=sys.stderr)
        return 2

    chosen = [bool(args.file), bool(args.command), bool(args.journal), bool(args.dmesg)]
    if sum(chosen) > 1:
        print("--file / --command / --journal / --dmesg 只能选一个", file=sys.stderr)
        return 2
    if args.units and not args.journal:
        print("位置参数 UNIT 只能配合 --journal 使用", file=sys.stderr)
        return 2
    if args.backlog < 1:
        print("--backlog 至少为 1", file=sys.stderr)
        return 2

    sender = Sender(args)

    def _on_signal(signum, frame):  # systemd stop / kill
        sender.stop = True

    try:
        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGHUP, _on_signal)
    except (AttributeError, ValueError, OSError):
        pass

    return sender.run()


if __name__ == "__main__":
    sys.exit(main())
