"""
局域网设备发现

通过 UDP 广播让局域网内的 LAN Remote 实例互相感知，从而支持「只填设备 ID 就能连」：

    - 每个实例周期性广播 announce（device_id + 业务端口 + 主机名）
    - 收到 query 的实例立即单播回复 announce，实现按需快速发现
    - 本模块维护一张 device_id -> (ip, port) 的存活表，超时未宣告即剔除

线程模型：独立守护线程收发 UDP；查询接口线程安全，可从任意线程调用。
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from dataclasses import dataclass

import config

logger = logging.getLogger("LAN_REMOTE")

APP_TAG = "lan-remote"
KIND_ANNOUNCE = "announce"
KIND_QUERY = "query"

BROADCAST_ADDR = "255.255.255.255"


@dataclass
class DiscoveredDevice:
    """一台在线的被控端。"""

    device_id: str
    ip: str
    port: int
    name: str
    last_seen: float

    def as_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "ip": self.ip,
            "port": self.port,
            "name": self.name,
            "age": round(max(0.0, time.time() - self.last_seen), 1),
        }


class DiscoveryService:
    """UDP 设备发现服务（宣告 + 监听 二合一）。"""

    def __init__(
        self,
        device_id: str,
        local_port: int = None,
        name: str = "",
        discovery_port: int = None,
        interval: float = None,
        ttl: float = None,
    ):
        self.device_id = str(device_id)
        self.local_port = int(local_port or config.server_port())
        self.name = name or socket.gethostname()
        self.discovery_port = int(discovery_port or config.DISCOVERY_PORT)
        self.interval = max(0.5, float(interval or config.DISCOVERY_INTERVAL))
        self.ttl = max(1.0, float(ttl or config.DISCOVERY_TTL))

        self._lock = threading.Lock()
        self._devices: dict[str, DiscoveredDevice] = {}
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error: str | None = None

    # ================================================================== 生命周期
    def start(self) -> bool:
        """启动发现服务；端口被占用等原因失败时返回 False（不影响其它功能）。"""
        if self._thread and self._thread.is_alive():
            return True

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            except OSError:
                logger.debug("当前平台不支持 SO_BROADCAST")
            sock.bind(("", self.discovery_port))
            sock.settimeout(0.25)
        except OSError as exc:
            self._last_error = f"设备发现服务启动失败（UDP {self.discovery_port}）：{exc}"
            logger.warning(self._last_error)
            return False

        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="Discovery", daemon=True)
        self._thread.start()
        logger.info("设备发现服务已启动（UDP %d，宣告端口 %d）", self.discovery_port, self.local_port)
        return True

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if self._thread and self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def last_error(self) -> str | None:
        return self._last_error

    # ================================================================== 配置
    def set_advertised_port(self, port: int) -> None:
        """业务端口变化（如自动回退）后更新对外宣告的端口。"""
        with self._lock:
            self.local_port = int(port)

    # ================================================================== 查询
    def lookup(self, device_id: str) -> tuple[str | None, int | None]:
        """从已知设备表里查找，返回 (ip, port)；找不到返回 (None, None)。"""
        if not device_id:
            return None, None
        with self._lock:
            device = self._devices.get(str(device_id))
            if device is None:
                return None, None
            if time.time() - device.last_seen > self.ttl:
                return None, None
            return device.ip, device.port

    def resolve(self, device_id: str, wait: float = None) -> tuple[str | None, int | None]:
        """查找设备；表里没有就广播一次 query 并等待 reply。"""
        if not device_id:
            return None, None

        ip, port = self.lookup(device_id)
        if ip is not None:
            return ip, port

        wait = float(config.DISCOVERY_RESOLVE_WAIT if wait is None else wait)
        self._send(self._payload(KIND_QUERY), (BROADCAST_ADDR, self.discovery_port))

        deadline = time.time() + max(0.5, wait)
        while time.time() < deadline:
            ip, port = self.lookup(device_id)
            if ip is not None:
                return ip, port
            time.sleep(0.1)
        return None, None

    def known_devices(self) -> list[dict]:
        """当前在线的其它设备列表（不含自己）。"""
        now = time.time()
        with self._lock:
            devices = [d for d in self._devices.values() if now - d.last_seen <= self.ttl]
        return [d.as_dict() for d in sorted(devices, key=lambda d: d.device_id)]

    def known_count(self) -> int:
        return len(self.known_devices())

    # ================================================================== 收发
    def _payload(self, kind: str) -> str:
        return json.dumps(
            {
                "app": APP_TAG,
                "kind": kind,
                "device_id": self.device_id,
                "port": self.local_port,
                "name": self.name,
                "time": time.time(),
            },
            ensure_ascii=False,
        )

    def _send(self, payload: str, addr: tuple[str, int]) -> None:
        sock = self._sock
        if sock is None:
            return
        try:
            sock.sendto(payload.encode("utf-8"), addr)
        except OSError as exc:
            logger.debug("发送发现报文失败（%s）：%s", addr, exc)

    def _run(self) -> None:
        last_announce = 0.0
        try:
            while not self._stop.is_set():
                now = time.time()
                if now - last_announce >= self.interval:
                    self._send(self._payload(KIND_ANNOUNCE), (BROADCAST_ADDR, self.discovery_port))
                    last_announce = now

                self._purge(now)

                try:
                    data, addr = self._sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break

                # 广播会被自己收到，按 device_id 过滤
                self._handle(data, addr)
        finally:
            logger.info("设备发现服务已退出")

    def _handle(self, data, addr) -> None:
        try:
            packet = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(packet, dict) or packet.get("app") != APP_TAG:
            return

        device_id = packet.get("device_id")
        if not device_id or device_id == self.device_id:
            return

        try:
            port = int(packet.get("port") or config.server_port())
        except (TypeError, ValueError):
            return

        name = str(packet.get("name") or "")
        kind = packet.get("kind")

        with self._lock:
            existing = self._devices.get(device_id)
            if existing is None:
                logger.info("发现局域网设备：%s @ %s:%d（%s）", device_id, addr[0], port, name)
            self._devices[device_id] = DiscoveredDevice(
                device_id=str(device_id), ip=addr[0], port=port, name=name, last_seen=time.time()
            )

        # 对方在找设备，立即单独回一份宣告，加快发现速度
        if kind == KIND_QUERY:
            self._send(self._payload(KIND_ANNOUNCE), (addr[0], self.discovery_port))

    def _purge(self, now: float) -> None:
        with self._lock:
            expired = [k for k, v in self._devices.items() if now - v.last_seen > self.ttl]
            for key in expired:
                self._devices.pop(key, None)
