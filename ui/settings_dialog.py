"""
设置对话框

集中管理所有可调参数并持久化到 cache/settings.json：
    - 常规：开机自启
    - 服务：监听端口、端口回退尝试次数（重启程序后生效）
    - 画面：帧率、JPEG 质量、最大宽度（保存后立即生效）
    - 密码：位数、有效期（保存后立即刷新新密码）
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

import config
from startup import auto_start as auto_start_module
from ui import theme
from ui.icons import eye_icon

logger = logging.getLogger("LAN_REMOTE")

RESTART_NOTE = "重启程序后生效"


class SettingsDialog(QDialog):
    """LAN Remote 设置。"""

    def __init__(self, parent=None, credentials=None, server=None):
        super().__init__(parent)
        self._credentials = credentials
        self._server = server
        # 记录打开时的主题，取消时还原（主题切换在对话框里是即时预览的）
        self._theme_at_open = theme.current_mode()

        self.setWindowTitle("设置")
        self.setModal(True)
        self.setMinimumWidth(480)
        self.resize(500, 680)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)

        # 内容放进滚动区：小屏也不会裁掉底部按钮
        content = QWidget()
        form_box = QVBoxLayout(content)
        form_box.setContentsMargins(0, 0, 0, 0)
        form_box.setSpacing(4)
        form_box.addWidget(self._build_appearance_group())
        form_box.addWidget(self._build_general_group())
        form_box.addWidget(self._build_server_group())
        form_box.addWidget(self._build_screen_group())
        form_box.addWidget(self._build_password_group())
        form_box.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        row = QHBoxLayout()
        note = QLabel(f"带 * 的项{RESTART_NOTE}；其余保存后立即生效。")
        note.setObjectName("hintLabel")
        note.setWordWrap(True)

        save = QPushButton("保存")
        save.setObjectName("connectButton")
        save.setCursor(Qt.CursorShape.PointingHandCursor)
        save.clicked.connect(self._save)
        cancel = QPushButton("取消")
        cancel.setObjectName("ghostButton")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(self.reject)

        row.addWidget(note, 1)
        row.addWidget(save)
        row.addWidget(cancel)
        layout.addLayout(row)

    # ================================================================== 控件
    def _build_appearance_group(self) -> QGroupBox:
        """外观：深浅色主题，切换即时预览，保存才写入设置。"""
        group = QGroupBox("外观")
        form = QFormLayout(group)

        self.theme_combo = QComboBox()
        for mode in theme.MODE_ORDER:
            self.theme_combo.addItem(theme.MODE_LABELS.get(mode, mode), mode)
        position = self.theme_combo.findData(theme.current_mode())
        if position >= 0:
            self.theme_combo.setCurrentIndex(position)
        self.theme_combo.setToolTip("切换立即生效并实时预览；点「取消」会还原")
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        form.addRow("主题：", self.theme_combo)
        return group

    def _on_theme_changed(self, _index: int) -> None:
        mode = self.theme_combo.currentData()
        if mode in theme.MODE_ORDER:
            theme.set_mode(mode, persist=False)
            theme.apply()

    def _build_general_group(self) -> QGroupBox:
        group = QGroupBox("常规")
        form = QFormLayout(group)
        self.auto_start_box = QCheckBox("开机自动启动")
        self.auto_start_box.setChecked(auto_start_module.check_auto_start())
        form.addRow("", self.auto_start_box)
        return group

    def _build_server_group(self) -> QGroupBox:
        group = QGroupBox("服务")
        form = QFormLayout(group)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(config.server_port())
        form.addRow("监听端口 *：", self.port_spin)

        self.fallback_spin = QSpinBox()
        self.fallback_spin.setRange(0, 20)
        self.fallback_spin.setValue(config.server_port_fallback())
        self.fallback_spin.setToolTip("首选端口被占用时，向后尝试的端口数量")
        form.addRow("端口回退次数 *：", self.fallback_spin)

        return group

    def _build_screen_group(self) -> QGroupBox:
        group = QGroupBox("画面")
        form = QFormLayout(group)

        self.profile_combo = QComboBox()
        for key in config.QUALITY_PROFILE_ORDER:
            self.profile_combo.addItem(config.QUALITY_PROFILES[key][0], key)
        position = self.profile_combo.findData(config.quality_profile())
        if position >= 0:
            self.profile_combo.setCurrentIndex(position)
        self.profile_combo.setToolTip(
            "选择档位会自动填入下面的帧率 / 质量 / 宽度，之后仍可手动微调。"
        )
        self.profile_combo.currentIndexChanged.connect(self._apply_profile_to_fields)
        form.addRow("画质档位：", self.profile_combo)

        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(config.SCREEN_FPS_MIN, config.SCREEN_FPS_MAX)
        self.fps_spin.setValue(config.screen_fps())
        self.fps_spin.setSuffix(" FPS")
        self.fps_spin.setToolTip(
            f"可设 1 ~ {config.SCREEN_FPS_MAX} FPS。\n"
            "30 已相当流畅；60 需要被控端 CPU 与局域网带宽都跟得上，"
            "建议配合较低的分辨率上限使用。"
        )
        form.addRow("帧率：", self.fps_spin)

        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(config.SCREEN_QUALITY_MIN, config.SCREEN_QUALITY_MAX)
        self.quality_spin.setValue(config.screen_quality())
        self.quality_spin.setSuffix(" %")
        form.addRow("JPEG 质量：", self.quality_spin)

        self.max_width_spin = QSpinBox()
        self.max_width_spin.setRange(0, 3840)
        self.max_width_spin.setSingleStep(160)
        self.max_width_spin.setValue(config.screen_max_width())
        self.max_width_spin.setSuffix(" px")
        self.max_width_spin.setToolTip("画面宽度上限，超出会等比缩小以降低带宽；0 表示不限制")
        form.addRow("最大宽度：", self.max_width_spin)

        return group

    def _apply_profile_to_fields(self, _index: int) -> None:
        """选中档位后把对应参数填进下面的输入框（仍允许手动微调）。"""
        profile = self.profile_combo.currentData()
        values = config.QUALITY_PROFILES.get(profile) if isinstance(profile, str) else None
        if values is None:
            return
        _, fps, quality, max_width = values
        self.fps_spin.setValue(int(fps))
        self.quality_spin.setValue(int(quality))
        self.max_width_spin.setValue(int(max_width))

    def _build_password_group(self) -> QGroupBox:
        group = QGroupBox("访问密码")
        form = QFormLayout(group)

        # ------------------------------------------------------ 随机密码位数
        self.length_spin = QSpinBox()
        self.length_spin.setRange(4, 16)
        self.length_spin.setValue(config.password_length())
        self.length_spin.setSuffix(" 位")
        self.length_spin.setToolTip("由字母 + 数字随机组合（不含 0/O、1/l/I 这类易混字符）")
        form.addRow("随机密码位数：", self.length_spin)

        # ------------------------------------------------------ 有效期：数值 + 单位
        expire_row = QHBoxLayout()
        expire_row.setSpacing(6)

        self.expire_spin = QSpinBox()
        self.expire_spin.setRange(1, 9999)
        self.expire_spin.setValue(self._split_expire(config.password_expire())[0])

        self.expire_unit_combo = QComboBox()
        self.expire_unit_combo.addItem("分钟", 60)
        self.expire_unit_combo.addItem("小时", 3600)
        self.expire_unit_combo.addItem("天", 86400)
        self.expire_unit_combo.addItem("永久有效", 0)
        self.expire_unit_combo.setCurrentIndex(self._expire_unit_index(config.password_expire()))
        self.expire_unit_combo.currentIndexChanged.connect(self._on_expire_unit_changed)

        expire_row.addWidget(self.expire_spin, 1)
        expire_row.addWidget(self.expire_unit_combo, 1)
        form.addRow("临时密码有效期：", self._wrap(expire_row))

        # ------------------------------------------------------ 固定密码
        self.fixed_box = QCheckBox("使用固定密码（不再自动更换）")
        self.fixed_box.setToolTip(
            "勾选后本机始终使用下面这个密码，不会到期、不会被自动刷新；\n"
            "取消勾选则恢复为按上方有效期自动更换的随机密码。"
        )
        credentials = self._credentials
        is_fixed = bool(credentials is not None and getattr(credentials, "is_fixed", False))
        self.fixed_box.setChecked(is_fixed)
        self.fixed_box.toggled.connect(self._on_fixed_toggled)
        form.addRow("", self.fixed_box)

        self.fixed_input = QLineEdit()
        self.fixed_input.setPlaceholderText("至少 4 位，可含字母、数字与符号")
        self.fixed_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.fixed_eye = self.fixed_input.addAction(
            eye_icon(False), QLineEdit.ActionPosition.TrailingPosition
        )
        self.fixed_eye.triggered.connect(lambda: self._toggle_password_visible(self.fixed_input, self.fixed_eye))
        if is_fixed and credentials is not None:
            self.fixed_input.setText(credentials.password)
        self.fixed_input.textEdited.connect(
            lambda _t: self._sync_eye_icon(self.fixed_input, self.fixed_eye)
        )
        form.addRow("固定密码：", self.fixed_input)

        self._on_fixed_toggled()
        return group

    @staticmethod
    def _wrap(layout) -> QWidget:
        holder = QWidget()
        layout.setContentsMargins(0, 0, 0, 0)
        holder.setLayout(layout)
        return holder

    @staticmethod
    def _split_expire(seconds: int) -> tuple[int, int]:
        """把秒数拆成 (数值, 每单位秒数)，选一个最合适的单位。"""
        seconds = max(0, int(seconds or 0))
        if seconds <= 0:
            return 1, 0
        if seconds % 86400 == 0:
            return seconds // 86400, 86400
        if seconds % 3600 == 0:
            return seconds // 3600, 3600
        return max(1, seconds // 60), 60

    @staticmethod
    def _expire_unit_index(seconds: int) -> int:
        """根据秒数挑一个最自然的单位下拉项。"""
        _, unit = SettingsDialog._split_expire(seconds)
        units = [60, 3600, 86400, 0]
        return units.index(unit) if unit in units else 0

    def _on_expire_unit_changed(self, _index: int) -> None:
        permanent = self.expire_unit_combo.currentData() == 0
        self.expire_spin.setEnabled(not permanent)

    def _on_fixed_toggled(self) -> None:
        """固定密码与随机密码互斥：选固定时有效期控件置灰。"""
        use_fixed = self.fixed_box.isChecked()
        self.fixed_input.setEnabled(use_fixed)
        self.length_spin.setEnabled(not use_fixed)
        self.expire_spin.setEnabled(not use_fixed and self.expire_unit_combo.currentData() != 0)
        self.expire_unit_combo.setEnabled(not use_fixed)
        if not use_fixed:
            self.fixed_input.clear()

    @staticmethod
    def _toggle_password_visible(field: QLineEdit, action) -> None:
        hidden = field.echoMode() == QLineEdit.EchoMode.Password
        field.setEchoMode(QLineEdit.EchoMode.Normal if hidden else QLineEdit.EchoMode.Password)
        SettingsDialog._sync_eye_icon(field, action)

    @staticmethod
    def _sync_eye_icon(field: QLineEdit, action) -> None:
        opened = field.echoMode() != QLineEdit.EchoMode.Password
        action.setIcon(eye_icon(opened))
        action.setToolTip("隐藏密码" if opened else "显示密码")

    # ================================================================== 保存
    def _expire_seconds(self) -> int:
        """把「数值 + 单位」换算成秒；永久有效返回 0。"""
        unit = int(self.expire_unit_combo.currentData() or 0)
        if unit <= 0:
            return 0
        return int(self.expire_spin.value()) * unit

    def _save(self) -> None:
        fps = int(self.fps_spin.value())
        quality = int(self.quality_spin.value())
        max_width = int(self.max_width_spin.value())

        # 手动微调过参数时，按实际值反推最接近的档位，保证界面回显一致
        profile = self.profile_combo.currentData()
        if isinstance(profile, str):
            _, p_fps, p_quality, p_width = config.QUALITY_PROFILES[profile]
            if (fps, quality, max_width) != (p_fps, p_quality, p_width):
                profile = config.detect_quality_profile(fps, quality, max_width)
        else:
            profile = config.DEFAULT_QUALITY_PROFILE

        try:
            config.set_setting("server_port", int(self.port_spin.value()))
            config.set_setting("server_port_fallback", int(self.fallback_spin.value()))
            # 主题：保存时才落盘（切换过程中只是预览）
            theme_mode = self.theme_combo.currentData()
            if theme_mode in theme.MODE_ORDER:
                theme.set_mode(theme_mode, persist=True)
            config.set_setting("quality_profile", profile)
            config.set_setting("screen_fps", fps)
            config.set_setting("screen_quality", quality)
            config.set_setting("screen_max_width", max_width)
            config.set_setting("password_length", int(self.length_spin.value()))
            config.set_setting("password_expire", self._expire_seconds())
        except OSError as exc:
            logger.error("写入设置失败：%s", exc)

        # 开机自启
        want_auto = self.auto_start_box.isChecked()
        if want_auto != auto_start_module.check_auto_start():
            if not (auto_start_module.enable_auto_start() if want_auto else auto_start_module.disable_auto_start()):
                logger.warning("应用开机自启设置失败")

        # 密码：位数与有效期立即生效；固定密码模式下保持口令不变
        if self._credentials is not None:
            try:
                if self.fixed_box.isChecked():
                    text = self.fixed_input.text().strip()
                    if len(text) < 4:
                        QMessageBox.warning(
                            self, "固定密码", "固定密码至少需要 4 位，请修改后再保存。"
                        )
                        self.fixed_input.setFocus()
                        return
                    self._credentials.set_fixed_password(text)
                else:
                    # 之前是固定密码、这次取消了勾选：先真正退出固定模式，
                    # 再应用位数与有效期（否则固定语义会吞掉这次修改）
                    if getattr(self._credentials, "is_fixed", False):
                        self._credentials.clear_fixed_password()
                    self._credentials.apply(
                        length=int(self.length_spin.value()),
                        ttl=self._expire_seconds(),
                    )
            except Exception as exc:
                logger.exception("应用密码设置失败")
                QMessageBox.warning(self, "设置", f"密码设置未能生效：{exc}")
                return

        # 画面参数：保存后立即作用于采集与编码（帧率同时同步给推流节拍）
        if self._server is not None:
            try:
                self._server.apply_screen_settings(
                    fps=fps, quality=quality, max_width=max_width
                )
            except Exception:
                logger.exception("应用画面设置失败")

        self.accept()


    def reject(self) -> None:  # noqa: N802
        """取消：还原打开时的主题（预览不生效）。"""
        if theme.current_mode() != self._theme_at_open:
            theme.set_mode(self._theme_at_open, persist=False)
            theme.apply()
        super().reject()


def open_settings(parent=None, credentials=None, server=None) -> bool:
    """打开设置对话框，返回是否点了保存。"""
    dialog = SettingsDialog(parent, credentials=credentials, server=server)
    return dialog.exec() == QDialog.DialogCode.Accepted
