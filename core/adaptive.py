"""
自适应画质调节

根据实测延迟与发送耗时动态调整 JPEG 质量与帧率。
"""

from __future__ import annotations

import threading
import time


class AdaptiveQuality:
    """在网络状况与画质之间自动取舍。"""

    MIN_QUALITY = 20
    MAX_QUALITY = 90
    MIN_FPS = 5
    MAX_FPS = 30

    def __init__(self, quality: int = 60, fps: int = 20):
        self._lock = threading.Lock()
        self.quality = self._clamp_quality(quality)
        self.fps = self._clamp_fps(fps)
        self.last_update = time.time()

    # ------------------------------------------------------------------ 工具
    @classmethod
    def _clamp_quality(cls, value) -> int:
        try:
            return max(cls.MIN_QUALITY, min(cls.MAX_QUALITY, int(value)))
        except (TypeError, ValueError):
            return 60

    @classmethod
    def _clamp_fps(cls, value) -> int:
        try:
            return max(cls.MIN_FPS, min(cls.MAX_FPS, int(value)))
        except (TypeError, ValueError):
            return 20

    # ------------------------------------------------------------------ 调节
    def update(self, delay_ms: float, bandwidth_mbps: float = None) -> dict:
        """
        根据延迟（毫秒）与可选带宽（Mbps）调整画质。

        返回 {"quality": int, "fps": int, "changed": bool}。
        """
        try:
            delay_ms = float(delay_ms)
        except (TypeError, ValueError):
            delay_ms = 0.0

        with self._lock:
            previous = (self.quality, self.fps)

            congested = delay_ms > 200 or (bandwidth_mbps is not None and bandwidth_mbps < 2)
            healthy = delay_ms < 50 and (bandwidth_mbps is None or bandwidth_mbps > 10)

            if congested:
                self.quality -= 10
                self.fps -= 5
            elif healthy:
                self.quality += 5
                self.fps += 2

            self.quality = self._clamp_quality(self.quality)
            self.fps = self._clamp_fps(self.fps)
            self.last_update = time.time()

            return {
                "quality": self.quality,
                "fps": self.fps,
                "changed": (self.quality, self.fps) != previous,
            }

    def state(self) -> dict:
        with self._lock:
            return {"quality": self.quality, "fps": self.fps}
