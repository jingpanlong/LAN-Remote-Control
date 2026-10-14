"""
自定义截图对话框

在最近一帧远端画面上圈选区域（或直接填 X/Y/宽/高），
支持按任意尺寸裁剪保存 PNG / 复制到剪贴板。
"""

from __future__ import annotations

from PyQt6.QtCore import QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtGui import QGuiApplication, QImage, QColor, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ui.icons import app_icon

# 预览区最大尺寸（原始画面等比缩小到这个范围内显示）
PREVIEW_MAX_W = 720
PREVIEW_MAX_H = 440


class _PreviewLabel(QLabel):
    """带框选功能的画面预览控件：鼠标拖拽圈选，实时绘制选框。"""

    selection_changed = pyqtSignal(QRect)  # 原图坐标系下的选区

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(320, 220)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._origin: QPoint | None = None
        self._current: QPoint | None = None
        self._scale = 1.0  # 预览 -> 原图的缩放系数
        self._frame: QImage | None = None
        self._pixmap: QPixmap | None = None

    # ---------------------------------------------------------------- 数据
    def set_frame(self, frame: QImage) -> None:
        """载入原始画面并等比缩小显示。"""
        if frame is None or frame.isNull():
            self.setText("暂无画面")
            self._pixmap = None
            return
        self._frame = frame
        size = frame.size()
        size.scale(PREVIEW_MAX_W, PREVIEW_MAX_H, Qt.AspectRatioMode.KeepAspectRatio)
        self._scale = frame.width() / max(1, size.width())
        self._pixmap = QPixmap.fromImage(frame).scaled(
            size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        self.setPixmap(self._pixmap)
        self.setFixedSize(self._pixmap.size())
        self._origin = None
        self._current = None
        self.update()

    def mapped_rect(self) -> QRect | None:
        """当前选区换算到原图坐标；无有效选区时返回整幅画面。"""
        if self._pixmap is None or self._frame is None:
            return None
        if self._origin is None or self._current is None:
            return QRect(0, 0, self._frame.width(), self._frame.height())
        preview = QRect(self._origin, self._current).normalized()
        rect = QRect(
            int(preview.x() * self._scale),
            int(preview.y() * self._scale),
            max(1, int(preview.width() * self._scale)),
            max(1, int(preview.height() * self._scale)),
        )
        # 夹在画面范围内
        return rect.intersected(self._frame.rect())

    def select_all(self) -> None:
        self._origin = QPoint(0, 0)
        self._current = QPoint(self.width() - 1, self.height() - 1)
        self.update()
        rect = self.mapped_rect()
        if rect is not None:
            self.selection_changed.emit(rect)

    # ---------------------------------------------------------------- 事件
    def _clamp(self, point: QPoint) -> QPoint:
        """把鼠标位置限制在预览控件范围内。"""
        return QPoint(
            max(0, min(self.width() - 1, point.x())),
            max(0, min(self.height() - 1, point.y())),
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = self._clamp(event.position().toPoint())
            self._current = self._origin
            self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._origin is not None:
            self._current = self._clamp(event.position().toPoint())
            self.update()
            rect = self.mapped_rect()
            if rect is not None:
                self.selection_changed.emit(rect)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._origin is not None:
            self._current = self._clamp(event.position().toPoint())
            self.update()
            rect = self.mapped_rect()
            if rect is not None:
                self.selection_changed.emit(rect)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if self._origin is None or self._current is None:
            return
        selection = QRect(self._origin, self._current).normalized()
        painter = QPainter(self)
        # 白 + 黑双色描边：深浅两套主题、明暗画面上都看得清
        white = QPen(QColor(255, 255, 255))
        white.setWidth(1)
        painter.setPen(white)
        painter.drawRect(selection)
        black = QPen(QColor(0, 0, 0))
        black.setWidth(1)
        painter.setPen(black)
        painter.drawRect(selection.adjusted(1, 1, -1, -1))
        painter.end()


class ScreenshotDialog(QDialog):
    """远端画面自定义截图：框选 / 填坐标 / 一键全屏，保存 PNG 或复制。"""

    def __init__(self, frame: QImage, source: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("自定义截图")
        self.setWindowIcon(app_icon())
        self.setModal(True)

        self._frame = frame
        self._source = source

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 12)
        outer.setSpacing(10)

        hint = QLabel("在画面上按住鼠标左键框选区域；也可以直接输入坐标与尺寸。")
        hint.setObjectName("aboutDesc")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        self.preview = _PreviewLabel(self)
        self.preview.set_frame(frame)
        preview_row = QHBoxLayout()
        preview_row.addStretch(1)
        preview_row.addWidget(self.preview)
        preview_row.addStretch(1)
        outer.addLayout(preview_row)

        # ------------------------------------------------------ 坐标 / 尺寸
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)

        def coord_spin(maximum: int) -> QSpinBox:
            spin = QSpinBox()
            spin.setRange(0, max(1, maximum))
            spin.setObjectName("coordSpin")
            return spin

        self.x_spin = coord_spin(frame.width())
        self.y_spin = coord_spin(frame.height())
        self.w_spin = coord_spin(frame.width())
        self.h_spin = coord_spin(frame.height())
        self.w_spin.setValue(max(1, frame.width()))
        self.h_spin.setValue(max(1, frame.height()))

        for column, (label, widget) in enumerate(
            (("X", self.x_spin), ("Y", self.y_spin), ("宽", self.w_spin), ("高", self.h_spin))
        ):
            title = QLabel(label)
            title.setObjectName("fieldLabel")
            grid.addWidget(title, 0, column)
            grid.addWidget(widget, 1, column)

        outer.addLayout(grid)

        # 选框变化 -> 同步数值输入框
        self.preview.selection_changed.connect(self._sync_fields)
        for spin in (self.x_spin, self.y_spin, self.w_spin, self.h_spin):
            spin.valueChanged.connect(self._on_field_changed)

        # ------------------------------------------------------ 按钮
        row = QHBoxLayout()
        row.setSpacing(8)

        full_button = QPushButton("整幅画面")
        full_button.setObjectName("ghostButton")
        full_button.clicked.connect(self._select_all)
        row.addWidget(full_button)

        copy_button = QPushButton("复制到剪贴板")
        copy_button.setObjectName("ghostButton")
        copy_button.clicked.connect(self._copy_crop)
        row.addWidget(copy_button)

        row.addStretch(1)

        cancel_button = QPushButton("取消")
        cancel_button.setObjectName("ghostButton")
        cancel_button.clicked.connect(self.reject)
        row.addWidget(cancel_button)

        save_button = QPushButton("保存 PNG")
        save_button.setObjectName("connectButton")
        save_button.setDefault(True)
        save_button.clicked.connect(self.accept)
        row.addWidget(save_button)

        outer.addLayout(row)

        self.saved_path: str = ""
        self.copied: bool = False

    # ---------------------------------------------------------------- 内部
    def _sync_fields(self, rect: QRect) -> None:
        for spin, value in (
            (self.x_spin, rect.x()),
            (self.y_spin, rect.y()),
            (self.w_spin, rect.width()),
            (self.h_spin, rect.height()),
        ):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)

    def _on_field_changed(self) -> None:
        """数值输入框变化 -> 反向刷新选框。"""
        rect = QRect(
            self.x_spin.value(),
            self.y_spin.value(),
            max(1, self.w_spin.value()),
            max(1, self.h_spin.value()),
        )
        scale = self.preview._scale
        self.preview._origin = QPoint(int(rect.x() / scale), int(rect.y() / scale))
        self.preview._current = QPoint(
            int((rect.x() + rect.width()) / scale),
            int((rect.y() + rect.height()) / scale),
        )
        self.preview.update()

    def _select_all(self) -> None:
        self.preview.select_all()

    # ---------------------------------------------------------------- 结果
    def crop_rect(self) -> QRect:
        rect = QRect(
            self.x_spin.value(),
            self.y_spin.value(),
            max(1, self.w_spin.value()),
            max(1, self.h_spin.value()),
        )
        return rect.intersected(self._frame.rect())

    def result_crop(self) -> QImage:
        return self._frame.copy(self.crop_rect())

    def accept(self) -> None:  # noqa: N802
        """保存裁剪结果到截图目录。"""
        import os
        from datetime import datetime

        import config

        image = self.result_crop()
        if image.isNull():
            self.reject()
            return
        directory = config.screenshot_dir()
        filename = f"remote-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
        path = os.path.join(directory, filename)
        try:
            if not image.save(path, "PNG"):
                raise OSError("图像编码失败")
        except OSError:
            self.saved_path = ""
            super().accept()
            return
        self.saved_path = path
        super().accept()

    def _copy_crop(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setImage(self.result_crop())
        self.copied = True
        self.reject()
