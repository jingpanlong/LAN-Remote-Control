"""
心跳存活判定
"""

from __future__ import annotations

import threading
import time


class HeartBeat:
    """记录最后一次收到心跳的时间，用于判断对端是否在线。"""

    def __init__(self, timeout: float = 10.0):
        self.timeout = float(timeout)
        self._lock = threading.Lock()
        self._last = time.time()

    def update(self) -> None:
        with self._lock:
            self._last = time.time()

    def elapsed(self) -> float:
        with self._lock:
            return time.time() - self._last

    def online(self) -> bool:
        return self.elapsed() < self.timeout
