"""
后台任务管理

统一托管后台线程，程序退出时可一次性收尾，避免残留在后台导致端口无法释放。
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger("LAN_REMOTE")


class TaskManager:
    """线程任务容器。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    def add(self, target, args: tuple = (), name: str | None = None, daemon: bool = True) -> threading.Thread:
        """启动一个后台线程并纳入托管。"""
        thread = threading.Thread(target=target, args=args, name=name, daemon=daemon)
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
        thread.start()
        return thread

    def alive_count(self) -> int:
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            return len(self._threads)

    def stop_all(self, timeout: float = 2.0) -> None:
        """等待所有托管线程结束（daemon 线程超时后不再阻塞退出）。"""
        with self._lock:
            threads = list(self._threads)
            self._threads.clear()

        for thread in threads:
            if thread.is_alive():
                thread.join(timeout=timeout)
                if thread.is_alive():
                    logger.warning("后台线程 %s 未能在 %.1fs 内结束", thread.name, timeout)
