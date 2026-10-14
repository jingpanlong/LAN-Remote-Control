"""
远程权限控制

被控端可以按能力维度（控制 / 文件 / 剪贴板）逐项授权。
"""

from __future__ import annotations

import threading

from core.protocol import MessageType, Protocol


class Permission:
    """权限状态容器（线程安全）。"""

    def __init__(self, control: bool = False, file: bool = False, clipboard: bool = False):
        self._lock = threading.RLock()
        self.control = control
        self.file = file
        self.clipboard = clipboard

    # ------------------------------------------------------------------ 批量
    def allow_all(self) -> None:
        with self._lock:
            self.control = self.file = self.clipboard = True

    def deny_all(self) -> None:
        with self._lock:
            self.control = self.file = self.clipboard = False

    # ------------------------------------------------------------------ 单项
    def set_control(self, value: bool) -> None:
        with self._lock:
            self.control = bool(value)

    def set_file(self, value: bool) -> None:
        with self._lock:
            self.file = bool(value)

    def set_clipboard(self, value: bool) -> None:
        with self._lock:
            self.clipboard = bool(value)

    # ------------------------------------------------------------------ 查询
    def check(self, msg_type: str) -> bool:
        """判断某种消息是否被授权。"""
        with self._lock:
            if msg_type in (
                MessageType.MOUSE_MOVE,
                MessageType.MOUSE_CLICK,
                MessageType.MOUSE_SCROLL,
                MessageType.KEYBOARD,
            ):
                return self.control
            if msg_type in (MessageType.FILE_INFO, MessageType.FILE_DATA, MessageType.FILE_FINISH, MessageType.FILE_CANCEL):
                return self.file
            if msg_type == MessageType.CLIPBOARD:
                return self.clipboard
        return True

    def to_dict(self) -> dict:
        with self._lock:
            return {"control": self.control, "file": self.file, "clipboard": self.clipboard}

    def to_message(self) -> str:
        return Protocol.create(MessageType.PERMISSION_RESPONSE, self.to_dict())
