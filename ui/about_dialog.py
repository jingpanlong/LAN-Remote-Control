"""
关于对话框

展示软件信息与联系方式，卡片式排版。
"""

from __future__ import annotations

import platform

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QGuiApplication
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

import config
from ui.icons import app_icon, app_pixmap

AUTHOR_WECHAT = "景同志"
AUTHOR_XHS = "景同学"
GITHUB_URL = config.GITHUB_URL
DOWNLOAD_URL = config.DOWNLOAD_URL


class AboutDialog(QDialog):
    """软件信息。"""

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowTitle(f"关于 {config.APP_NAME}")
        self.setModal(True)
        self.setFixedWidth(480)
        self.setWindowIcon(app_icon())

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)

        card = QWidget()
        card.setObjectName("aboutCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 22, 22, 18)
        layout.setSpacing(10)

        logo = QLabel()
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = app_pixmap(56)
        if not pixmap.isNull():
            logo.setPixmap(pixmap)
        layout.addWidget(logo)

        name = QLabel(config.APP_NAME)
        name.setObjectName("aboutName")
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(name)

        version = QLabel(f"v{config.VERSION}")
        version.setObjectName("headerVersion")
        version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(version, 0, Qt.AlignmentFlag.AlignHCenter)

        intro = QLabel(
            "局域网远程控制工具：屏幕实时查看、鼠标键盘远程操作。\n"
            "支持「设备 ID 授权」与「临时密码」两种接入方式，无需公网。"
        )
        intro.setObjectName("aboutDesc")
        intro.setWordWrap(True)
        intro.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(intro)

        env = QLabel(
            f"运行环境：Python {platform.python_version()} · {platform.system()}\n"
            f"技术栈：PyQt6 · websockets · mss + OpenCV · PyAutoGUI"
        )
        env.setObjectName("aboutMeta")
        env.setWordWrap(True)
        env.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(env)

        self.contact = QLabel(
            f"<b>联系作者</b><br>"
            f"微信公众号：{AUTHOR_WECHAT}<br>"
            f"小红书：{AUTHOR_XHS}<br>"
            f'开源仓库：<a href="{GITHUB_URL}">{GITHUB_URL.replace("https://", "")}</a>'
        )
        self.contact.setObjectName("contactCard")
        self.contact.setTextFormat(Qt.TextFormat.RichText)
        self.contact.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.contact.setOpenExternalLinks(True)
        self.contact.setWordWrap(True)
        self.contact.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.contact)

        repo_row = QHBoxLayout()
        repo_row.setSpacing(8)
        open_repo = QPushButton("GitHub 项目主页")
        open_repo.setObjectName("ghostButton")
        open_repo.clicked.connect(self._open_github)
        copy_repo = QPushButton("复制下载地址")
        copy_repo.setObjectName("ghostButton")
        copy_repo.clicked.connect(lambda: self._copy(DOWNLOAD_URL))
        repo_row.addWidget(open_repo, 1)
        repo_row.addWidget(copy_repo, 1)
        layout.addLayout(repo_row)

        row = QHBoxLayout()
        row.setSpacing(8)
        copy_wechat = QPushButton("复制公众号")
        copy_wechat.setObjectName("ghostButton")
        copy_wechat.clicked.connect(lambda: self._copy(AUTHOR_WECHAT))
        copy_xhs = QPushButton("复制小红书")
        copy_xhs.setObjectName("ghostButton")
        copy_xhs.clicked.connect(lambda: self._copy(AUTHOR_XHS))
        close = QPushButton("关闭")
        close.setObjectName("connectButton")
        close.clicked.connect(self.accept)

        row.addWidget(copy_wechat, 1)
        row.addWidget(copy_xhs, 1)
        row.addWidget(close, 1)
        layout.addLayout(row)

        outer.addWidget(card)

    @staticmethod
    def _copy(text: str) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)

    @staticmethod
    def _open_github() -> None:
        QDesktopServices.openUrl(QUrl(GITHUB_URL))


def open_about(parent=None) -> None:
    AboutDialog(parent).exec()
