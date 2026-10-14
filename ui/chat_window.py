"""
远程聊天窗口

复用 core.protocol 的标准聊天消息，避免出现第二套报文格式。

两种角色共用一个窗口：
    - 本机作为控制端（bridge 有连接）：消息走本机发起的连接；
    - 本机作为被控端（server 有客户端在线）：消息由服务端广播出去。
两者同时可用时以控制端连接为准；都没有时提示"尚未连接"。
"""

from __future__ import annotations

import html
import logging

from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ui import theme
from ui.icons import app_icon

logger = logging.getLogger("LAN_REMOTE")


class ChatWindow(QWidget):
    """点对点文本聊天。"""
    def __init__(self, bridge=None, title: str = "远程聊天", parent=None, server=None):
        super().__init__(parent)
        self._bridge = bridge
        # 本机同时可以扮演两种角色：
        #   bridge 有连接 -> 我是控制端，消息走本机发起的连接；
        #   server 有客户端 -> 我是被控端，消息由服务端广播给控制端。
        # 优先级：控制端连接 > 被控端客户端，二者同时存在时以控制端为准。
        self._server = server

        self.setWindowTitle(title)
        self.setWindowIcon(app_icon())
        self.resize(420, 520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.history = QTextEdit()
        self.history.setReadOnly(True)
        self.history.setObjectName("chatView")
        layout.addWidget(self.history, 1)
        # 记录全部消息：主题切换后按新配色整页重画
        self._messages: list[tuple[str, str]] = []
        theme.register_refresh(self._rerender_all)

        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("输入消息，回车发送")
        self.input.returnPressed.connect(self.send_message)

        button = QPushButton("发送")
        button.setObjectName("connectButton")
        button.clicked.connect(self.send_message)

        row.addWidget(self.input, 1)
        row.addWidget(button)
        layout.addLayout(row)

    # ------------------------------------------------------------------ 状态
    @property
    def control_connected(self) -> bool:
        """本机作为控制端时是否已连上对端。"""
        return bool(self._bridge is not None and self._bridge.connected)

    @property
    def hosting_clients(self) -> bool:
        """本机作为被控端时是否有控制端在线。"""
        try:
            return bool(self._server is not None and self._server.client_count() > 0)
        except Exception:
            return False

    @property
    def available(self) -> bool:
        return self.control_connected or self.hosting_clients

    # ------------------------------------------------------------------ 发送
    def send_message(self) -> None:
        text = self.input.text().strip()
        if not text:
            return

        if self.control_connected:
            self._bridge.send_chat(text)
            self._record_sent(text)
            return

        if self.hosting_clients:
            if self._server.send_chat(text):
                self._record_sent(text)
            else:
                self.append("系统", "发送失败，对方可能刚刚断开")
            return

        self.append("系统", "尚未连接到远端，消息未发送")

    def _record_sent(self, text: str) -> None:
        self.append("我", text)
        self.input.clear()

    # ------------------------------------------------------------------ 接收
    def append(self, sender: str, text: str) -> None:
        """追加一条消息（记录下来，便于切主题后整页重画）。"""
        self._messages.append((str(sender), str(text)))
        self._render_message(str(sender), str(text))

    def _render_message(self, sender: str, text: str) -> None:
        """
        渲染一条气泡消息：自己的靠右带品牌色，对方的靠左。

        Qt 富文本的 CSS 支持非常有限（实测 border-radius / span 的
        background-color 都不生效），能做出「气泡贴内容」效果的唯一写法是
        **嵌套表格 + td 的 bgcolor 属性**：内层表格按内容收缩，外层用
        占位列把气泡挤到左边或右边。
        """
        mine = sender == "我"
        bubble_bg = theme.color("accent_soft") if mine else theme.color("surface_2")
        who_color = theme.color("accent") if mine else theme.color("text_3")

        who = html.escape(sender)
        body = html.escape(text).replace("\n", "<br>")
        who_html = f'<span style="color:{who_color}; font-size:11px;">{who}</span>'
        bubble = (
            f'<table cellspacing="0" cellpadding="0" bgcolor="{bubble_bg}"><tr>'
            f'<td style="padding:7px 12px; color:{theme.color("text")};">{body}</td>'
            f"</tr></table>"
        )
        spacer = '<td width="24%"></td>'
        if mine:
            row = f'<tr>{spacer}<td align="right">{who_html}<br>{bubble}</td></tr>'
        else:
            row = f'<tr><td align="left">{who_html}<br>{bubble}</td>{spacer}</tr>'
        self.history.append(f'<table width="100%" cellspacing="0" cellpadding="0">{row}</table>')

    def _rerender_all(self) -> None:
        """主题切换后按新配色重画全部气泡。"""
        self.history.clear()
        for sender, text in list(self._messages):
            self._render_message(sender, text)

    def on_message(self, text: str, sender: str = "") -> None:
        self.append(sender or "对端", text)
