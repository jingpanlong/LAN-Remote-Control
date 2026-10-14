"""
剪贴板同步

本地剪贴板发生变化时对外产出内容，避免重复推送同一份数据。
"""

from __future__ import annotations

import logging

from core.clipboard import Clipboard

logger = logging.getLogger("LAN_REMOTE")

MAX_TEXT_LENGTH = 1024 * 1024  # 单次同步上限 1MB，避免拖垮连接


class ClipboardSync:
    """带变更检测的剪贴板同步器。"""

    def __init__(self, max_length: int = MAX_TEXT_LENGTH):
        self._last = ""
        self._max_length = max_length

    def get(self) -> str | None:
        """返回发生变化的新内容；无变化或不可用返回 None。"""
        data = Clipboard.get()
        if not data or data == self._last:
            return None
        if len(data) > self._max_length:
            logger.debug("剪贴板内容超过 %d 字节，跳过同步", self._max_length)
            return None
        self._last = data
        return data

    def mark(self, text: str) -> None:
        """将某个值标记为“已同步”，避免回环推送。"""
        self._last = text or ""

    def set(self, text: str) -> bool:
        """把远端内容写入本地剪贴板。"""
        if not Clipboard.set(text):
            return False
        self._last = text or ""
        return True
