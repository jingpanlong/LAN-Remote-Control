"""
主窗口

修复要点：
    - 远程窗口在创建时就注入客户端桥接器（此前 client 永远为空，控制完全失效）
    - 密码刷新直接作用于服务端共享的 Credentials，真正生效
    - 状态栏 / 日志只通过信号接收后台线程消息
    - 关闭窗口仅隐藏到托盘，托盘“退出”才是真正退出
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from datetime import datetime

from PyQt6.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import config
from core.server import RemoteServer
from core.security import Credentials
from core.web_access import WebAccessServer
from database.database import Database, now_text
from ui import theme
from ui.about_dialog import open_about
from ui.chat_window import ChatWindow
from ui.client_bridge import ClientBridge
from ui.file_window import FileWindow
from ui.icons import app_icon, app_pixmap, eye_icon, glyph
from ui.remote_window import RemoteWindow
from ui.settings_dialog import open_settings
from ui.web_access_dialog import WebAccessDialog
from ui.widgets import SegmentedControl

logger = logging.getLogger("LAN_REMOTE")

LOG_MAX_LINES = 400
PASSWORD_MASK_CHAR = "●"
PASSWORD_VISIBLE_SECONDS = 6
HISTORY_LIMIT = 30
HISTORY_VISIBLE_ROWS = 4

# 主机名（或域名）的宽松校验；IP 走 ipaddress 模块精确校验
_HOSTNAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")


class MainWindow(QWidget):
    """LAN Remote 主控面板。"""

    # 服务端（事件循环线程）通知画质档位被远端修改，用信号转回 Qt 主线程
    remote_quality_changed = pyqtSignal(str, int, int, int)
    # 服务端（事件循环线程）收到控制端的聊天 / 文件事件，同样用信号转回主线程
    server_chat_received = pyqtSignal(str, str)
    server_file_event = pyqtSignal(str, dict)
    # 服务端有客户端认证成功（含授权接入）：callback(device_id, address)
    client_connected = pyqtSignal(str, str)
    # 已认证的控制端断开：callback(device_id, address)，被控端弹「对方已断开」提醒
    client_disconnected = pyqtSignal(str, str)

    def __init__(
        self,
        device_id: str,
        credentials: Credentials,
        ip: str,
        server: RemoteServer | None = None,
        discovery=None,
        parent=None,
        database: Database | None = None,
    ):
        super().__init__(parent)

        self.device_id = device_id
        self.credentials = credentials
        self.ip = ip
        self.server = server
        self.discovery = discovery

        self.tray = None
        self.force_quit = False
        # 记住本次连接目标，连接成功后才写入历史设备（失败不入库）
        self._active_host = ""
        self._active_port = 0
        self._monitor_signature: tuple = ()
        self._applying_quality = False
        # 用户手动点「开始连接」后置 True；失败时据此弹窗提醒
        self._manual_connect = False

        # 多设备并发会话：key = "host:port" -> {bridge, remote, chat, files, host, port}
        # 第一个会话复用主桥接器（既有信号接线不变），之后每个目标各自独立
        self.sessions: dict[str, dict] = {}
        self._active_session_key: str | None = None
        self._primary_session_key: str | None = None

        # 历史设备库（自检可传入临时库，避免污染用户的真实记录）
        self.db = database if database is not None else Database()
        self._owns_db = database is None

        self.bridge = ClientBridge(port=config.server_port(), parent=self)
        self.remote: RemoteWindow | None = None
        # 手机网页访问：HTTP 服务承载浏览器控制页（鉴权仍走 WebSocket 登录）
        self.web_access: WebAccessServer | None = None
        if server is not None:
            web = WebAccessServer(
                device_id,
                ws_port_provider=lambda: (server.actual_port or config.server_port()),
            )
            if web.start():
                self.web_access = web
                logger.info("手机网页访问已就绪：http://<本机IP>:%s/", web.actual_port)
        # 聊天 / 文件窗口提前建好（隐藏），这样在窗口打开之前收到的消息
        # 也会进历史记录，不会因为"先收后开"而丢消息。
        # 两个窗口同时拿到 bridge（控制端出口）与 server（被控端出口）。
        self.chat = ChatWindow(self.bridge, server=server)
        self.files = FileWindow(self.bridge, server=server)

        self._build_ui()
        self._load_style()
        self._wire_signals()
        self._restore_inputs()
        self._reload_history()

        # 周期性检查密码是否到期（到期自动刷新，不打扰界面）
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start()
        self._tick()

        # 密码查看后自动恢复打码
        self._mask_timer = QTimer(self)
        self._mask_timer.setSingleShot(True)
        self._mask_timer.timeout.connect(lambda: self._set_password_visible(False))

        # 服务端画质档位被控制端远程修改：该回调运行在事件循环线程，
        # 必须经由信号转到 Qt 主线程后再更新控件
        self.remote_quality_changed.connect(self._apply_remote_quality)
        self.server_chat_received.connect(self._on_chat)
        self.server_file_event.connect(self._on_file_event)
        self.client_connected.connect(self._on_client_connected)
        self.client_disconnected.connect(self._on_client_disconnected)
        if self.server is not None:
            self.server.on_quality_changed = self.remote_quality_changed.emit
            self.server.on_chat = self.server_chat_received.emit
            self.server.on_file_event = self.server_file_event.emit
            self.server.on_client_connected = self.client_connected.emit
            self.server.on_client_disconnected = self.client_disconnected.emit

    # ================================================================== 界面
    def _build_ui(self) -> None:
        self.setWindowTitle(f"{config.APP_NAME}  v{config.VERSION}")
        self.setWindowIcon(app_icon())
        self._apply_window_size()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 内容整体放进滚动区：窗口再矮也不会裁掉底部控件。
        # （历史上"主窗口显示不全"就是内容高度超过窗口导致的，这里做根本性解决）
        self.scroll = QScrollArea()
        self.scroll.setObjectName("mainScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(18, 18, 18, 16)
        layout.setSpacing(6)

        self.header = self._build_header()
        layout.addWidget(self.header)
        layout.addWidget(self._build_tabs(), 1)

        self.scroll.setWidget(content)
        root.addWidget(self.scroll, 1)

        # 底部状态栏固定在窗口底部，不随内容滚动
        bottom_bar = QWidget()
        bottom_bar.setObjectName("bottomBar")
        bottom = QHBoxLayout(bottom_bar)
        bottom.setContentsMargins(18, 8, 18, 12)
        bottom.setSpacing(8)
        self.status = QLabel("状态：就绪")
        self.status.setObjectName("statusBar")

        self.settings_button = QPushButton("设置")
        self.settings_button.setObjectName("ghostButton")
        self.settings_button.setIcon(glyph("settings", 16))
        self.settings_button.setIconSize(QSize(16, 16))
        self.settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_button.clicked.connect(self.open_settings)

        self.about_button = QPushButton("关于")
        self.about_button.setObjectName("ghostButton")
        self.about_button.setIcon(glyph("info", 16))
        self.about_button.setIconSize(QSize(16, 16))
        self.about_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.about_button.clicked.connect(self.open_about)

        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.settings_button)
        bottom.addWidget(self.about_button)
        root.addWidget(bottom_bar)
        self.bottom_bar = bottom_bar

        self._center_on_screen()

    def _build_tabs(self) -> QWidget:
        """
        五个标签页：本机信息 / 远程连接 / 远程工具 / 历史设备 / 运行日志。

        点击切换查看；历史设备与运行日志独立成页后，
        主窗口一屏只承载一件事，窗口高度也随内容大幅变矮。
        """
        holder = QWidget()

        self.tabs = SegmentedControl(
            (
                ("local", "本机信息"),
                ("connect", "远程连接"),
                ("tools", "远程工具"),
                ("history", "历史设备"),
                ("logs", "运行日志"),
            ),
            expand=True,
        )
        # 覆盖 objectName：标签页条要比工具栏分段控制器更高更醒目（见 style.qss #pageTabs）
        self.tabs.setObjectName("pageTabs")
        self.tabs.current_changed.connect(self._on_tab_changed)

        local_page = QWidget()
        local_box = QVBoxLayout(local_page)
        local_box.setContentsMargins(0, 0, 0, 0)
        local_box.setSpacing(6)
        local_group = self._build_local_group()
        local_group.setTitle("")  # 标签名即卡片标题，去掉重复的分组标题
        local_box.addWidget(local_group)
        local_box.addStretch(1)

        connect_page = QWidget()
        connect_box = QVBoxLayout(connect_page)
        connect_box.setContentsMargins(0, 0, 0, 0)
        connect_box.setSpacing(6)
        connect_group = self._build_connect_group()
        connect_group.setTitle("")
        connect_box.addWidget(connect_group)
        connect_box.addStretch(1)

        tools_page = QWidget()
        tools_box = QVBoxLayout(tools_page)
        tools_box.setContentsMargins(0, 0, 0, 0)
        tools_box.setSpacing(6)
        tools_group = self._build_tools_group()
        tools_group.setTitle("")
        tools_box.addWidget(tools_group)
        tools_box.addStretch(1)

        history_page = QWidget()
        history_box = QVBoxLayout(history_page)
        history_box.setContentsMargins(0, 0, 0, 0)
        history_box.setSpacing(6)
        history_group = self._build_history_group()
        history_group.setTitle("")
        history_box.addWidget(history_group)
        history_box.addStretch(1)

        logs_page = QWidget()
        logs_box = QVBoxLayout(logs_page)
        logs_box.setContentsMargins(0, 0, 0, 0)
        logs_box.setSpacing(6)
        logs_group = self._build_log_group()
        logs_group.setTitle("")
        logs_box.addWidget(logs_group, 1)

        self.pages = QStackedWidget()
        self.pages.setObjectName("pages")
        for page in (local_page, connect_page, tools_page, history_page, logs_page):
            self.pages.addWidget(page)

        box = QVBoxLayout(holder)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)
        box.addWidget(self.tabs)
        box.addWidget(self.pages, 1)

        # 默认落在远程连接页：多数时候打开程序就是为了连别人
        self.tabs.set_current("connect")
        self.pages.setCurrentIndex(1)
        return holder

    def _on_tab_changed(self, key: str) -> None:
        index = {"local": 0, "connect": 1, "tools": 2, "history": 3, "logs": 4}.get(key, 0)
        self.pages.setCurrentIndex(index)

    def show_tab(self, key: str) -> None:
        """程序化切到某个标签页（不触发额外逻辑）。"""
        self.tabs.set_current(key)
        self._on_tab_changed(key)

    def _build_header(self) -> QWidget:
        """顶部品牌区：图标 + 名称 + 版本胶囊 + 连接状态胶囊。"""
        header = QWidget()
        header.setObjectName("appHeader")
        row = QHBoxLayout(header)
        row.setContentsMargins(16, 14, 16, 14)
        row.setSpacing(12)

        self.logo_label = QLabel()
        self.logo_label.setObjectName("headerLogo")
        self.logo_label.setFixedSize(44, 44)
        self.logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = app_pixmap(40)
        if not pixmap.isNull():
            self.logo_label.setPixmap(pixmap)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)
        title = QLabel(config.APP_NAME)
        title.setObjectName("headerTitle")
        version = QLabel(f"v{config.VERSION}")
        version.setObjectName("headerVersion")
        title_row.addWidget(title)
        title_row.addWidget(version)
        title_row.addStretch(1)

        subtitle = QLabel("局域网远程控制 · 无需公网 · 端到端直连")
        subtitle.setObjectName("headerSubtitle")

        texts = QVBoxLayout()
        texts.setContentsMargins(0, 0, 0, 0)
        texts.setSpacing(3)
        texts.addLayout(title_row)
        texts.addWidget(subtitle)

        self.status_chip = QLabel("● 未连接")
        self.status_chip.setObjectName("statusChip")
        theme.set_prop(self.status_chip, "state", "offline")

        row.addWidget(self.logo_label)
        row.addLayout(texts, 1)
        row.addWidget(self.status_chip)
        return header

    def _set_status_chip(self, text: str, state: str) -> None:
        """更新连接状态胶囊（state: idle / pending / online / offline）。"""
        self.status_chip.setText(text)
        theme.set_prop(self.status_chip, "state", state)

    def _apply_window_size(self) -> None:
        """
        固定尺寸开窗，并适配屏幕可用区域。

        注意：**不要**用 setMinimumSize() 强行压低高度 —— 那会让窗口小于
        布局真实需要的高度，底部控件（日志区、状态栏）就会被裁切。
        这里只约束宽度，高度取固定值（600px 能容纳最高的历史设备页，
        其余页面的卡片会自然留一点呼吸空间），屏幕太小再按可用区域收缩。
        """
        preferred_w, preferred_h = 680, 600
        min_w, min_h = 580, 560

        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            width = max(min_w, min(preferred_w, available.width() - 40))
            height = max(min_h, min(preferred_h, available.height() - 50))
        else:
            width, height = preferred_w, preferred_h

        self.setMinimumWidth(min_w)
        self.resize(width, height)

    def _center_on_screen(self) -> None:
        """窗口在可用区域内居中。"""
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        x = available.x() + (available.width() - self.width()) // 2
        y = available.y() + (available.height() - self.height()) // 2
        self.move(max(available.x(), x), max(available.y(), y))

    def _build_local_group(self) -> QGroupBox:
        """
        本机信息：5 行紧凑网格（标签 | 值 | 标签 | 值）。

        用网格而不是逐行表单，一是信息密度高、观感整齐，
        二是省出纵向空间让日志卡片默认就能完整露出来。
        """
        group = QGroupBox("本机信息")
        grid = QGridLayout(group)
        grid.setContentsMargins(2, 2, 2, 2)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(11)

        def field_label(text: str) -> QLabel:
            label = QLabel(text)
            label.setObjectName("fieldLabel")
            return label

        def ghost(text: str, icon_name: str, slot) -> QPushButton:
            button = QPushButton(text)
            button.setObjectName("ghostButton")
            button.setIcon(glyph(icon_name, 14))
            button.setIconSize(QSize(14, 14))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(slot)
            return button

        # ---------------------------------------------------------- 设备 ID
        self.id_label = QLabel(self.device_id)
        self.id_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        copy_id = ghost("复制", "copy", self.copy_id)
        copy_id.setToolTip("复制设备 ID")
        grid.addWidget(field_label("设备 ID"), 0, 0)
        grid.addWidget(self.id_label, 0, 1)
        grid.addWidget(copy_id, 0, 3)

        # ---------------------------------------------------------- 临时密码
        self.password_field_label = field_label("临时密码")
        self.pwd_label = QLabel(PASSWORD_MASK_CHAR * len(self.credentials.password))
        self.pwd_label.setObjectName("passwordLabel")
        self.pwd_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._pwd_visible = False

        eye = QPushButton()
        eye.setObjectName("ghostButton")
        eye.setCheckable(True)
        eye.setFixedSize(34, 30)
        eye.setIcon(eye_icon(False))
        eye.setIconSize(QSize(16, 16))
        eye.setToolTip("显示 / 隐藏临时密码")
        eye.toggled.connect(self._set_password_visible)
        self.eye_button = eye

        refresh = ghost("", "refresh", self.refresh_password)
        refresh.setFixedSize(34, 30)
        refresh.setToolTip("立即刷新临时密码（旧密码随即失效）")
        self.password_refresh_button = refresh

        pwd_actions = QHBoxLayout()
        pwd_actions.setContentsMargins(0, 0, 0, 0)
        pwd_actions.setSpacing(6)
        pwd_actions.addWidget(eye)
        pwd_actions.addWidget(refresh)
        grid.addWidget(self.password_field_label, 1, 0)
        grid.addWidget(self.pwd_label, 1, 1)
        grid.addLayout(pwd_actions, 1, 3)

        # ---------------------------------------------------------- 网络状态
        self.ip_label = QLabel(self.ip)
        self.port_label = QLabel("—")
        self.client_label = QLabel("0")
        self.found_label = QLabel("—")

        grid.addWidget(field_label("本机 IP"), 2, 0)
        grid.addWidget(self.ip_label, 2, 1)
        grid.addWidget(field_label("端口"), 2, 2, Qt.AlignmentFlag.AlignRight)
        grid.addWidget(self.port_label, 2, 3)

        grid.addWidget(field_label("在线"), 3, 0)
        grid.addWidget(self.client_label, 3, 1)
        grid.addWidget(field_label("发现设备"), 3, 2, Qt.AlignmentFlag.AlignRight)
        grid.addWidget(self.found_label, 3, 3)

        # ---------------------------------------------------------- 画质 / 屏幕
        self.quality_combo = QComboBox()
        for key in config.QUALITY_PROFILE_ORDER:
            self.quality_combo.addItem(config.QUALITY_PROFILES[key][0], key)
        self.quality_combo.setToolTip(
            "一键联动「帧率 / JPEG 质量 / 分辨率上限」。\n"
            "切换后本机采集与编码参数立即生效，远端看到的画面会随之变化。"
        )
        self.quality_combo.currentIndexChanged.connect(self._on_local_quality_changed)

        self.monitor_combo = QComboBox()
        self.monitor_combo.addItem("主显示器", 1)
        self.monitor_combo.setMinimumWidth(128)
        self.monitor_combo.setToolTip("选择对外共享哪一块屏幕（多显示器时生效）")
        self.monitor_combo.currentIndexChanged.connect(self._on_local_monitor_changed)

        grid.addWidget(field_label("画质"), 4, 0)
        grid.addWidget(self.quality_combo, 4, 1)
        grid.addWidget(field_label("屏幕"), 4, 2, Qt.AlignmentFlag.AlignRight)
        grid.addWidget(self.monitor_combo, 4, 3)

        # ---------------------------------------------------------- 只看画面开关
        self.allow_control_box = QCheckBox("允许对方控制本机鼠标键盘")
        self.allow_control_box.setChecked(config.allow_control())
        self.allow_control_box.setToolTip(
            "取消勾选后进入「只看画面」模式：对方仍能看到屏幕，但鼠标键盘操作会被忽略。"
        )
        self.allow_control_box.toggled.connect(self._on_allow_control_toggled)
        grid.addWidget(self.allow_control_box, 5, 1, 1, 3)

        # ---------------------------------------------------------- 手机网页访问
        web_button = ghost("手机扫码，浏览器控制本机", "phone", self.open_web_access)
        web_button.setToolTip(
            "手机与本机连同一 Wi-Fi 后扫码，即可在浏览器里远程控制本机（无需安装 App）"
        )
        grid.addWidget(web_button, 6, 1, 1, 3)

        grid.setColumnStretch(1, 2)
        grid.setColumnStretch(3, 1)
        grid.setColumnMinimumWidth(2, 64)

        return group

    def _on_allow_control_toggled(self, checked: bool) -> None:
        """被控端「只看画面」开关：立即生效，无需断开当前连接。"""
        config.set_setting("allow_control", bool(checked))
        state = "已允许对方控制本机键鼠" if checked else "已开启只看画面（键鼠操作将被忽略）"
        self._append_log(state)
        self.status.setText(f"状态：{state}")

    def _build_connect_group(self) -> QGroupBox:
        """远程连接：与「本机信息」同一套网格语言（标签 | 值 | 标签 | 值）。"""
        group = QGroupBox("远程连接")
        grid = QGridLayout(group)
        grid.setContentsMargins(2, 2, 2, 2)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(11)

        def field_label(text: str) -> QLabel:
            label = QLabel(text)
            label.setObjectName("fieldLabel")
            return label

        self.host_input = QLineEdit()
        self.host_input.setPlaceholderText("对方 IP，例如 192.168.1.20")

        self.port_input = QLineEdit()
        self.port_input.setPlaceholderText(f"默认 {config.server_port()}")
        self.port_input.setMaximumWidth(130)
        self.port_input.setToolTip(
            "留空即使用默认端口 9527。\n"
            "只有当对方的“监听端口”不是 9527 时才需要填写（端口被占用时会自动切换）。"
        )

        self.device_input = QLineEdit()
        self.device_input.setPlaceholderText("可选，对方设备 ID")
        self.device_input.setToolTip(
            "可留空。目前仅用于在日志中标注对方身份，不参与连接校验。"
        )

        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("对方屏幕上显示的访问密码")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        # 小眼睛：明文/打码切换（与「本机信息」里的临时密码同一套图标）
        self.pwd_eye_action = self.password_input.addAction(
            eye_icon(False), QLineEdit.ActionPosition.TrailingPosition
        )
        self.pwd_eye_action.triggered.connect(self._toggle_connect_password)
        self.password_input.returnPressed.connect(self.start_connect)

        grid.addWidget(field_label("目标 IP"), 0, 0)
        grid.addWidget(self.host_input, 0, 1)
        grid.addWidget(field_label("端口"), 0, 2, Qt.AlignmentFlag.AlignRight)
        grid.addWidget(self.port_input, 0, 3)

        grid.addWidget(field_label("设备 ID"), 1, 0)
        grid.addWidget(self.device_input, 1, 1, 1, 3)

        grid.addWidget(field_label("访问密码"), 2, 0)
        grid.addWidget(self.password_input, 2, 1, 1, 3)

        self.connect_button = QPushButton("开始连接")
        self.connect_button.setObjectName("connectButton")
        self.connect_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.connect_button.clicked.connect(self.start_connect)

        self.disconnect_button = QPushButton("断开")
        self.disconnect_button.setObjectName("dangerButton")
        self.disconnect_button.setEnabled(False)
        self.disconnect_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.disconnect_button.clicked.connect(self.disconnect)

        self.show_remote_button = QPushButton("远程窗口")
        self.show_remote_button.setObjectName("ghostButton")
        self.show_remote_button.setEnabled(False)
        self.show_remote_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.show_remote_button.clicked.connect(self.show_remote_window)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(self.connect_button, 2)
        row.addWidget(self.disconnect_button, 1)
        row.addWidget(self.show_remote_button, 1)
        grid.addLayout(row, 3, 0, 1, 4)

        grid.setColumnStretch(1, 2)
        grid.setColumnStretch(3, 1)
        grid.setColumnMinimumWidth(2, 64)

        return group

    def _build_log_group(self) -> QGroupBox:
        group = QGroupBox("运行日志")
        box = QVBoxLayout(group)
        box.setContentsMargins(4, 6, 4, 4)
        box.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.log_hint = QLabel(f"最多保留 {LOG_MAX_LINES} 条，超过自动丢弃最早的一条")
        self.log_hint.setObjectName("hintLabel")

        export_btn = QPushButton("导出日志")
        export_btn.setObjectName("ghostButton")
        export_btn.setIcon(glyph("log", 15))
        export_btn.setIconSize(QSize(15, 15))
        export_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        export_btn.setToolTip("把当前日志保存为 txt 文件")
        export_btn.clicked.connect(self.export_log)

        clear_btn = QPushButton("清空")
        clear_btn.setObjectName("ghostButton")
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_btn.clicked.connect(self.clear_log)

        row.addWidget(self.log_hint, 1)
        row.addWidget(export_btn)
        row.addWidget(clear_btn)
        box.addLayout(row)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setObjectName("logView")
        self.log_view.setMinimumHeight(200)
        box.addWidget(self.log_view, 1)
        return group

    def clear_log(self) -> None:
        """清空界面上的运行日志（不影响 logs/app.log 文件）。"""
        if hasattr(self, "log_view"):
            self.log_view.clear()

    def export_log(self) -> None:
        """把当前运行日志导出为 txt，默认存到 logs 目录。"""
        if not hasattr(self, "log_view"):
            return
        default_name = f"lanremote-log-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
        default_path = os.path.join(config.LOG_DIR, default_name)
        path, _ = QFileDialog.getSaveFileName(
            self, "导出运行日志", default_path, "文本文件 (*.txt);;所有文件 (*)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fp:
                fp.write(self.log_view.toPlainText())
            self._append_log(f"日志已导出：{path}")
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", f"无法写入文件：{exc}")

    def _build_tools_group(self) -> QGroupBox:
        """远程工具：聊天 / 文件传输入口（远程窗口工具栏里也有同样的按钮）。"""
        group = QGroupBox("远程工具")
        box = QVBoxLayout(group)
        box.setContentsMargins(4, 6, 4, 4)
        box.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(8)

        self.chat_button = QPushButton("远程聊天")
        self.chat_button.setObjectName("ghostButton")
        self.chat_button.setIcon(glyph("chat", 15))
        self.chat_button.setIconSize(QSize(15, 15))
        self.chat_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chat_button.setToolTip("与对端在线聊天；收到新消息这里会出现提示")
        self.chat_button.clicked.connect(self.open_chat)

        self.files_button = QPushButton("文件传输")
        self.files_button.setObjectName("ghostButton")
        self.files_button.setIcon(glyph("folder", 15))
        self.files_button.setIconSize(QSize(15, 15))
        self.files_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.files_button.setToolTip("双向传文件（支持拖拽），接收到的文件存到 downloads 目录")
        self.files_button.clicked.connect(self.open_files)

        row.addWidget(self.chat_button, 1)
        row.addWidget(self.files_button, 1)
        box.addLayout(row)

        hint = QLabel("聊天与文件复用同一条连接，无需额外端口或额外授权。")
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        box.addWidget(hint)
        return group

    def _build_history_group(self) -> QGroupBox:
        group = QGroupBox("历史设备")
        box = QVBoxLayout(group)
        box.setContentsMargins(4, 6, 4, 4)
        box.setSpacing(8)

        # 搜索：按名称 / 备注 / IP / 设备 ID 实时过滤
        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self.history_search = QLineEdit()
        self.history_search.setPlaceholderText("搜索设备（名称 / 备注 / IP / 设备 ID）")
        self.history_search.setClearButtonEnabled(True)
        search_row.addWidget(self.history_search, 1)
        box.addLayout(search_row)

        self.history_list = QListWidget()
        self.history_list.setObjectName("historyList")
        self.history_list.setAlternatingRowColors(True)
        self.history_list.setMinimumHeight(180)
        self.history_list.setToolTip("双击填入连接信息；右键可快速连接、收藏、加备注或删除")
        self.history_list.itemDoubleClicked.connect(self._on_history_activated)
        self.history_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.history_list.customContextMenuRequested.connect(self._show_history_menu)
        box.addWidget(self.history_list, 1)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.history_hint = QLabel("暂无记录")
        self.history_hint.setObjectName("hintLabel")

        connect_btn = QPushButton("连接")
        connect_btn.setObjectName("connectButton")
        connect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        connect_btn.clicked.connect(self.connect_selected_history)
        delete_btn = QPushButton("删除")
        delete_btn.setObjectName("ghostButton")
        delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        delete_btn.clicked.connect(self.delete_selected_history)
        clear_btn = QPushButton("清空")
        clear_btn.setObjectName("ghostButton")
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_btn.clicked.connect(self.clear_history)

        row.addWidget(self.history_hint, 1)
        row.addWidget(connect_btn)
        row.addWidget(delete_btn)
        row.addWidget(clear_btn)
        box.addLayout(row)

        self.history_search.textChanged.connect(self._reload_history)
        return group

    @staticmethod
    def _wrap(layout) -> QWidget:
        holder = QWidget()
        holder.setObjectName("rowHolder")
        layout.setContentsMargins(0, 0, 0, 0)
        holder.setLayout(layout)
        return holder

    def _load_style(self) -> None:
        """
        应用全局主题。

        样式挂在 QApplication 上而不是本窗口：这样设置 / 关于 / 聊天 / 远程窗口
        等所有窗口一并生效，切换深浅色时也不需要逐个窗口刷新。
        """
        theme.apply()
        theme.register_refresh(self._refresh_icons)

    def _refresh_icons(self) -> None:
        """主题变化后重绘程序化图标（它们的颜色取自主题，不会自动更新）。"""
        for button, name in (
            (getattr(self, "settings_button", None), "settings"),
            (getattr(self, "about_button", None), "info"),
        ):
            if button is not None:
                button.setIcon(glyph(name, 16))
        eye = getattr(self, "eye_button", None)
        if eye is not None:
            eye.setIcon(eye_icon(bool(getattr(self, "_pwd_visible", False))))

    def _wire_signals(self) -> None:
        self.bridge.status_changed.connect(self._on_status)
        self.bridge.connected_changed.connect(self._on_connected)
        self.bridge.connect_failed.connect(self._on_primary_connect_failed)
        self.bridge.chat_received.connect(self._on_chat)
        self.bridge.screen_ready.connect(self._on_screen)
        self.bridge.clipboard_received.connect(self._on_clipboard_received)
        self.bridge.quality_state_changed.connect(self._on_quality_state)
        # 文件事件统一走 MainWindow：既更新面板，也按需弹托盘通知
        self.bridge.file_event.connect(self._on_file_event)

    # ================================================================== 历史设备
    @staticmethod
    def _history_text(record: dict) -> str:
        name = record.get("name") or record.get("device_id") or "未知设备"
        ip = record.get("ip") or ""
        port = record.get("port") or 0
        note = str(record.get("note") or "").strip()
        favorite = bool(int(record.get("favorite") or 0))
        address = f"{ip}:{port}" if ip and port else (ip or "—")
        parts = [f"★ {name}" if favorite else str(name), address]
        if note:
            parts.append(f"「{note}」")
        last = record.get("last_time") or ""
        if last:
            parts.append(last)
        return "    ".join(parts)

    def _reload_history(self) -> None:
        """从数据库重读历史设备并刷新列表（应用搜索框的关键词过滤）。"""
        if not hasattr(self, "history_list"):
            return

        keep_id = None
        current = self.history_list.currentItem()
        if current is not None:
            data = current.data(Qt.ItemDataRole.UserRole)
            if isinstance(data, dict):
                keep_id = data.get("id")

        self.history_list.clear()
        try:
            records = self.db.get_devices(limit=HISTORY_LIMIT)
        except Exception:
            logger.exception("读取历史设备失败")
            records = []

        keyword = ""
        search = getattr(self, "history_search", None)
        if search is not None:
            keyword = search.text().strip().lower()
            if keyword:
                records = [
                    record
                    for record in records
                    if keyword in self._history_text(record).lower()
                    or keyword in str(record.get("device_id") or "").lower()
                ]

        for record in records:
            item = QListWidgetItem(self._history_text(record))
            item.setData(Qt.ItemDataRole.UserRole, record)
            item.setToolTip(
                f"设备 ID：{record.get('device_id') or '—'}\n"
                f"地址：{record.get('ip') or '—'}:{record.get('port') or '—'}\n"
                f"最近连接：{record.get('last_time') or '—'}"
            )
            self.history_list.addItem(item)
            if keep_id is not None and record.get("id") == keep_id:
                self.history_list.setCurrentItem(item)

        if keyword:
            self.history_hint.setText(f"匹配 {len(records)} 台（共 {self.db.count()} 台）")
        else:
            self.history_hint.setText(
                f"共 {len(records)} 台" if records else "暂无记录，连接成功后会自动出现"
            )

    def toggle_favorite(self) -> None:
        """收藏 / 取消收藏选中的历史设备（收藏的排在列表最前）。"""
        record = self._selected_record()
        if record is None:
            QMessageBox.information(self, "提示", "请先在列表中选择一台设备")
            return
        favorite = not bool(int(record.get("favorite") or 0))
        self.db.set_favorite(int(record.get("id") or 0), favorite)
        self._reload_history()
        name = record.get("name") or record.get("device_id") or "该设备"
        self._append_log(f"已{'收藏' if favorite else '取消收藏'}设备：{name}")

    def _selected_record(self) -> dict | None:
        item = self.history_list.currentItem()
        if item is None:
            return None
        data = item.data(Qt.ItemDataRole.UserRole)
        return data if isinstance(data, dict) else None

    def _fill_from_record(self, record: dict) -> None:
        """把历史记录回填到连接表单。"""
        ip = str(record.get("ip") or "")
        device = str(record.get("device_id") or "")
        port = record.get("port") or ""
        if ip:
            self.host_input.setText(ip)
        if device and device != ip:
            self.device_input.setText(device)
        if port:
            self.port_input.setText(str(port))

    def _on_history_activated(self, item) -> None:
        """双击历史项：只回填信息，不主动发起连接（避免误弹对方授权框）。"""
        record = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(record, dict):
            return
        self._fill_from_record(record)
        self.status.setText("状态：已填入历史设备信息，填好密码即可连接")
        self.password_input.setFocus()

    def connect_selected_history(self) -> None:
        record = self._selected_record()
        if record is None:
            QMessageBox.information(self, "提示", "请先在列表中选择一台设备")
            return
        self._fill_from_record(record)
        self.start_connect()

    def delete_selected_history(self) -> None:
        record = self._selected_record()
        if record is None:
            QMessageBox.information(self, "提示", "请先在列表中选择一台设备")
            return
        name = record.get("name") or record.get("device_id") or "该设备"
        answer = QMessageBox.question(self, "删除记录", f"确定从历史记录中删除「{name}」吗？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.db.delete_device(int(record.get("id") or 0))
        self._reload_history()
        self._append_log(f"已删除历史设备：{name}")

    def clear_history(self) -> None:
        if self.db.count() == 0:
            QMessageBox.information(self, "提示", "当前没有历史记录")
            return
        answer = QMessageBox.question(
            self, "清空记录", "确定清空全部历史设备记录吗？此操作不可撤销。"
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.db.clear_devices()
        self._reload_history()
        self._append_log("已清空历史设备记录")

    def edit_selected_note(self) -> None:
        """给历史设备加一个易记的备注。"""
        record = self._selected_record()
        if record is None:
            QMessageBox.information(self, "提示", "请先在列表中选择一台设备")
            return
        from PyQt6.QtWidgets import QInputDialog

        text, ok = QInputDialog.getText(
            self, "编辑备注", "给这台设备起个名字：", QLineEdit.EchoMode.Normal,
            str(record.get("note") or ""),
        )
        if not ok:
            return
        self.db.set_note(int(record.get("id") or 0), text.strip())
        self._reload_history()
        self._append_log("已更新设备备注")

    def _show_history_menu(self, position) -> None:
        item = self.history_list.itemAt(position)
        if item is None:
            return
        self.history_list.setCurrentItem(item)

        menu = QMenu(self)
        connect_action = menu.addAction("连接此设备")
        connect_action.triggered.connect(self.connect_selected_history)
        record = item.data(Qt.ItemDataRole.UserRole)
        favorite = bool(int((record or {}).get("favorite") or 0))
        favorite_action = menu.addAction("取消收藏" if favorite else "收藏置顶")
        favorite_action.triggered.connect(self.toggle_favorite)
        note_action = menu.addAction("编辑备注…")
        note_action.triggered.connect(self.edit_selected_note)
        menu.addSeparator()
        delete_action = menu.addAction("删除记录")
        delete_action.triggered.connect(self.delete_selected_history)
        menu.exec(self.history_list.mapToGlobal(position))

    def _remember_device(self, host: str, port: int) -> None:
        """连接成功后写入历史设备（按设备 ID 去重，保留已有备注）。"""
        device = self.device_input.text().strip()
        key = device or host
        if not key:
            return
        try:
            self.db.upsert_device(
                device_id=key,
                name=device or host,
                ip=host,
                port=int(port or 0),
                last_time=now_text(),
            )
        except Exception:
            logger.exception("写入历史设备失败")
            return
        self._reload_history()

    def _on_clipboard_received(self, text: str) -> None:
        self._append_log(f"已接收远端剪贴板（{len(text)} 字符）")

    def _on_quality_state(self, profile: str, fps: int, quality: int, max_width: int) -> None:
        label = config.QUALITY_PROFILES.get(profile, ("未知",))[0]
        self._append_log(f"远端画质档位：{label}（{fps} FPS / 质量 {quality}）")

    # ================================================================== 密码 / 信息
    def _restore_inputs(self) -> None:
        last_ip = config.get_setting("last_host", "")
        if last_ip:
            self.host_input.setText(str(last_ip))
        last_port = config.get_setting("last_port", "")
        if last_port:
            self.port_input.setText(str(last_port))

    def _tick(self) -> None:
        # 到期自动换新密码，保证复制出去的密码始终可用；界面不显示倒计时
        if self.credentials.is_expired():
            self.credentials.refresh()
            self._append_log("临时密码已到期，已自动刷新")
            self._refresh_password_text()

        if self.server is not None:
            port = self.server.actual_port
            self.port_label.setText(str(port) if port else "未启动")
            self.client_label.setText(str(self.server.client_count()))
            self._sync_local_screen_controls()
        else:
            self.port_label.setText("未启动")
            self.client_label.setText("0")

        if self.discovery is not None:
            self.found_label.setText(f"{self.discovery.known_count()} 台")
        else:
            self.found_label.setText("未启用")

    # ================================================================== 画面参数
    def _sync_local_screen_controls(self) -> None:
        """把服务端当前的画质 / 显示器状态同步到主界面控件。"""
        if self.server is None:
            return

        # 画质档位可能被控制端远程改过，以配置为准
        profile = config.quality_profile()
        position = self.quality_combo.findData(profile)
        if position >= 0 and position != self.quality_combo.currentIndex():
            self._applying_quality = True
            self.quality_combo.blockSignals(True)
            try:
                self.quality_combo.setCurrentIndex(position)
            finally:
                self.quality_combo.blockSignals(False)
                self._applying_quality = False

        # 显示器列表只在布局变化时重建
        monitors = self.server.screen.monitors()
        if not monitors:
            return
        signature = tuple(
            (item.get("index"), item.get("width"), item.get("height")) for item in monitors
        )
        if signature == self._monitor_signature:
            return
        self._monitor_signature = signature

        self.monitor_combo.blockSignals(True)
        try:
            self.monitor_combo.clear()
            for item in monitors:
                index = int(item.get("index", 0))
                width = int(item.get("width", 0))
                height = int(item.get("height", 0))
                label = (
                    f"全部屏幕 {width}×{height}"
                    if index == 0
                    else f"显示器 {index}  {width}×{height}"
                )
                self.monitor_combo.addItem(label, index)
            position = self.monitor_combo.findData(self.server.screen.monitor_index)
            if position >= 0:
                self.monitor_combo.setCurrentIndex(position)
        finally:
            self.monitor_combo.blockSignals(False)

    def _on_local_quality_changed(self, _index: int) -> None:
        """本机（被控端）切换画质档位。"""
        if self._applying_quality:
            return
        profile = self.quality_combo.currentData()
        if not isinstance(profile, str) or not config.apply_quality_profile(profile):
            return

        _, fps, quality, max_width = config.QUALITY_PROFILES[profile]
        if self.server is not None:
            self.server.apply_screen_settings(fps=fps, quality=quality, max_width=max_width)
        label = config.QUALITY_PROFILES[profile][0]
        self._append_log(f"画质档位已切换：{label}（{fps} FPS / 质量 {quality} / 宽 {max_width}）")
        self.status.setText("状态：画质档位已更新")

    def _on_local_monitor_changed(self, _index: int) -> None:
        """本机（被控端）切换对外共享的显示器。"""
        index = self.monitor_combo.currentData()
        if index is None or self.server is None:
            return
        index = int(index)
        if self.server.screen.set_monitor(index):
            config.set_setting("screen_monitor", index)
            self._append_log(f"共享屏幕已切换为显示器 {index}")

    def _apply_remote_quality(self, profile: str, fps: int, quality: int, max_width: int) -> None:
        """控制端远程改了画质：在主线程刷新界面（本方法由信号触发）。"""
        label = config.QUALITY_PROFILES.get(profile, ("未知",))[0]
        self._append_log(
            f"控制端把画质档位改为：{label}（{fps} FPS / 质量 {quality} / 宽 {max_width}）",
            "warning",
        )
        self.status.setText(f"状态：画质已被远端调整为 {label}")

    # ================================================================== 密码显示
    def _toggle_connect_password(self) -> None:
        """远程连接页访问密码的明文/打码切换。"""
        hidden = self.password_input.echoMode() == QLineEdit.EchoMode.Password
        self.password_input.setEchoMode(
            QLineEdit.EchoMode.Normal if hidden else QLineEdit.EchoMode.Password
        )
        opened = not hidden
        self.pwd_eye_action.setIcon(eye_icon(opened))
        self.pwd_eye_action.setToolTip("隐藏密码" if opened else "显示密码")

    def _refresh_password_mode(self) -> None:
        """设置里切换固定密码后，同步本机信息区的文案与按钮状态。"""
        fixed = bool(getattr(self.credentials, "is_fixed", False))
        if getattr(self, "password_field_label", None) is not None:
            self.password_field_label.setText("固定密码" if fixed else "临时密码")
        if getattr(self, "password_refresh_button", None) is not None:
            self.password_refresh_button.setEnabled(not fixed)
            self.password_refresh_button.setToolTip(
                "固定密码模式下不会自动更换；取消固定后可手动刷新" if fixed
                else "立即刷新临时密码（旧密码随即失效）"
            )
        self._refresh_password_text()

    def _refresh_password_text(self) -> None:
        """按当前可见状态刷新密码文本：默认打码，点眼睛后明文显示几秒。"""
        password = self.credentials.password
        if self._pwd_visible:
            self.pwd_label.setText(password)
        else:
            self.pwd_label.setText(PASSWORD_MASK_CHAR * max(1, len(password)))

    def _set_password_visible(self, visible: bool) -> None:
        self._pwd_visible = bool(visible)
        self._refresh_password_text()
        self.eye_button.setIcon(eye_icon(self._pwd_visible))
        self.eye_button.setToolTip("隐藏临时密码" if visible else "显示临时密码")
        if visible:
            self._mask_timer.start(PASSWORD_VISIBLE_SECONDS * 1000)
        else:
            self._mask_timer.stop()

    def copy_id(self) -> None:
        self._copy(self.device_id, "设备 ID")

    def copy_password(self) -> None:
        self._copy(self.credentials.password, "临时密码")

    def _copy(self, text: str, what: str) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            QMessageBox.warning(self, "提示", "剪贴板不可用")
            return
        clipboard.setText(text)
        self.status.setText(f"状态：{what}已复制")

    def refresh_password(self) -> None:
        if bool(getattr(self.credentials, "is_fixed", False)):
            self.status.setText("状态：当前使用固定密码，不会自动更换")
            self._append_log("固定密码模式下无需刷新", "warning")
            return
        self.credentials.refresh()
        self._refresh_password_text()
        self._append_log("已刷新临时密码")
        self.status.setText("状态：密码已刷新（立即生效）")

    def open_settings(self) -> None:
        """打开设置对话框；保存后把能立即生效的参数应用到运行中的服务。"""
        if open_settings(self, credentials=self.credentials, server=self.server):
            self._refresh_password_mode()
            self._tick()
            self._refresh_password_text()
            self._append_log("设置已保存")
            self.status.setText("状态：设置已保存")

    def open_about(self) -> None:
        open_about(self)

    def open_web_access(self) -> None:
        """展示手机网页访问的链接与二维码。"""
        if self.web_access is None or not self.web_access.running:
            QMessageBox.information(
                self, "手机访问", "手机网页访问服务未启动。\n请确认服务端已正常运行后重试。"
            )
            return
        ip = self.ip or "127.0.0.1"
        url = f"http://{ip}:{self.web_access.actual_port}/"
        self._append_log(f"已展示手机访问链接：{url}")
        WebAccessDialog(self.device_id, ip, url, self).exec()

    # ================================================================== 远程工具
    def open_chat(self) -> None:
        """打开（或前置）当前活动会话的远程聊天窗口。"""
        session = self.sessions.get(self._active_session_key) if self._active_session_key else None
        self._show_tool_window((session or {}).get("chat") or self.chat)

    def open_files(self) -> None:
        """打开（或前置）当前活动会话的文件传输窗口。"""
        session = self.sessions.get(self._active_session_key) if self._active_session_key else None
        self._show_tool_window((session or {}).get("files") or self.files)

    @staticmethod
    def _show_tool_window(window) -> None:
        if window is None:
            return
        if window.isMinimized():
            window.showNormal()
        else:
            window.show()
        window.raise_()
        window.activateWindow()
        window.setFocus()

    # ================================================================== 连接
    @staticmethod
    def _validate_connect_form(host: str, device: str, password: str) -> str:
        """
        校验连接表单，返回错误描述；返回空字符串表示通过。

        提前拦下明显填错的内容，避免点「开始连接」后静默失败、用户不知道哪里错了。
        """
        if not host and not device:
            return (
                "请填写「目标 IP + 访问密码」直接连接；\n"
                "或者只填「目标设备 ID」，由对方点击授权后接入。"
            )

        if device and not device.isdigit():
            return f"设备 ID 应为纯数字（当前输入：{device}），请核对后重新填写。"

        if host:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                if not _HOSTNAME_RE.fullmatch(host):
                    return f"目标 IP / 主机名格式不正确：{host}"

        if not password:
            if host and not device:
                return (
                    "只填 IP 时需要访问密码；\n"
                    "若想免密码，请改填对方设备 ID，由对方点击授权。"
                )
        elif len(password) < 4:
            return "访问密码至少 4 位，请核对是否输入完整。"
        return ""

    def start_connect(self) -> None:
        host = self.host_input.text().strip()
        password = self.password_input.text().strip()
        device = self.device_input.text().strip()
        port_text = self.port_input.text().strip()

        # 两种接入方式：
        #   填了访问密码        -> 密码直连
        #   只填设备 ID（无密码）-> 授权模式，对方点击同意后才能接入
        mode = "approval" if not password else "password"

        error = self._validate_connect_form(host, device, password)
        if error:
            QMessageBox.warning(self, "无法开始连接", error)
            return

        if not host:
            # 只填了设备 ID：通过局域网设备发现解析出对方 IP 和端口
            self.status.setText("状态：正在查找设备…")
            self._set_status_chip("◐ 查找设备", "pending")
            found_ip, found_port = self._resolve_device(device)
            if not found_ip:
                QMessageBox.warning(
                    self,
                    "未找到设备",
                    f"局域网内暂时没有发现设备 {device}。\n\n"
                    "请确认对方已打开 LAN Remote、与本机在同一局域网，然后重试。",
                )
                self.status.setText("状态：未找到设备")
                self._set_status_chip("● 未找到设备", "offline")
                return
            host = found_ip
            if found_port and not port_text:
                port_text = str(found_port)
                self.port_input.setText(port_text)
            self.host_input.setText(host)

        try:
            port = int(port_text or config.server_port())
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            QMessageBox.warning(self, "提示", "端口必须是 1 ~ 65535 之间的整数")
            return

        config.set_setting("last_host", host)
        config.set_setting("last_port", port)
        # 多设备并发：每个目标一个独立会话（独立桥接器 + 独立远程窗口）
        key = f"{host}:{port}"
        self._active_session_key = key
        self._active_host = host
        self._active_port = port
        # 本次是用户手动发起的连接：连上才打开远程窗口，失败弹分类提示
        self._manual_connect = True

        session = self._ensure_session(key, host, port)
        session["bridge"].set_port(port)

        self.connect_button.setEnabled(False)
        self.disconnect_button.setEnabled(True)
        self.show_remote_button.setEnabled(True)
        if mode == "approval":
            self.status.setText(f"状态：已向 {host} 发送授权请求，等待对方确认…")
            self._set_status_chip("◐ 等待授权", "pending")
        else:
            self.status.setText("状态：连接中…")
            self._set_status_chip("◐ 连接中", "pending")

        if not session["bridge"].start_session(host, device, password, mode):
            self._manual_connect = False
            self.status.setText("状态：客户端初始化失败")
            self._set_status_chip("● 连接失败", "offline")
            self.connect_button.setEnabled(True)
            self.disconnect_button.setEnabled(False)

    def _resolve_device(self, device_id: str) -> tuple[str | None, int | None]:
        """按设备 ID 在局域网内查找对方，返回 (ip, port)。"""
        if self.discovery is None:
            return None, None
        return self.discovery.resolve(device_id, wait=config.DISCOVERY_RESOLVE_WAIT)

    def disconnect(self) -> None:
        """断开当前活动会话（远程窗口保持打开，可再次连接）。"""
        self._manual_connect = False
        key = self._active_session_key
        session = self.sessions.get(key) if key else None
        bridge = (session or {}).get("bridge") or self.bridge
        bridge.disconnect()
        self.status.setText("状态：已断开")
        self._set_status_chip("● 未连接", "offline")
        self.connect_button.setEnabled(True)
        self.disconnect_button.setEnabled(False)
        self._append_log(f"已断开 {key or '当前连接'}")

    def _ensure_remote(self) -> RemoteWindow:
        """创建（或复用）远程窗口，并接好它的工具栏回调。"""
        if self.remote is None:
            self.remote = RemoteWindow(self.bridge)
            self.remote.closed.connect(self._on_remote_closed)
            # 工具栏上的「聊天 / 文件」交由主窗口统一管理窗口生命周期
            self.remote.chat_requested.connect(self.open_chat)
            self.remote.files_requested.connect(self.open_files)
        return self.remote

    def show_remote_window(self) -> None:
        session = self.sessions.get(self._active_session_key) if self._active_session_key else None
        remote = (session or {}).get("remote") or self._ensure_remote()
        remote.show()
        remote.raise_()
        remote.activateWindow()

    # ============================================================== 多会话管理
    def _ensure_session(self, key: str, host: str, port: int) -> dict:
        """
        取回（或创建）一个远程会话：独立桥接器 + 独立远程窗口。

        第一个会话复用主桥接器与聊天 / 文件窗口（既有信号接线保持不变），
        从第二个会话起各自创建一套，实现多台设备同时远程。
        """
        if key in self.sessions:
            return self.sessions[key]

        if not self.sessions:
            bridge = self.bridge
            remote = self._ensure_remote()
            chat, files = self.chat, self.files
            self._primary_session_key = key
        else:
            bridge = ClientBridge(port=port, parent=self)
            remote = RemoteWindow(bridge)
            remote.closed.connect(self._on_remote_closed)
            remote.chat_requested.connect(lambda k=key: self._open_chat_for(k))
            remote.files_requested.connect(lambda k=key: self._open_files_for(k))
            chat = ChatWindow(bridge, title=f"远程聊天 · {host}")
            files = FileWindow(bridge)

        remote.setWindowTitle(f"LAN Remote · {host}:{port}")
        session = {
            "key": key,
            "bridge": bridge,
            "remote": remote,
            "chat": chat,
            "files": files,
            "host": host,
            "port": port,
        }
        self.sessions[key] = session

        if bridge is not self.bridge:
            # 主桥接器的信号已在 _wire_signals 接好，这里只接额外会话的
            bridge.status_changed.connect(
                lambda text, level, k=key: self._on_session_status(k, text, level)
            )
            bridge.connected_changed.connect(
                lambda connected, k=key: self._on_session_connected(k, connected)
            )
            bridge.connect_failed.connect(
                lambda kind, reason, k=key: self._on_session_connect_failed(k, kind, reason)
            )
            bridge.chat_received.connect(
                lambda text, sender, k=key: self._on_session_chat(k, text, sender)
            )
            bridge.file_event.connect(
                lambda event, payload, k=key: self._on_session_file_event(k, event, payload)
            )
            bridge.screen_ready.connect(
                lambda image, width, height, fps, delay, k=key: self._on_session_screen(
                    k, image, width, height, fps, delay
                )
            )
        return session

    def _connect_failure_message(self, kind: str, reason: str, host: str) -> tuple[str, str]:
        """把连接失败分类成用户能看懂的弹窗文案。"""
        target = host or "目标设备"
        if kind == "auth":
            return "接入被拒绝", (
                "设备 ID 或访问密码不正确。\n\n"
                f"对方返回：{reason or '密码错误'}\n\n"
                "请核对临时密码（字母+数字组合）后重新输入。"
            )
        if kind == "offline":
            return "被控端未在线", (
                f"无法连接 {target}。\n\n"
                "可能原因：\n"
                "· 对方尚未打开 LAN Remote（被控端未在线）\n"
                "· IP / 端口填写有误\n"
                "· 双方不在同一局域网"
            )
        if kind == "timeout":
            return "连接超时", (
                f"{target} 长时间没有响应。\n\n"
                "请确认对方程序已打开、网络可达后重试。"
            )
        return "连接失败", f"未能连接到 {target}。\n\n{reason or '未知原因'}"

    def _on_session_connected(self, key: str | None, connected: bool) -> None:
        """某个会话的连接状态变化：只有活动会话驱动主窗口按钮与弹窗。"""
        if key != self._active_session_key:
            return
        if connected:
            self._handle_connect_success(key)
        else:
            self._handle_disconnect(key)

    def _handle_connect_success(self, key: str | None) -> None:
        was_manual = self._manual_connect
        self._manual_connect = False
        session = self.sessions.get(key) if key else None
        host = (session or {}).get("host") or self._active_host
        port = (session or {}).get("port") or self._active_port

        self.status.setText("状态：已连接")
        self._set_status_chip("● 已连接", "online")
        self.connect_button.setEnabled(False)
        self.disconnect_button.setEnabled(True)
        self.show_remote_button.setEnabled(True)
        self._append_log(f"连接已建立：{host}:{port}")
        try:
            self.files.refresh_hint()
        except Exception:
            logger.debug("刷新文件面板提示失败", exc_info=True)
        if host:
            # 只有真正连上才写入历史设备，连不上的目标不留记录
            self._remember_device(host, port)
        if was_manual:
            # 手动发起的连接：成功后才打开远程控制窗口
            remote = (session or {}).get("remote")
            if remote is not None:
                remote.show()
                remote.raise_()
                remote.activateWindow()
            self._notify_tray("远程连接已建立", f"正在控制 {host}:{port}")

    def _handle_disconnect(self, key: str | None) -> None:
        self.status.setText("状态：未连接")
        self._set_status_chip("● 未连接", "offline")
        self.connect_button.setEnabled(True)
        self.disconnect_button.setEnabled(False)
        self._append_log("连接已断开", "warning")
        try:
            self.files.refresh_hint()
        except Exception:
            logger.debug("刷新文件面板提示失败", exc_info=True)

    def _on_session_connect_failed(self, key: str | None, kind: str, reason: str) -> None:
        """连接失败：按「密码错 / 对方不在线 / 超时」分类弹窗。"""
        if key != self._active_session_key:
            return
        was_manual = self._manual_connect
        self._manual_connect = False
        self.connect_button.setEnabled(True)
        self.disconnect_button.setEnabled(False)
        self._set_status_chip("● 连接失败", "offline")
        session = self.sessions.get(key) if key else None
        host = (session or {}).get("host") or self._active_host
        title, text = self._connect_failure_message(kind, reason, host)
        self._append_log(f"连接失败：{title}", "warning")
        if was_manual:
            QMessageBox.warning(self, title, text)

    def _on_primary_connect_failed(self, kind: str, reason: str) -> None:
        self._on_session_connect_failed(self._primary_session_key, kind, reason)

    def _on_session_status(self, key: str, text: str, level: str = "info") -> None:
        self._append_log(text, level)
        if key != self._active_session_key:
            return
        self.status.setText(f"状态：{text}")
        if level == "error":
            session = self.sessions.get(key) or {}
            bridge = session.get("bridge") or self.bridge
            if not bridge.connected:
                self._set_status_chip("● 连接异常", "offline")

    def _on_session_chat(self, key: str, text: str, sender: str) -> None:
        session = self.sessions.get(key) or {}
        chat = session.get("chat")
        who = sender or "对端"
        if chat is not None:
            try:
                chat.on_message(text, who)
            except Exception:
                logger.exception("写入聊天窗口失败")
        self._append_log(f"[{key}] [聊天] {who}：{text}")
        if chat is None or not chat.isVisible():
            self._notify_tray(f"来自 {who} 的聊天消息", str(text)[:80])

    def _on_session_file_event(self, key: str, event: str, payload: dict) -> None:
        session = self.sessions.get(key) or {}
        files = session.get("files")
        if files is not None:
            try:
                files.on_file_event(event, payload)
            except Exception:
                logger.exception("更新文件面板失败")
        if payload.get("direction") != "in":
            return
        name = str(payload.get("name") or "文件")
        visible = bool(files is not None and files.isVisible())
        if event == "offer" and not visible:
            self._notify_tray("收到文件", f"{name} 正在传输，点击「文件传输」查看进度")
        elif event == "done":
            if payload.get("ok"):
                path = str(payload.get("path") or "")
                self._notify_tray("文件接收完成", f"{name}\n已保存到 {path}" if path else name)
            elif not visible:
                self._notify_tray("文件接收失败", f"{name}：{payload.get('message') or '未知原因'}")

    def _on_session_screen(self, key: str, image, width: int, height: int, fps: float, delay: float) -> None:
        session = self.sessions.get(key) or {}
        remote = session.get("remote")
        if remote is not None and remote.isVisible():
            remote.on_frame(image, width, height, fps, delay)
        else:
            # 远程窗口不可见时不消费画面，避免事件队列堆积
            (session.get("bridge") or self.bridge).frame_consumed()

    def _open_chat_for(self, key: str | None) -> None:
        session = self.sessions.get(key) if key else None
        window = (session or {}).get("chat") or self.chat
        window.show()
        window.raise_()
        window.activateWindow()

    def _open_files_for(self, key: str | None) -> None:
        session = self.sessions.get(key) if key else None
        window = (session or {}).get("files") or self.files
        window.show()
        window.raise_()
        window.activateWindow()

    # ================================================================== 槽函数
    def _on_screen(self, image, width: int, height: int, fps: float, delay: float) -> None:
        if self.remote is not None and self.remote.isVisible():
            self.remote.on_frame(image, width, height, fps, delay)
        else:
            # 远程窗口不可见时不消费画面，避免事件队列堆积
            self.bridge.frame_consumed()

    def _on_status(self, text: str, level: str = "info") -> None:
        self.status.setText(f"状态：{text}")
        # 出现错误且尚未建立连接时，状态胶囊同步转为异常态
        if level == "error" and not self.bridge.connected:
            self._set_status_chip("● 连接异常", "offline")
        self._append_log(text, level)

    def _on_connected(self, connected: bool) -> None:
        # 主桥接器 = 第一个会话（复用），统一交给会话处理器
        self._on_session_connected(self._primary_session_key, connected)

    def _on_chat(self, text: str, sender: str) -> None:
        """收到远端聊天（控制端与被控端两条链路都汇到这里）。"""
        who = sender or "对端"
        # 聊天窗口始终存在（构造时就建好），所以窗口没打开也不会丢消息
        try:
            self.chat.on_message(text, who)
        except Exception:
            logger.exception("写入聊天窗口失败")
        self._append_log(f"[聊天] {who}：{text}")
        if not self.chat.isVisible():
            self.status.setText(f"状态：收到 {who} 的消息（点「远程聊天」查看）")
            # 聊天窗口没开着：右下角弹托盘提醒，避免消息被无声吞掉
            self._notify_tray(f"来自 {who} 的聊天消息", str(text)[:80])

    def _on_file_event(self, event: str, payload: dict) -> None:
        """文件传输事件：更新面板 + 在被控/控制端收到文件时弹托盘提醒。"""
        try:
            self.files.on_file_event(event, payload)
        except Exception:
            logger.exception("更新文件面板失败")

        if payload.get("direction") != "in":
            return
        name = str(payload.get("name") or "文件")
        visible = self.files.isVisible()
        if event == "offer" and not visible:
            self._notify_tray("收到文件", f"{name} 正在传输，点击「文件传输」查看进度")
        elif event == "done":
            if payload.get("ok"):
                path = str(payload.get("path") or "")
                self._notify_tray(
                    "文件接收完成",
                    f"{name}\n已保存到 {path}" if path else name,
                )
            elif not visible:
                self._notify_tray("文件接收失败", f"{name}：{payload.get('message') or '未知原因'}")

    def _notify_tray(self, title: str, message: str) -> None:
        """右下角托盘气泡提醒（系统级，程序在后台也能看到）。"""
        if self.tray is None:
            return
        try:
            self.tray.notify(title, message)
        except Exception:
            logger.debug("托盘通知发送失败", exc_info=True)

    def _on_client_connected(self, device_id: str, address: str) -> None:
        """有设备接入本机（认证成功）：托盘提醒 + 提示音。"""
        self._append_log(f"设备接入：{device_id}（{address}）")
        self._notify_tray("有设备接入", f"{device_id}（{address}）已开始远程控制本机")
        try:
            QGuiApplication.beep()
        except Exception:
            logger.debug("播放提示音失败", exc_info=True)

    def _on_client_disconnected(self, device_id: str, address: str) -> None:
        """控制端断开（点击断开 / 关闭程序 / 掉线）：被控端托盘提醒 + 提示音。"""
        who = device_id or address or "对方"
        self._append_log(f"控制端已断开连接：{who}（{address}）", "warning")
        self._notify_tray("控制端已断开连接", f"{who} 已断开对本机的远程控制")
        try:
            QGuiApplication.beep()
        except Exception:
            logger.debug("播放提示音失败", exc_info=True)

    def _on_authorize_request(self, info: dict) -> None:
        """收到局域网内其它设备的远程访问授权请求（Qt 主线程槽函数）。"""
        from ui.permission_dialog import PermissionDialog

        peer = f"{info.get('device_id', '未知设备')}（{info.get('address', '')}）"
        approved = PermissionDialog.ask(peer, self)

        respond = info.get("respond")
        if callable(respond):
            try:
                respond(approved)
            except Exception:
                logger.exception("回传授权结果失败")

        self._append_log(
            f"{'已同意' if approved else '已拒绝'} {peer} 的远程访问请求",
            "info" if approved else "warning",
        )

    def _on_remote_closed(self) -> None:
        self._append_log("远程窗口已关闭（连接保持）")

    def _append_log(self, text: str, level: str = "info") -> None:
        if not hasattr(self, "log_view"):
            return
        prefix = {"error": "✖", "warning": "⚠"}.get(level, "•")
        self.log_view.append(f"{prefix} {text}")
        document = self.log_view.document()
        if document is not None and document.blockCount() > LOG_MAX_LINES:
            cursor = self.log_view.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            cursor.select(cursor.SelectionType.BlockUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar()

    # ================================================================== 关闭
    def closeEvent(self, event) -> None:  # noqa: N802
        if self.force_quit or self.tray is None:
            self._cleanup()
            event.accept()
            return

        event.ignore()
        self.hide()
        if getattr(self, "_tray_hint_shown", False) is False:
            self._tray_hint_shown = True
            if self.tray is not None:
                self.tray.notify("LAN Remote 仍在后台运行", "点击托盘图标可以重新打开主窗口")

    def _cleanup(self) -> None:
        if self._tick_timer.isActive():
            self._tick_timer.stop()
        if self._mask_timer.isActive():
            self._mask_timer.stop()
        # 工具窗口先关，避免它们继续响应桥接器信号
        # （每个会话都有自己的聊天 / 文件窗口，主窗口自带的两个也在会话表里）
        for session in self.sessions.values():
            for name in ("chat", "files"):
                window = session.get(name)
                if window is None:
                    continue
                try:
                    window.close()
                except Exception:
                    pass
        # 先停额外会话，再停主桥接器
        for session in self.sessions.values():
            bridge = session.get("bridge")
            if bridge is None or bridge is self.bridge:
                continue
            try:
                bridge.shutdown()
            except Exception:
                logger.exception("关闭会话客户端失败")
        try:
            self.bridge.shutdown()
        except Exception:
            logger.exception("关闭客户端失败")
        if self.web_access is not None:
            try:
                self.web_access.stop()
            except Exception:
                logger.exception("停止手机网页访问失败")
        if self.server is not None:
            try:
                self.server.stop()
            except Exception:
                logger.exception("关闭服务端失败")
        # 只关闭自己创建的数据库连接（自检传入的临时库由调用方负责）
        if getattr(self, "_owns_db", False):
            try:
                self.db.close()
            except Exception:
                pass
