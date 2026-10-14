"""
图标统一入口

全程序（主窗口 / 远程窗口 / 聊天 / 文件 / 托盘 / 任务栏）的图标
统一从这里取，避免各处各写一套、口径不一致。

两类图标：
    - app_icon()：assets/icon.ico，程序主图标；
    - glyph()   ：程序化绘制的小图标（设置、关于、电源、显示器…），
                  颜色跟随当前主题，因此不需要额外素材，也不会在浅色主题下发灰。
"""

from __future__ import annotations

import logging
import math
import os

from PyQt6.QtCore import QObject, QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF

import config
from ui import theme

logger = logging.getLogger("LAN_REMOTE")

# 支持的程序化图标
GLYPHS = (
    "settings", "info", "power", "monitor", "chat", "copy", "refresh", "folder", "home",
    "camera", "search", "star", "log", "phone",
)

_cached: QIcon | None = None
_eye_cache: dict[tuple[bool, str], QIcon] = {}
_glyph_cache: dict[tuple[str, int, str], QIcon] = {}


def app_icon() -> QIcon:
    """
    返回应用图标（assets/icon.ico）。

    结果进程内缓存，重复调用无开销；
    文件缺失或无法解析时返回空图标，由 Qt 回退到系统默认图。
    """
    global _cached
    if _cached is not None:
        return _cached

    if os.path.exists(config.ICON_FILE):
        icon = QIcon(config.ICON_FILE)
        if not icon.isNull():
            # .ico 内含多种尺寸，确保大图标场景（Alt+Tab / 任务栏）不糊
            _cached = icon
            return _cached
        logger.warning("图标文件无法解析：%s", config.ICON_FILE)
    else:
        logger.warning("图标文件不存在：%s", config.ICON_FILE)

    _cached = QIcon()
    return _cached


def app_pixmap(size: int = 40) -> QPixmap:
    """主图标位图（用于界面里的 brand 标识）。"""
    icon = app_icon()
    if icon.isNull():
        return QPixmap()
    return icon.pixmap(size, size)


def apply_to(window: QObject) -> None:
    """给窗口设置应用图标（供各窗口统一调用）。"""
    setter = getattr(window, "setWindowIcon", None)
    if setter is not None:
        setter(app_icon())


def _new_canvas(size: int) -> tuple[QPixmap, QPainter]:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    return pixmap, painter


def _rounded(painter: QPainter, x: float, y: float, w: float, h: float, r: float) -> None:
    path = QPainterPath()
    path.addRoundedRect(QRectF(x, y, w, h), r, r)
    painter.drawPath(path)


