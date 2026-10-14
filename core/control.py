"""
远程控制执行端

负责在被控端把收到的鼠标 / 键盘指令落到操作系统。

坐标映射说明：
    远端画面可能被缩放（见 Encoder.fit），也可能两端分辨率/DPI 不同，
    因此所有坐标都按「画面尺寸 -> 本机屏幕尺寸」做一次等比映射。
"""

from __future__ import annotations

import logging
import platform

import pyautogui

logger = logging.getLogger("LAN_REMOTE")

# 远程控制场景必须关闭失效保护（鼠标顶到屏幕角落不再抛异常），并取消默认停顿
pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0

_VALID_BUTTONS = {"left", "right", "middle", "primary", "secondary"}
_VALID_ACTIONS = {"click", "down", "up"}
_MAX_CLICK_COUNT = 3


class Control:
    """鼠标 / 键盘控制。所有方法均为静态方法，可直接调用。"""

    # ------------------------------------------------------------------ 屏幕信息
    @staticmethod
    def screen_size() -> dict:
        width, height = pyautogui.size()
        return {"width": width, "height": height}

    @staticmethod
    def monitor_info() -> dict:
        info = {"system": platform.system(), "monitor": 1}
        try:
            info.update(Control.screen_size())
        except Exception as exc:  # 无图形会话时可能失败
            logger.warning("获取屏幕尺寸失败：%s", exc)
        return info

    # ------------------------------------------------------------------ 坐标映射
    @staticmethod
    def map_position(x, y, source_width=None, source_height=None) -> tuple[int, int]:
        """把画面坐标映射为本机屏幕坐标。"""
        target_width, target_height = pyautogui.size()

        try:
            x = float(x)
            y = float(y)
        except (TypeError, ValueError):
            raise ValueError("坐标必须是数字")

        if source_width and source_height and source_width > 0 and source_height > 0:
            if source_width != target_width or source_height != target_height:
                x = x * target_width / float(source_width)
                y = y * target_height / float(source_height)

        new_x = max(0, min(target_width - 1, int(round(x))))
        new_y = max(0, min(target_height - 1, int(round(y))))
        return new_x, new_y

    # ------------------------------------------------------------------ 鼠标
    @staticmethod
    def mouse_move(x, y, source_width=None, source_height=None) -> None:
        pos_x, pos_y = Control.map_position(x, y, source_width, source_height)
        pyautogui.moveTo(pos_x, pos_y, _pause=False)

    @staticmethod
    def mouse_click(button: str = "left", action: str = "click", count: int = 1) -> None:
        if button not in _VALID_BUTTONS:
            button = "left"
        action = action if action in _VALID_ACTIONS else "click"
        try:
            count = max(1, min(_MAX_CLICK_COUNT, int(count)))
        except (TypeError, ValueError):
            count = 1

        if action == "down":
            pyautogui.mouseDown(button=button, _pause=False)
        elif action == "up":
            pyautogui.mouseUp(button=button, _pause=False)
        elif count > 1:
            pyautogui.click(button=button, clicks=count, interval=0.05, _pause=False)
        else:
            pyautogui.click(button=button, _pause=False)

    @staticmethod
    def double_click(button: str = "left") -> None:
        pyautogui.click(button=button, clicks=2, interval=0.05, _pause=False)

    @staticmethod
    def scroll(value, x=None, y=None) -> None:
        """滚轮。value 为滚动格数，正数向上、负数向下。"""
        try:
            amount = int(value)
        except (TypeError, ValueError):
            return
        if not amount:
            return
        amount = max(-50, min(50, amount))
        try:
            if x is not None and y is not None:
                pos = Control.map_position(x, y)
                pyautogui.moveTo(pos[0], pos[1], _pause=False)
            pyautogui.scroll(amount, _pause=False)
        except Exception as exc:
            logger.warning("滚轮执行失败：%s", exc)

    # ------------------------------------------------------------------ 键盘
    @staticmethod
    def keyboard(key: str, action: str = "press") -> None:
        """统一键盘入口：press / down / up。"""
        if not isinstance(key, str) or not key:
            return
        if action == "down":
            pyautogui.keyDown(key, _pause=False)
        elif action == "up":
            pyautogui.keyUp(key, _pause=False)
        else:
            pyautogui.press(key, _pause=False)

    # 兼容旧调用名
    key_press = keyboard

    @staticmethod
    def hotkey(*keys) -> None:
        if keys:
            pyautogui.hotkey(*keys, _pause=False)

    @staticmethod
    def key_down(key: str) -> None:
        if key:
            pyautogui.keyDown(key, _pause=False)

    @staticmethod
    def key_up(key: str) -> None:
        if key:
            pyautogui.keyUp(key, _pause=False)
