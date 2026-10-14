"""
远程连接授权对话框

被控端在有人请求连接时弹出，由用户决定是否放行。
"""

from __future__ import annotations

from PyQt6.QtWidgets import QMessageBox

from ui.icons import app_icon


class PermissionDialog(QMessageBox):
    """请求远程控制授权。"""

    def __init__(self, peer: str = "对方", parent=None):
        super().__init__(parent)

        self.setWindowTitle("远程连接请求")
        self.setWindowIcon(app_icon())
        self.setIcon(QMessageBox.Icon.Question)
        self.setText(f"{peer} 请求连接并控制这台电脑")
        self.setInformativeText("同意后对方可以看到本机屏幕并操作鼠标键盘，请确认对方身份。")
        self.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        self.setDefaultButton(QMessageBox.StandardButton.No)

    @staticmethod
    def ask(peer: str = "对方", parent=None) -> bool:
        """弹出对话框，返回用户是否同意。"""
        dialog = PermissionDialog(peer, parent)
        return dialog.exec() == QMessageBox.StandardButton.Yes
