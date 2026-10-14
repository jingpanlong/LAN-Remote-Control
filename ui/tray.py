"""
系统托盘

修复要点：
    - 图标使用绝对路径（此前相对路径依赖工作目录，换个目录启动就没有图标）
    - “退出”真正结束进程（此前只调用了 close()，窗口被拦截后隐藏，程序永远退不掉）
    - 双击托盘图标恢复主窗口
"""

from __future__ import annotations

import logging

from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

import config
from ui.icons import app_icon, glyph

logger = logging.getLogger("LAN_REMOTE")


class TrayManager:
    """托盘图标与菜单。"""

    def __init__(self, window):
        self.window = window
        self.tray = QSystemTrayIcon()
        self.tray.setIcon(app_icon())
        self.tray.setToolTip(f"{config.APP_NAME} v{config.VERSION}")

        menu = QMenu()

        show_action = QAction(glyph("home", 16), "打开主窗口", menu)
        show_action.triggered.connect(self.show_window)
        menu.addAction(show_action)

        remote_action = QAction(glyph("monitor", 16), "远程桌面", menu)
        remote_action.triggered.connect(self.show_remote)
        menu.addAction(remote_action)

        chat_action = QAction(glyph("chat", 16), "远程聊天", menu)
        chat_action.triggered.connect(self.show_chat)
        menu.addAction(chat_action)

        files_action = QAction(glyph("folder", 16), "文件传输", menu)
        files_action.triggered.connect(self.show_files)
        menu.addAction(files_action)

        menu.addSeparator()

        exit_action = QAction(glyph("power", 16), "退出", menu)
        exit_action.triggered.connect(self.exit)
        menu.addAction(exit_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_activated)

        if not QSystemTrayIcon.isSystemTrayAvailable():
            logger.warning("当前系统不支持托盘图标")
        self.tray.show()

    # ------------------------------------------------------------------ 行为
    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_window()

    def show_window(self) -> None:
        if self.window.isMinimized():
            self.window.showNormal()
        else:
            self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def show_remote(self) -> None:
        if hasattr(self.window, "show_remote_window"):
            self.window.show_remote_window()

    def show_chat(self) -> None:
        if hasattr(self.window, "open_chat"):
            self.window.open_chat()

    def show_files(self) -> None:
        if hasattr(self.window, "open_files"):
            self.window.open_files()

    def notify(self, title: str, message: str) -> None:
        try:
            self.tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 4000)
        except Exception:
            logger.debug("托盘通知发送失败")

    def exit(self) -> None:
        """真正退出：先收尾资源，再结束 Qt 事件循环。"""
        try:
            self.window.force_quit = True
            self.window._cleanup()
        except Exception:
            logger.exception("退出清理失败")

        self.tray.hide()
        app = QApplication.instance()
        if app is not None:
            app.quit()
