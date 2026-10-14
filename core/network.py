"""
网络工具

- get_local_ip()  : 获取本机在局域网中的出口 IP
- find_free_port(): 从首选端口开始寻找可用端口
- scan_network()  : 并发扫描局域网内存活的 LAN Remote 服务
"""

from __future__ import annotations

import concurrent.futures
import logging
import socket

import config

logger = logging.getLogger("LAN_REMOTE")

DEFAULT_PROBE_HOST = "8.8.8.8"
PROBE_PORT = 80


def get_local_ip() -> str:
    """
    获取本机局域网 IP。

    通过 UDP connect 探测出口网卡，不会真的发包。失败时回退到 127.0.0.1。
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1.0)
        sock.connect((DEFAULT_PROBE_HOST, PROBE_PORT))
        return sock.getsockname()[0]
    except OSError as exc:
        logger.warning("获取本机 IP 失败，回退到 127.0.0.1：%s", exc)
        return "127.0.0.1"
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def is_port_free(port: int, host: str = config.SERVER_HOST) -> bool:
    """检测端口是否可绑定。"""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        sock.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def find_free_port(start: int = None, attempts: int = None) -> int | None:
    """从 start 开始向后寻找可用端口；全部占用返回 None。"""
    start = int(start or config.server_port())
    attempts = config.server_port_fallback() if attempts is None else int(attempts)
    for offset in range(0, max(1, attempts) + 1):
        port = start + offset
        if port > 65535:
            break
        if is_port_free(port):
            return port
    return None


def check_host(ip: str, port: int = None, timeout: float = 0.3) -> str | None:
    """检测单个主机端口是否开放，开放返回 ip。"""
    port = int(port or config.server_port())
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        if sock.connect_ex((ip, port)) == 0:
            return ip
    except OSError:
        pass
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    return None


def scan_network(prefix: str, port: int = None, workers: int = 64, timeout: float = 0.3) -> list[str]:
    """
    并发扫描 prefix.1 ~ prefix.254。

    prefix 形如 "192.168.1"。
    """
    if not prefix or prefix.count(".") != 2:
        raise ValueError("prefix 必须形如 '192.168.1'")

    ips = [f"{prefix}.{i}" for i in range(1, 255)]
    found: list[str] = []

    def _probe(ip: str) -> str | None:
        return check_host(ip, port, timeout)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(256, workers))) as pool:
        for result in pool.map(_probe, ips):
            if result:
                found.append(result)

    return sorted(found, key=lambda value: int(value.rsplit(".", 1)[-1]))
