"""
屏幕采集模块

在独立后台线程中持续抓取屏幕最新帧。

注意：mss 实例**必须**在采集线程内创建并销毁，跨线程复用会在 Windows 上
产生未定义行为，因此这里不在 __init__ 中创建 mss 对象。

帧序号（frame_seq）说明：
    推流端需要知道「手上这一帧是否已经编码发送过」。仅比较对象引用不可靠
    （mss 复用缓冲区），因此这里每产出一张新图就把序号 +1，
    推流端只在序号变化时才编码，从根本上避免重复编码同一帧空耗 CPU。
"""

from __future__ import annotations

import logging
import threading
import time

import cv2
import mss
import numpy as np

import config

logger = logging.getLogger("LAN_REMOTE")


class ScreenCapture:
    """高速屏幕采集器，始终向外部提供最新一帧。"""

    def __init__(self, monitor: int = None, fps: int = None):
        self._monitor_index = int(monitor if monitor is not None else config.screen_monitor())
        self._interval = self._interval_for(fps)
        self._frame: np.ndarray | None = None
        self._size = (0, 0)
        self._seq = 0                        # 新帧序号，单调递增
        self._monitors: list[dict] = []
        self._monitor_signature: tuple = ()
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_error: str | None = None

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _interval_for(fps) -> float:
        try:
            value = int(fps if fps is not None else config.screen_fps())
        except (TypeError, ValueError):
            value = config.screen_fps()
        value = max(1, min(config.SCREEN_FPS_MAX, value))
        return 1.0 / value

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, name="ScreenCapture", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._running = False
        if self._thread and self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=timeout)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------ 采集
    def _capture_loop(self) -> None:
        sct = None
        try:
            sct = mss.mss()
        except Exception as exc:  # mss 在无桌面会话（如服务账户）下会失败
            self._last_error = f"初始化屏幕采集失败：{exc}"
            logger.error(self._last_error)
            self._running = False
            return

        consecutive_errors = 0
        try:
            while self._running:
                cycle_start = time.perf_counter()
                try:
                    monitors = list(sct.monitors)
                    self._refresh_monitors(monitors)

                    index = self._monitor_index
                    if index < 0 or index >= len(monitors):
                        # 目标显示器已拔出时回退到主显示器
                        index = 1 if len(monitors) > 1 else 0
                        self._monitor_index = index

                    img = sct.grab(monitors[index])
                    # np.array 会复制一份，避免后续编码时 mss 缓冲区被覆盖
                    frame = np.array(img)
                    if frame.ndim == 3 and frame.shape[2] == 4:
                        frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                    elif frame.ndim == 3 and frame.shape[2] == 3:
                        frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                    with self._lock:
                        self._frame = frame
                        self._size = (frame.shape[1], frame.shape[0])
                        self._seq += 1
                    consecutive_errors = 0
                except Exception as exc:
                    consecutive_errors += 1
                    self._last_error = f"抓屏失败：{exc}"
                    if consecutive_errors <= 3 or consecutive_errors % 50 == 0:
                        logger.warning(self._last_error)
                    # 连续失败时降低重试频率，避免空转刷屏
                    if consecutive_errors > 10:
                        time.sleep(1.0)
                        continue

                elapsed = time.perf_counter() - cycle_start
                sleep_for = self._interval - elapsed
                if sleep_for > 0:
                    time.sleep(sleep_for)
        finally:
            try:
                if sct is not None:
                    sct.close()
            except Exception:
                pass
            logger.info("屏幕采集线程已退出")

    def _refresh_monitors(self, monitors: list) -> None:
        """仅在显示器布局发生变化时重建列表（避免每帧无谓分配）。"""
        signature = tuple(
            (m.get("width"), m.get("height"), m.get("left"), m.get("top")) for m in monitors
        )
        if signature == self._monitor_signature:
            return
        self._monitor_signature = signature
        detail = []
        for index, item in enumerate(monitors):
            detail.append(
                {
                    "index": index,
                    "width": int(item.get("width", 0)),
                    "height": int(item.get("height", 0)),
                    "left": int(item.get("left", 0)),
                    "top": int(item.get("top", 0)),
                    "primary": index == 1,
                }
            )
        with self._lock:
            self._monitors = detail

    # ------------------------------------------------------------------ 读取
    def get_frame(self) -> np.ndarray | None:
        """获取最新一帧（无数据返回 None）。"""
        with self._lock:
            return self._frame

    def frame_size(self) -> tuple[int, int]:
        """返回最近一帧的 (宽, 高)。"""
        with self._lock:
            return self._size

    @property
    def frame_seq(self) -> int:
        """最新帧序号；值不变说明自上次读取以来没有产生新帧。"""
        with self._lock:
            return self._seq

    @property
    def last_error(self) -> str | None:
        return self._last_error

    # ------------------------------------------------------------------ 显示器
    @property
    def monitor_index(self) -> int:
        return self._monitor_index

    def monitors(self) -> list[dict]:
        """可用显示器列表（索引 0 为所有屏幕的合成画布）。"""
        with self._lock:
            return [dict(item) for item in self._monitors]

    def set_monitor(self, index) -> bool:
        """切换采集目标显示器；采集线程会在下一轮自动生效。"""
        try:
            index = int(index)
        except (TypeError, ValueError):
            return False
        if index < 0:
            return False
        self._monitor_index = index
        return True

    # ------------------------------------------------------------------ 配置
    def set_fps(self, fps: int) -> None:
        self._interval = self._interval_for(fps)
