"""
图像编码模块

将 BGR 图像帧压缩为 JPEG 字节流，并支持按宽度上限等比缩放以降低带宽。
"""

from __future__ import annotations

import logging

import cv2
import numpy as np

import config

logger = logging.getLogger("LAN_REMOTE")


class Encoder:
    """JPEG 编码器（线程安全：所有状态均为简单的可重入读写）。"""

    MIN_QUALITY = 20
    MAX_QUALITY = 95

    def __init__(self, quality: int = None, max_width: int = None):
        self._quality = self._clamp(config.screen_quality() if quality is None else quality)
        self._max_width = config.screen_max_width() if max_width is None else max_width
        self._params = [int(cv2.IMWRITE_JPEG_QUALITY), self._quality]

    # ------------------------------------------------------------------ 配置
    @classmethod
    def _clamp(cls, value) -> int:
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = config.screen_quality()
        return max(cls.MIN_QUALITY, min(cls.MAX_QUALITY, value))

    @property
    def quality(self) -> int:
        return self._quality

    def set_quality(self, value) -> int:
        self._quality = self._clamp(value)
        self._params = [int(cv2.IMWRITE_JPEG_QUALITY), self._quality]
        return self._quality

    def get_quality(self) -> int:
        return self._quality

    def set_max_width(self, value) -> None:
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = 0
        self._max_width = max(0, value)

    # ------------------------------------------------------------------ 缩放
    def fit(self, frame: np.ndarray) -> np.ndarray:
        """按 max_width 等比缩小；无需缩放时原样返回。"""
        if frame is None or self._max_width <= 0:
            return frame
        height, width = frame.shape[:2]
        if width <= self._max_width:
            return frame
        scale = self._max_width / float(width)
        new_size = (self._max_width, max(1, int(round(height * scale))))
        return cv2.resize(frame, new_size, interpolation=cv2.INTER_AREA)

    # ------------------------------------------------------------------ 编码
    def encode_jpeg(self, frame: np.ndarray) -> bytes | None:
        """编码为 JPEG 字节流；失败返回 None（调用方需判空）。"""
        if frame is None or not isinstance(frame, np.ndarray) or frame.size == 0:
            return None
        try:
            frame = self.fit(frame)
            ok, buffer = cv2.imencode(".jpg", frame, self._params)
        except cv2.error as exc:
            logger.error("JPEG 编码失败：%s", exc)
            return None
        if not ok:
            return None
        return buffer.tobytes()

    def encode_scaled(self, frame: np.ndarray) -> tuple[bytes, int, int] | None:
        """
        缩放 + 编码一步完成，返回 (JPEG 字节, 宽, 高)。

        相比「先 fit 再 encode_jpeg」少一次缩放调用与一次数组拷贝，
        在高帧率推流（60 FPS）下这点开销很关键。
        """
        if frame is None or not isinstance(frame, np.ndarray) or frame.size == 0:
            return None
        try:
            scaled = self.fit(frame)
            ok, buffer = cv2.imencode(".jpg", scaled, self._params)
        except cv2.error as exc:
            logger.error("JPEG 编码失败：%s", exc)
            return None
        if not ok:
            return None
        height, width = scaled.shape[:2]
        return buffer.tobytes(), int(width), int(height)