def _draw_glyph(kind: str, painter: QPainter, s: float) -> bool:
    """按 18px 基准的等比坐标绘制；返回是否识别该图标。"""
    if kind == "settings":
        center = QPointF(s / 2.0, s / 2.0)
        painter.drawEllipse(center, s * 0.29, s * 0.29)
        painter.drawEllipse(center, s * 0.11, s * 0.11)
        for i in range(8):
            angle = math.radians(i * 45.0)
            cos_a, sin_a = math.cos(angle), math.sin(angle)
            painter.drawLine(
                QPointF(center.x() + cos_a * s * 0.30, center.y() + sin_a * s * 0.30),
                QPointF(center.x() + cos_a * s * 0.45, center.y() + sin_a * s * 0.45),
            )
    elif kind == "info":
        painter.drawEllipse(QPointF(s / 2.0, s / 2.0), s * 0.42, s * 0.42)
        painter.setBrush(painter.pen().color())
        painter.drawEllipse(QPointF(s / 2.0, s * 0.30), s * 0.055, s * 0.055)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(QPointF(s / 2.0, s * 0.45), QPointF(s / 2.0, s * 0.69))
    elif kind == "power":
        painter.drawArc(QRectF(s * 0.15, s * 0.17, s * 0.70, s * 0.70),
                        int(60 * 16), int(240 * 16))
        painter.drawLine(QPointF(s / 2.0, s * 0.10), QPointF(s / 2.0, s * 0.47))
    elif kind == "monitor":
        _rounded(painter, s * 0.10, s * 0.16, s * 0.80, s * 0.54, s * 0.11)
        painter.drawLine(QPointF(s / 2.0, s * 0.70), QPointF(s / 2.0, s * 0.82))
        painter.drawLine(QPointF(s * 0.33, s * 0.84), QPointF(s * 0.67, s * 0.84))
    elif kind == "chat":
        _rounded(painter, s * 0.10, s * 0.15, s * 0.80, s * 0.55, s * 0.16)
        painter.drawPolyline(
            QPointF(s * 0.31, s * 0.70),
            QPointF(s * 0.31, s * 0.87),
            QPointF(s * 0.50, s * 0.70),
        )
    elif kind == "copy":
        _rounded(painter, s * 0.14, s * 0.14, s * 0.50, s * 0.50, s * 0.12)
        _rounded(painter, s * 0.36, s * 0.36, s * 0.50, s * 0.50, s * 0.12)
    elif kind == "refresh":
        painter.drawArc(QRectF(s * 0.16, s * 0.16, s * 0.68, s * 0.68),
                        int(40 * 16), int(290 * 16))
        painter.drawPolyline(
            QPointF(s * 0.52, s * 0.10),
            QPointF(s * 0.72, s * 0.20),
            QPointF(s * 0.54, s * 0.34),
        )
    elif kind == "folder":
        painter.drawPolyline(
            QPointF(s * 0.12, s * 0.78),
            QPointF(s * 0.12, s * 0.24),
            QPointF(s * 0.42, s * 0.24),
            QPointF(s * 0.51, s * 0.36),
            QPointF(s * 0.88, s * 0.36),
            QPointF(s * 0.88, s * 0.78),
            QPointF(s * 0.12, s * 0.78),
        )
    elif kind == "home":
        painter.drawPolyline(
            QPointF(s * 0.12, s * 0.50),
            QPointF(s * 0.50, s * 0.16),
            QPointF(s * 0.88, s * 0.50),
        )
        painter.drawPolyline(
            QPointF(s * 0.23, s * 0.45),
            QPointF(s * 0.23, s * 0.85),
            QPointF(s * 0.77, s * 0.85),
            QPointF(s * 0.77, s * 0.45),
        )
    elif kind == "camera":
        _rounded(painter, s * 0.10, s * 0.28, s * 0.80, s * 0.54, s * 0.14)
        painter.drawPolyline(
            QPointF(s * 0.34, s * 0.28),
            QPointF(s * 0.40, s * 0.16),
            QPointF(s * 0.60, s * 0.16),
            QPointF(s * 0.66, s * 0.28),
        )
        painter.drawEllipse(QPointF(s / 2.0, s * 0.55), s * 0.16, s * 0.16)
    elif kind == "search":
        painter.drawEllipse(QPointF(s * 0.44, s * 0.44), s * 0.26, s * 0.26)
        painter.drawLine(QPointF(s * 0.63, s * 0.63), QPointF(s * 0.86, s * 0.86))
    elif kind == "star":
        points = []
        for i in range(10):
            radius = s * 0.40 if i % 2 == 0 else s * 0.17
            angle = math.radians(-90 + i * 36)
            points.append(
                QPointF(s / 2.0 + math.cos(angle) * radius, s / 2.0 + math.sin(angle) * radius)
            )
        painter.drawPolygon(QPolygonF(points))
    elif kind == "log":
        painter.drawLine(QPointF(s * 0.20, s * 0.20), QPointF(s * 0.80, s * 0.20))
        painter.drawLine(QPointF(s * 0.20, s * 0.44), QPointF(s * 0.80, s * 0.44))
        painter.drawLine(QPointF(s * 0.20, s * 0.68), QPointF(s * 0.60, s * 0.68))
    elif kind == "phone":
        _rounded(painter, s * 0.28, s * 0.10, s * 0.44, s * 0.80, s * 0.08)
        painter.drawEllipse(QPointF(s / 2.0, s * 0.78), s * 0.045, s * 0.045)
    else:
        return False
    return True


def glyph(name: str, size: int = 18, color: str | None = None) -> QIcon:
    """
    程序化小图标。

    color 缺省时取当前主题的次级文字色，因此切主题需要重取 ——
    窗口可用 theme.register_refresh() 登记刷新回调。
    """
    ink = color or theme.color("text_2")
    key = (name, size, ink)
    cached = _glyph_cache.get(key)
    if cached is not None:
        return cached

    pixmap, painter = _new_canvas(size)
    pen = QPen(QColor(ink), max(1.3, size * 0.095))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    if not _draw_glyph(name, painter, float(size)):
        painter.end()
        return QIcon()

    painter.end()
    icon = QIcon(pixmap)
    _glyph_cache[key] = icon
    return icon


def eye_icon(opened: bool, color: str | None = None) -> QIcon:
    """
    密码可见性切换的小眼睛图标（程序化绘制，无需额外素材）。

    opened=True  睁眼：当前明文可见
    opened=False 闭眼加斜杠：当前已打码
    """
    ink = color or theme.color("text_2")
    key = (opened, ink)
    if key in _eye_cache:
        return _eye_cache[key]

    pixmap, painter = _new_canvas(18)
    paint_color = QColor(ink)
    pen = QPen(paint_color, 1.4)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(2, 5, 14, 8)
    painter.setBrush(paint_color)
    painter.drawEllipse(QPointF(9, 9), 2.2, 2.2)

    if not opened:
        slash = QPen(QColor(theme.color("danger")), 1.5)
        slash.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(slash)
        painter.drawLine(4, 4, 14, 14)

    painter.end()

    icon = QIcon(pixmap)
    _eye_cache[key] = icon
    return icon
