"""
Windows 开机自启（写入 HKCU\\...\\Run）

要点：
    - 使用绝对路径，避免依赖当前工作目录
    - 优先使用 pythonw.exe，开机启动时不弹出控制台黑窗
"""

from __future__ import annotations

import logging
import os
import sys

import config

logger = logging.getLogger("LAN_REMOTE")

APP_NAME = config.APP_NAME
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MAIN_SCRIPT = os.path.join(config.BASE_DIR, "main.py")


def _python_executable() -> str:
    """返回用于启动的解释器路径（优先无控制台窗口的 pythonw）。"""
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)

    executable = os.path.abspath(sys.executable)
    directory, name = os.path.split(executable)
    if name.lower() == "python.exe":
        candidate = os.path.join(directory, "pythonw.exe")
        if os.path.exists(candidate):
            return candidate
    return executable


def _winreg():
    try:
        import winreg  # noqa: PLC0415
    except ImportError:
        logger.warning("当前平台不支持开机自启（非 Windows）")
        return None
    return winreg


def enable_auto_start() -> bool:
    """开启开机自启，返回是否成功。"""
    winreg = _winreg()
    if winreg is None or not os.path.exists(MAIN_SCRIPT):
        if not os.path.exists(MAIN_SCRIPT):
            logger.error("未找到入口文件：%s", MAIN_SCRIPT)
        return False

    command = f'"{_python_executable()}" "{MAIN_SCRIPT}"'
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
        logger.info("已开启开机自启：%s", command)
        return True
    except OSError as exc:
        logger.error("开启开机自启失败：%s", exc)
        return False


def disable_auto_start() -> bool:
    """关闭开机自启，返回是否成功（原本未开启也算成功）。"""
    winreg = _winreg()
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                return True
        logger.info("已关闭开机自启")
        return True
    except OSError as exc:
        logger.error("关闭开机自启失败：%s", exc)
        return False


def check_auto_start() -> bool:
    """查询是否已开启开机自启。"""
    winreg = _winreg()
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, APP_NAME)
        return True
    except (OSError, FileNotFoundError):
        return False
