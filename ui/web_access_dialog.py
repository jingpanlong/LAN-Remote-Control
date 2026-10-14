"""
手机访问对话框

展示访问链接与二维码：手机扫码（或输入链接）即可在浏览器里
远程控制本机，无需安装任何 App。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication, QImage, QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import config
from core.web_access import render_qr_png
from ui.icons import app_icon


class WebAccessDialog(QDialog):
    """展示手机访问链接 + 二维码。"""

    def __init__(self, device_id: str, ip: str, url: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("手机访问")
        self.setWindowIcon(app_icon())
        self.setModal(True)
        self.setFixedWidth(420)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 16)
        outer.setSpacing(12)

        title = QLabel("手机扫码，浏览器直接控制本机")
        title.setObjectName("aboutName")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(title)

        desc = QLabel(
            "1. 手机与本机连接同一 Wi-Fi / 局域网\n"
            "2. 用相机扫码，或浏览器输入下方链接\n"
            "3. 在网页中输入访问密码（或请求授权）即可远程"
        )
        desc.setObjectName("aboutDesc")
        desc.setWordWrap(True)
        outer.addWidget(desc)

        # 二维码
        qr_row = QHBoxLayout()
        qr_row.addStretch(1)
        self.qr_label = QLabel()
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        png = render_qr_png(url)
        if png:
            image = QImage.fromData(png, "PNG")
            if not image.isNull():
                self.qr_label.setPixmap(
                    QPixmap.fromImage(image).scaled(
                        220, 220,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
        if self.qr_label.pixmap() is None or self.qr_label.pixmap().isNull():
            self.qr_label.setText("（二维码生成失败，请直接输入链接）")
            self.qr_label.setStyleSheet("color:#999999;")
        qr_row.addWidget(self.qr_label)
        qr_row.addStretch(1)
        outer.addLayout(qr_row)

        # 链接
        self.url_label = QLabel(url)
        self.url_label.setObjectName("contactCard")
        self.url_label.setWordWrap(True)
        self.url_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.url_label)

        hint = QLabel(f"设备 ID：{device_id} · 网页端口 {config.web_port()}（占用自动后移）")
        hint.setObjectName("aboutMeta")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(hint)

        row = QHBoxLayout()
        row.setSpacing(8)
        copy_url = QPushButton("复制链接")
        copy_url.setObjectName("ghostButton")
        copy_url.clicked.connect(lambda: self._copy(url))
        close = QPushButton("关闭")
        close.setObjectName("connectButton")
        close.clicked.connect(self.accept)
        row.addWidget(copy_url, 1)
        row.addWidget(close, 1)
        outer.addLayout(row)

    @staticmethod
    def _copy(text: str) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)
