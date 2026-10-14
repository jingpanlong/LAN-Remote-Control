"""
远程桌面窗口（控制端）

关键设计：
    - 画面更新走 Qt 信号，绝不从后台线程直接操作控件
    - 鼠标坐标按「实际绘制区域」精确反算到远端分辨率，解决黑边偏移
    - 鼠标移动做节流合并，避免高频事件把网络打满
    - 画面显示在 QScrollArea 中，因此支持「适应窗口 / 原始尺寸 / 拉伸铺满」三种模式
    - 鼠标与滚轮事件统一走 image_label 的事件过滤器 ——
      这样拿到的坐标天然是图像控件本地坐标，坐标反算不受工具栏 / 滚动条影响

高级功能：
    - 实时状态栏：分辨率、帧率、延迟、带宽、当前画质
    - 画质档位远程切换（请求被控端调整帧率 / 质量 / 分辨率上限）
    - 被控端多显示器切换
    - 画面暂停 / 继续（暂停时被控端停下采集，节省对方 CPU 与带宽）
    - 剪贴板双向同步（发送本机剪贴板 / 获取远端剪贴板）
    - 特殊按键面板（Alt+Tab、任务管理器、Win 系列等组合键）
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QEvent, QPointF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QKeyEvent, QPixmap
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import config
from ui import theme
from ui.icons import app_icon, glyph
from ui.widgets import SegmentedControl

logger = logging.getLogger("LAN_REMOTE")

# 鼠标移动发送频率上限（Hz），远高于此的移动会被合并
MOUSE_SEND_HZ = 60

# 显示模式
DISPLAY_FIT = "fit"
DISPLAY_ORIGINAL = "original"
DISPLAY_STRETCH = "stretch"

DISPLAY_MODES = (
    (DISPLAY_FIT, "适应窗口"),
    (DISPLAY_ORIGINAL, "原始尺寸"),
    (DISPLAY_STRETCH, "拉伸铺满"),
)

# 特殊按键面板：(菜单文字, 修饰键, 主键)
SPECIAL_KEYS = (
    ("切换窗口　Alt+Tab", ("alt",), "tab"),
    ("关闭窗口　Alt+F4", ("alt",), "f4"),
    ("任务管理器　Ctrl+Shift+Esc", ("ctrl", "shift"), "esc"),
    ("显示桌面　Win+D", ("win",), "d"),
    ("资源管理器　Win+E", ("win",), "e"),
    ("运行　Win+R", ("win",), "r"),
    ("锁定屏幕　Win+L", ("win",), "l"),
    ("复制　Ctrl+C", ("ctrl",), "c"),
    ("粘贴　Ctrl+V", ("ctrl",), "v"),
    ("全选　Ctrl+A", ("ctrl",), "a"),
    ("撤销　Ctrl+Z", ("ctrl",), "z"),
    ("保存　Ctrl+S", ("ctrl",), "s"),
)

# Qt 按键 -> pyautogui 按键名
KEY_MAP = {
    Qt.Key.Key_Return: "enter",
    Qt.Key.Key_Enter: "enter",
    Qt.Key.Key_Backspace: "backspace",
    Qt.Key.Key_Tab: "tab",
    Qt.Key.Key_Backtab: "tab",
    Qt.Key.Key_Escape: "esc",
    Qt.Key.Key_Delete: "delete",
    Qt.Key.Key_Insert: "insert",
    Qt.Key.Key_Home: "home",
    Qt.Key.Key_End: "end",
    Qt.Key.Key_PageUp: "pageup",
    Qt.Key.Key_PageDown: "pagedown",
    Qt.Key.Key_Up: "up",
    Qt.Key.Key_Down: "down",
    Qt.Key.Key_Left: "left",
    Qt.Key.Key_Right: "right",
    Qt.Key.Key_Space: "space",
    Qt.Key.Key_CapsLock: "capslock",
    Qt.Key.Key_NumLock: "numlock",
    Qt.Key.Key_ScrollLock: "scrolllock",
    Qt.Key.Key_Pause: "pause",
    Qt.Key.Key_Print: "printscreen",
    Qt.Key.Key_Menu: "apps",
    Qt.Key.Key_Shift: "shift",
    Qt.Key.Key_Control: "ctrl",
    Qt.Key.Key_Alt: "alt",
    Qt.Key.Key_Meta: "win",
    Qt.Key.Key_Plus: "add",
    Qt.Key.Key_Minus: "subtract",
    Qt.Key.Key_Asterisk: "multiply",
    Qt.Key.Key_Slash: "divide",
}
KEY_MAP.update({getattr(Qt.Key, f"Key_F{i}"): f"f{i}" for i in range(1, 13)})


class RemoteWindow(QWidget):
    """显示远端画面并回传输入事件。"""

    closed = pyqtSignal()
    # 工具栏请求打开聊天 / 文件面板（由主窗口统一管理这两个窗口的生命周期）
    chat_requested = pyqtSignal()
    files_requested = pyqtSignal()

    def __init__(self, bridge=None, parent=None):
        super().__init__(parent)
        self._bridge = bridge

        self.fullscreen = False
        self.remote_width = 0
        self.remote_height = 0
        self.display_mode = DISPLAY_FIT

        # 绘制区域（用于坐标反算；坐标系为 image_label 本地坐标）
        self._pixmap: QPixmap | None = None
        # 最近一帧原始 QImage（供自定义截图裁剪，非缩放后）
        self._frame_image: QImage | None = None
        self._draw_scale = 1.0
        self._draw_offset_x = 0.0
        self._draw_offset_y = 0.0

        self._pending_move: tuple[float, float] | None = None
        self._pending_scroll = 0

        self._last_fps = 0.0
        self._last_delay = 0.0
        self._paused = False
        self._monitors: list[dict] = []
        self._applying_quality = False

        self._init_ui()
        self._wire_bridge()

        # 鼠标移动 / 滚轮节流发送
        self._input_timer = QTimer(self)
        self._input_timer.setInterval(max(1, int(1000 / MOUSE_SEND_HZ)))
        self._input_timer.timeout.connect(self._flush_input)
        self._input_timer.start()

    # ================================================================== 界面
    def _init_ui(self) -> None:
        self.setWindowTitle("LAN Remote Desktop")
        self.setWindowIcon(app_icon())
        self.resize(1200, 800)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_toolbar())

        self.scroll = QScrollArea()
        self.scroll.setObjectName("remoteScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        self.image_label = QLabel("等待远端画面…")
        self.image_label.setObjectName("remoteScreen")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumSize(320, 240)
        self.image_label.setMouseTracking(True)
        self.image_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Ignored：让 label 始终填满滚动区域，坐标反算才能用 viewport 尺寸
        self.image_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.image_label.installEventFilter(self)

        self.scroll.setWidget(self.image_label)
        layout.addWidget(self.scroll, 1)

        self.hud = QLabel("未连接")
        self.hud.setObjectName("remoteHud")
        self.hud.setTextFormat(Qt.TextFormat.RichText)
        self.hud.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.hud, 0)

        self._hud_timer = QTimer(self)
        self._hud_timer.setInterval(1000)
        self._hud_timer.timeout.connect(self._refresh_hud)
        self._hud_timer.start()

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("remoteToolbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(10, 8, 10, 8)
        row.setSpacing(8)

        row.addWidget(self._tool_label("显示"))
        # 三个固定选项用分段控制器，比下拉框直观
        self.display_segment = SegmentedControl(DISPLAY_MODES)
        self.display_segment.setToolTip("画面缩放方式")
        self.display_segment.current_changed.connect(self._on_display_mode_changed)
        row.addWidget(self.display_segment)

        self.fullscreen_button = QPushButton("全屏")
        self.fullscreen_button.setObjectName("ghostButton")
        self.fullscreen_button.setToolTip("全屏显示（F11 切换，ESC 退出）")
        self.fullscreen_button.clicked.connect(self.toggle_fullscreen)
        row.addWidget(self.fullscreen_button)

        row.addWidget(self._tool_separator())

        row.addWidget(self._tool_label("画质"))
        self.quality_combo = QComboBox()
        for key in config.QUALITY_PROFILE_ORDER:
            label = config.QUALITY_PROFILES[key][0]
            self.quality_combo.addItem(label, key)
        self.quality_combo.setToolTip("通知被控端切换采集与编码参数")
        self.quality_combo.currentIndexChanged.connect(self._on_quality_changed)
        row.addWidget(self.quality_combo)

        row.addWidget(self._tool_label("显示器"))
        self.monitor_combo = QComboBox()
        self.monitor_combo.addItem("主显示器", 1)
        self.monitor_combo.setToolTip("被控端有多块屏幕时可切换采集目标")
        self.monitor_combo.currentIndexChanged.connect(self._on_monitor_changed)
        row.addWidget(self.monitor_combo)

        row.addWidget(self._tool_separator())

        self.pause_button = QPushButton("暂停画面")
        self.pause_button.setObjectName("ghostButton")
        self.pause_button.setCheckable(True)
        self.pause_button.setToolTip("暂停后画面不再传输，可节省对方的 CPU 与带宽")
        self.pause_button.toggled.connect(self._on_pause_toggled)
        row.addWidget(self.pause_button)

        clipboard_button = QToolButton()
        clipboard_button.setText("剪贴板")
        clipboard_button.setObjectName("ghostButton")
        clipboard_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        clipboard_menu = QMenu(clipboard_button)
        send_action = clipboard_menu.addAction("发送本机剪贴板")
        send_action.triggered.connect(self.send_clipboard)
        fetch_action = clipboard_menu.addAction("获取远端剪贴板")
        fetch_action.triggered.connect(self.fetch_clipboard)
        clipboard_button.setMenu(clipboard_menu)
        row.addWidget(clipboard_button)

        keys_button = QToolButton()
        keys_button.setText("特殊按键")
        keys_button.setObjectName("ghostButton")
        keys_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        keys_menu = QMenu(keys_button)
        for text, modifiers, key in SPECIAL_KEYS:
            action = keys_menu.addAction(text)
            action.triggered.connect(
                lambda _checked=False, m=modifiers, k=key: self.send_hotkey(m, k)
            )
        keys_button.setMenu(keys_menu)
        row.addWidget(keys_button)

        row.addWidget(self._tool_separator())

        self.screenshot_button = QPushButton("截图")
        self.screenshot_button.setObjectName("ghostButton")
        self.screenshot_button.setIcon(glyph("camera", 15))
        self.screenshot_button.setToolTip("自定义截图：框选区域或指定尺寸，保存 PNG / 复制到剪贴板")
        self.screenshot_button.clicked.connect(self.save_screenshot)
        row.addWidget(self.screenshot_button)

        # 聊天 / 文件：复用同一条连接，不需要额外端口或授权
        # 注意：这里刻意不注册 Ctrl+? 快捷键 —— 远程窗口会把所有按键转发给
        # 被控端，窗口级快捷键会把它截走，导致远端收不到该组合键。
        self.chat_button = QPushButton("聊天")
        self.chat_button.setObjectName("ghostButton")
        self.chat_button.setIcon(glyph("chat", 15))
        self.chat_button.setToolTip("和对方在线聊天（复用当前连接，无需额外授权）")
        self.chat_button.clicked.connect(self.chat_requested.emit)
        row.addWidget(self.chat_button)

        self.files_button = QPushButton("文件")
        self.files_button.setObjectName("ghostButton")
        self.files_button.setIcon(glyph("folder", 15))
        self.files_button.setToolTip("双向传文件，支持拖拽")
        self.files_button.clicked.connect(self.files_requested.emit)
        row.addWidget(self.files_button)

        row.addStretch(1)
        return bar

    @staticmethod
    def _tool_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("toolLabel")
        return label

    @staticmethod
    def _tool_separator() -> QFrame:
        """工具栏竖向分隔线（细线比字符「|」更干净）。"""
        line = QFrame()
        line.setObjectName("toolSeparator")
        line.setFrameShape(QFrame.Shape.VLine)
        line.setFixedHeight(20)
        return line

    def _wire_bridge(self) -> None:
        if self._bridge is None:
            return
        self._bridge.connected_changed.connect(self._on_connected)
        self._bridge.monitor_list_changed.connect(self._on_monitor_list)
        self._bridge.quality_state_changed.connect(self._on_quality_state)
        self._bridge.clipboard_received.connect(self._on_clipboard_received)

    # ================================================================== 画面
    def on_frame(self, image: QImage, width: int, height: int, fps: float, delay: float) -> None:
        """接收来自桥接器的画面（Qt 主线程槽函数）。"""
        if image is None or image.isNull():
            return

        self._last_fps = fps
        self._last_delay = delay
        self.remote_width = width or image.width()
        self.remote_height = height or image.height()

        self._frame_image = image
        self._pixmap = QPixmap.fromImage(image)
        self._render()

        # 通知桥接器本帧已消费，允许投递下一帧
        if self._bridge is not None:
            try:
                self._bridge.frame_consumed()
            except Exception:
                logger.exception("通知帧消费失败")

    # 兼容旧接口名
    update_screen = on_frame

    def _render(self) -> None:
        if self._pixmap is None:
            return

        if self.display_mode == DISPLAY_ORIGINAL:
            # 1:1 显示，超出部分由滚动条承载
            self.scroll.setWidgetResizable(False)
            self.image_label.setMinimumSize(1, 1)
            self.image_label.setMaximumSize(16777215, 16777215)
            self.image_label.setFixedSize(self._pixmap.size())
            self.image_label.setPixmap(self._pixmap)
            self._draw_scale = 1.0
            self._draw_offset_x = 0.0
            self._draw_offset_y = 0.0
            return

        self.scroll.setWidgetResizable(True)
        self.image_label.setMinimumSize(0, 0)
        self.image_label.setMaximumSize(16777215, 16777215)

        target = self.image_label.size()
        if target.width() <= 0 or target.height() <= 0:
            return

        if self.display_mode == DISPLAY_STRETCH:
            scaled = self._pixmap.scaled(
                target,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            offset_x = offset_y = 0.0
        else:
            scaled = self._pixmap.scaled(
                target,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            offset_x = (target.width() - scaled.width()) / 2.0
            offset_y = (target.height() - scaled.height()) / 2.0

        self.image_label.setPixmap(scaled)

        # 记录实际绘制区域，供坐标反算使用
        self._draw_scale = (scaled.width() / self._pixmap.width()) if self._pixmap.width() else 1.0
        self._draw_offset_x = offset_x
        self._draw_offset_y = offset_y

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # 布局尚未完成时 _render 会拿到 0 尺寸，延到下一轮事件循环再画
        QTimer.singleShot(0, self._render)

    def _refresh_hud(self) -> None:
        """状态栏：富文本渲染，关键数值按健康度着色。"""
        if self._pixmap is None:
            self.hud.setText("未连接 / 等待画面…")
            return

        stats: dict = {}
        if self._bridge is not None:
            try:
                stats = self._bridge.stats()
            except Exception:
                stats = {}

        fps = float(stats.get("fps", self._last_fps) or 0.0)
        delay = float(stats.get("delay", self._last_delay) or 0.0)
        bandwidth = self._format_bandwidth(float(stats.get("bytes_per_sec", 0.0) or 0.0))
        profile = self.quality_combo.currentData() or config.quality_profile()
        quality_label = config.QUALITY_PROFILES.get(profile, ("未知",))[0]
        mode_label = dict(DISPLAY_MODES).get(self.display_mode, self.display_mode)

        # 健康度配色：帧率越高越好，延迟越低越好
        c_good = theme.color("success_text")
        c_warn = theme.color("warn_text")
        c_bad = theme.color("danger_text")
        fps_color = c_good if fps >= 25 else (c_warn if fps >= 12 else c_bad)
        delay_color = c_good if delay <= 60 else (c_warn if delay <= 150 else c_bad)

        def item(label: str, value: str, value_color: str | None = None) -> str:
            color = value_color or theme.color("text")
            return (
                f'{label}&nbsp;&nbsp;'
                f'<span style="color:{color};font-weight:600;">{value}</span>'
            )

        dot = f'&nbsp;&nbsp;<span style="color:{theme.color("text_3")}">·</span>&nbsp;&nbsp;'
        parts = [
            item("分辨率", f"{self.remote_width}×{self.remote_height}"),
            item("帧率", f"{fps:.1f} FPS", fps_color),
            item("延迟", f"{delay:.0f} ms", delay_color),
            item("带宽", bandwidth),
            item("画质", quality_label),
            item("显示", mode_label),
        ]
        if self._paused:
            parts.append(item("⏸", "已暂停传输", theme.color("warn_text")))
        self.hud.setText(dot.join(parts))

    @staticmethod
    def _format_bandwidth(bytes_per_sec: float) -> str:
        if bytes_per_sec <= 0:
            return "—"
        if bytes_per_sec >= 1024 * 1024:
            return f"{bytes_per_sec / 1024 / 1024:.1f} MB/s"
        if bytes_per_sec >= 1024:
            return f"{bytes_per_sec / 1024:.0f} KB/s"
        return f"{bytes_per_sec:.0f} B/s"

    # ================================================================== 工具栏行为
    def _on_display_mode_changed(self, mode: str) -> None:
        if mode not in (DISPLAY_FIT, DISPLAY_ORIGINAL, DISPLAY_STRETCH):
            return
        self.display_mode = str(mode)
        self._render()
        self._refresh_hud()

    def _on_quality_changed(self, _index: int) -> None:
        if self._applying_quality:
            return
        profile = self.quality_combo.currentData()
        if not isinstance(profile, str) or self._bridge is None:
            return
        if not self._bridge.connected:
            self._append_hint("尚未连接，画质设置将在连接后生效")
            return
        self._bridge.set_quality_profile(profile)

    def _on_monitor_changed(self, _index: int) -> None:
        index = self.monitor_combo.currentData()
        if index is None or self._bridge is None or not self._bridge.connected:
            return
        self._bridge.select_monitor(int(index))

    def _on_pause_toggled(self, paused: bool) -> None:
        self._paused = bool(paused)
        self.pause_button.setText("继续画面" if paused else "暂停画面")
        if self._bridge is not None and self._bridge.connected:
            self._bridge.set_screen_enabled(not paused)
        self._refresh_hud()

    def send_clipboard(self) -> None:
        """把本机剪贴板推送到被控端。"""
        if self._bridge is None or not self._bridge.connected:
            self._append_hint("尚未连接，无法同步剪贴板")
            return
        from PyQt6.QtGui import QGuiApplication

        clipboard = QGuiApplication.clipboard()
        text = clipboard.text() if clipboard is not None else ""
        if not text:
            self._append_hint("本机剪贴板为空，未发送")
            return
        self._bridge.push_clipboard(text)
        self._append_hint(f"已发送剪贴板（{len(text)} 字符）")

    def fetch_clipboard(self) -> None:
        """请求被控端把它的剪贴板发过来。"""
        if self._bridge is None or not self._bridge.connected:
            self._append_hint("尚未连接，无法同步剪贴板")
            return
        self._bridge.fetch_clipboard()
        self._append_hint("正在获取远端剪贴板…")

    def send_hotkey(self, modifiers, key: str) -> None:
        """发送组合键：依次按下修饰键 -> 按主键 -> 反向抬起修饰键。"""
        if self._bridge is None or not self._bridge.connected:
            self._append_hint("尚未连接，无法发送按键")
            return
        if not key:
            return
        for name in modifiers:
            self._bridge.send_key(name, "down")
        self._bridge.send_key(key, "press")
        for name in reversed(tuple(modifiers)):
            self._bridge.send_key(name, "up")

    def _append_hint(self, text: str) -> None:
        self.hud.setText(text)

    def save_screenshot(self) -> None:
        """打开自定义截图对话框：框选区域 / 指定尺寸，保存 PNG 或复制。"""
        if self._frame_image is None or self._frame_image.isNull():
            self._append_hint("暂无画面，无法截图")
            return

        from ui.screenshot_dialog import ScreenshotDialog

        dialog = ScreenshotDialog(
            self._frame_image,
            source=f"{self.remote_width}x{self.remote_height}",
            parent=self,
        )
        dialog.exec()

        if dialog.saved_path:
            self._append_hint(f"已截图：{dialog.saved_path}")
        elif dialog.copied:
            self._append_hint("截图已复制到剪贴板")

    # ================================================================== 桥接回调
    def _on_connected(self, connected: bool) -> None:
        if connected:
            # 连上后主动同步一次画质档位与显示器列表
            if self._bridge is not None:
                self._bridge.request_monitor_list()
        else:
            self._paused = False
            self.pause_button.setChecked(False)

    def _on_monitor_list(self, monitors: list, current: int) -> None:
        self._monitors = list(monitors)
        self.monitor_combo.blockSignals(True)
        self.monitor_combo.clear()
        for item in self._monitors:
            index = int(item.get("index", 0))
            width = int(item.get("width", 0))
            height = int(item.get("height", 0))
            if index == 0:
                name = f"全部屏幕 {width}×{height}"
            else:
                name = f"显示器 {index}  {width}×{height}"
            self.monitor_combo.addItem(name, index)
        position = self.monitor_combo.findData(int(current))
        if position >= 0:
            self.monitor_combo.setCurrentIndex(position)
        self.monitor_combo.blockSignals(False)

    def _on_quality_state(self, profile: str, fps: int, quality: int, max_width: int) -> None:
        self._applying_quality = True
        try:
            position = self.quality_combo.findData(profile)
            if position >= 0:
                self.quality_combo.setCurrentIndex(position)
        finally:
            self._applying_quality = False
        self._refresh_hud()

    def _on_clipboard_received(self, text: str) -> None:
        """收到对端剪贴板：写入本机剪贴板。"""
        from PyQt6.QtGui import QGuiApplication

        clipboard = QGuiApplication.clipboard()
        if clipboard is not None and text:
            clipboard.setText(text)
        preview = text.strip().replace("\n", " ")[:30]
        self._append_hint(f"已获取远端剪贴板（{len(text)} 字符）：{preview}")

    # ================================================================== 坐标映射
    def _to_remote(self, pos: QPointF) -> tuple[float, float] | None:
        """
        把 image_label 本地坐标映射为远端画面坐标。

        注意传入的必须是 **image_label 的本地坐标**（事件过滤器里天然满足），
        这样工具栏、滚动条的出现都不会影响坐标换算。
        """
        if self._pixmap is None or self._draw_scale <= 0 or self.remote_width <= 0:
            return None
        x = (pos.x() - self._draw_offset_x) / self._draw_scale
        y = (pos.y() - self._draw_offset_y) / self._draw_scale
        x = max(0.0, min(float(self.remote_width - 1), x))
        y = max(0.0, min(float(self.remote_height - 1), y))
        return x, y

    def _to_label_pos(self, window_pos: QPointF) -> QPointF:
        """窗口坐标 -> image_label 本地坐标（供窗口级事件兜底使用）。"""
        point = self.image_label.mapFrom(self, window_pos.toPoint())
        return QPointF(float(point.x()), float(point.y()))

    # ================================================================== 事件过滤
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """拦截图像控件上的鼠标与滚轮事件（此处坐标即图像本地坐标）。"""
        if obj is not self.image_label:
            return super().eventFilter(obj, event)

        event_type = event.type()
        if event_type == QEvent.Type.MouseMove:
            self._queue_move(event.position())
            return True
        if event_type == QEvent.Type.MouseButtonPress:
            self._handle_press(event, event.position())
            return True
        if event_type == QEvent.Type.MouseButtonRelease:
            self._handle_release(event)
            return True
        if event_type == QEvent.Type.MouseButtonDblClick:
            # Qt 会把「第二次按下」派发成双击事件而不是 Press；这里若吞掉，
            # 远端只能收到一次点击，表现为双击打开失灵。
            # 处理方式与 Press 完全一致：再发一次 按下，随后的 Release 发抬起，
            # 远端于是收到完整的两次点击，由对方系统判定为双击。
            self._handle_press(event, event.position())
            return True
        if event_type == QEvent.Type.Wheel:
            return self._handle_wheel(event)
        return super().eventFilter(obj, event)

    def _queue_move(self, label_pos: QPointF) -> None:
        coords = self._to_remote(label_pos)
        if coords is not None:
            self._pending_move = coords

    def _handle_press(self, event, label_pos: QPointF) -> None:
        self.setFocus()
        if self._bridge is None:
            return
        button = self._button_name(event.button())
        coords = self._to_remote(label_pos)
        if coords is not None:
            self._pending_move = coords
            self._bridge.send_mouse_move(coords[0], coords[1])
        if button:
            self._bridge.send_mouse_button(button, "down")

    def _handle_release(self, event) -> None:
        if self._bridge is None:
            return
        button = self._button_name(event.button())
        if button:
            self._bridge.send_mouse_button(button, "up")

    def _handle_wheel(self, event) -> bool:
        # 原始尺寸模式下让滚轮去滚滚动条，其余模式把滚动发给远端
        if self.display_mode == DISPLAY_ORIGINAL:
            return False
        if self._bridge is None:
            return True
        delta = event.angleDelta().y()
        if not delta:
            return True
        steps = int(delta / 120)
        if steps == 0:
            steps = 1 if delta > 0 else -1
        self._pending_scroll += steps
        return True

    # ================================================================== 输入
    def _flush_input(self) -> None:
        """把节流期间累积的移动 / 滚轮一次性发出。"""
        if self._bridge is None:
            return

        move, self._pending_move = self._pending_move, None
        if move is not None:
            self._bridge.send_mouse_move(move[0], move[1])

        scroll, self._pending_scroll = self._pending_scroll, 0
        if scroll:
            self._bridge.send_mouse_scroll(scroll)

    @staticmethod
    def _button_name(button) -> str | None:
        if button == Qt.MouseButton.LeftButton:
            return "left"
        if button == Qt.MouseButton.RightButton:
            return "right"
        if button == Qt.MouseButton.MiddleButton:
            return "middle"
        return None

    # 窗口级兜底：图像控件未覆盖到的区域（理论上不存在，保险起见保留）
    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self._queue_move(self._to_label_pos(event.position()))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._handle_press(event, self._to_label_pos(event.position()))

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._handle_release(event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        self._handle_wheel(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if self._bridge is None or event.isAutoRepeat():
            return

        key = event.key()
        text = event.text()
        modifiers = event.modifiers()

        if key == Qt.Key.Key_F11:
            self.toggle_fullscreen()
            return

        # ESC：先退出全屏
        if key == Qt.Key.Key_Escape and self.fullscreen:
            self.toggle_fullscreen()

        mods = self._active_modifiers(modifiers)
        if mods:
            mapped = KEY_MAP.get(key) or (text if len(text) == 1 else None)
            # 单独按下修饰键时不发组合键
            if key in (Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Alt, Qt.Key.Key_Meta):
                return
            for name in mods:
                self._bridge.send_key(name, "down")
            if mapped:
                self._bridge.send_key(mapped, "press")
            for name in reversed(mods):
                self._bridge.send_key(name, "up")
            return

        mapped = KEY_MAP.get(key)
        if mapped:
            self._bridge.send_key(mapped, "press")
        elif text and len(text) == 1 and text.isprintable():
            self._bridge.send_key(text, "press")

    @staticmethod
    def _active_modifiers(modifiers) -> list[str]:
        result = []
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            result.append("ctrl")
        if modifiers & Qt.KeyboardModifier.AltModifier:
            result.append("alt")
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            result.append("shift")
        if modifiers & Qt.KeyboardModifier.MetaModifier:
            result.append("win")
        return result

    # ================================================================== 全屏
    def toggle_fullscreen(self) -> None:
        if self.fullscreen:
            self.showNormal()
        else:
            self.showFullScreen()
        self.fullscreen = not self.fullscreen
        self.fullscreen_button.setText("退出全屏" if self.fullscreen else "全屏")
        QTimer.singleShot(0, self._render)

    def keyReleaseEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and self.fullscreen:
            self.toggle_fullscreen()

    # ================================================================== 关闭
    def closeEvent(self, event) -> None:  # noqa: N802
        self._input_timer.stop()
        self._hud_timer.stop()
        self.closed.emit()
        super().closeEvent(event)
