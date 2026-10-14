"""
剪贴板读写

统一使用 pyperclip（跨平台、无 GUI 依赖）。
不使用 tkinter：Tk 实例必须在主线程创建，在后台线程调用会直接崩溃。
"""

from __future__ import annotations

import logging

try:
    import pyperclip
except ImportError:  # pragma: no cover
    pyperclip = None

logger = logging.getLogger("LAN_REMOTE")


class Clipboard:
    """剪贴板工具（全部为静态方法）。"""

    @staticmethod
    def available() -> bool:
        return pyperclip is not None

    @staticmethod
    def get() -> str:
        """读取剪贴板；失败返回空字符串。"""
        if pyperclip is None:
            return ""
        try:
            return pyperclip.paste() or ""
        except Exception as exc:
            logger.debug("读取剪贴板失败：%s", exc)
            return ""

    @staticmethod
    def set(text: str) -> bool:
        """写入剪贴板；返回是否成功。"""
        if pyperclip is None or text is None:
            return False
        try:
            pyperclip.copy(str(text))
            return True
        except Exception as exc:
            logger.debug("写入剪贴板失败：%s", exc)
            return False
