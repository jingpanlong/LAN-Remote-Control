"""
主题与设计系统

职责：
    1. 提供「深色 / 浅色」两套设计令牌（颜色、圆角、间距的取值来源）；
    2. 用 QPainter 现场生成 QSS 需要的小图形（勾选标记、下拉箭头、微调箭头），
       既不用额外素材文件，也能保证深浅色切换时图形颜色跟着变；
    3. 读取 ui/style.qss 模板，把 {{token}} 占位符替换成当前主题的色值 / 图标
       路径后，套用到 QApplication —— 这样所有窗口（主窗口 / 设置 / 关于 /
       聊天 / 文件 / 远程）风格天然一致，不会各写一套。

设计约定：
    - 颜色一律写死在 PALETTES 里，不要在窗口代码里写死 hex，否则切主题会花。
    - QSS 模板里的 {{...}} 必须在交给 Qt 之前全部替换掉，
      否则整张样式表会解析失败（Qt 遇到非法值会整体丢弃）。
    - 需要跟随主题重绘的图标，用 register_refresh() 登记回调，切主题时自动刷新。
"""

from __future__ import annotations

import logging
import os
import re
import weakref
from typing import Callable

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QApplication, QWidget

import config

logger = logging.getLogger("LAN_REMOTE")

MODE_DARK = "dark"
MODE_LIGHT = "light"
MODE_ORDER = (MODE_DARK, MODE_LIGHT)
MODE_LABELS = {MODE_DARK: "深色", MODE_LIGHT: "浅色"}
DEFAULT_MODE = MODE_DARK
SETTING_KEY = "ui_theme"

# ---------------------------------------------------------------------- 令牌
# 命名约定：bg 底 / surface 卡片 / border 描边 / text 文字 / 状态色三件套
# （正常态、hover 态、柔和底色），grad_* 为渐变。
PALETTES: dict[str, dict[str, str]] = {
    MODE_DARK: {
        "bg": "#0f1115",
        "bg_alt": "#14171d",
        "surface": "#171a21",
        "surface_2": "#1c2029",
        "surface_3": "#232833",
        "border": "#262b36",
        "border_strong": "#333a48",
        "divider": "#2a3040",

        "text": "#e9edf4",
        "text_2": "#a3adbe",
        "text_3": "#6e7889",

        "accent": "#4d8dff",
        "accent_hover": "#6ba1ff",
        "accent_press": "#3a7ae8",
        "accent_soft": "#1b2438",
        "accent_border": "#2f4a7d",
        "on_accent": "#ffffff",
        "grad_primary": "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
                        "stop:0 #4d8dff, stop:1 #7b6cff)",

        "success": "#1fa463",
        "success_hover": "#29bd76",
        "success_press": "#178a52",
        "success_soft": "#14291f",
        "success_border": "#245c40",
        "success_text": "#63e0a2",
        "grad_success": "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
                        "stop:0 #1fa463, stop:1 #2bc27c)",

        "danger": "#e5484d",
        "danger_hover": "#f2595e",
        "danger_soft": "#2b1a1d",
        "danger_border": "#6b2b30",
        "danger_text": "#ff8b8f",

        "warn": "#e0a13c",
        "warn_soft": "#2a2216",
        "warn_border": "#63501f",
        "warn_text": "#ffc978",

        "chip_bg": "#1c2029",
        "chip_border": "#2f3542",
        "tip_bg": "#1c2029",
        "tip_text": "#dbe2ee",

        "rail_bg": "#14171d",
        "log_bg": "#101319",
        "log_border": "#232833",
        "screen_bg": "#0b0d11",
        "screen_text": "#5d6775",
        "hud_bg": "#12151b",
        "hud_border": "#232833",

        "scroll": "#333a48",
        "scroll_hover": "#454e60",
        "selection": "#2b4a80",
        # 侧边装饰条 / 强调竖线
        "accent_rail": "#4d8dff",
    },
    MODE_LIGHT: {
        "bg": "#f3f5f9",
        "bg_alt": "#eceff5",
        "surface": "#ffffff",
        "surface_2": "#f6f8fb",
        "surface_3": "#edf1f7",
        "border": "#e3e7ef",
        "border_strong": "#cfd6e2",
        "divider": "#e8ecf3",

        "text": "#1b2230",
        "text_2": "#5c6679",
        "text_3": "#8b94a5",

        "accent": "#2f6bff",
        "accent_hover": "#4a80ff",
        "accent_press": "#245ae0",
        "accent_soft": "#e9f0ff",
        "accent_border": "#bcd2ff",
        "on_accent": "#ffffff",
        "grad_primary": "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
                        "stop:0 #2f6bff, stop:1 #6a5cff)",

        "success": "#12a15e",
        "success_hover": "#17b76b",
        "success_press": "#0d864c",
        "success_soft": "#e6f7ee",
        "success_border": "#b6e5cd",
        "success_text": "#0d7a48",
        "grad_success": "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
                        "stop:0 #12a15e, stop:1 #22bd76)",

        "danger": "#e5484d",
        "danger_hover": "#f2595e",
        "danger_soft": "#fdecec",
        "danger_border": "#f6c9cb",
        "danger_text": "#b3272c",

        "warn": "#c9812a",
        "warn_soft": "#fdf3e3",
        "warn_border": "#f0d5a8",
        "warn_text": "#8a5a12",

        "chip_bg": "#f0f3f9",
        "chip_border": "#dde3ee",
        "tip_bg": "#1b2230",
        "tip_text": "#f2f5fa",

        "rail_bg": "#ffffff",
        "log_bg": "#fbfcfe",
        "log_border": "#e3e7ef",
        # 画面区在浅色主题下同样保持深色底：远端画面多为亮色，深底更好看
        "screen_bg": "#171a21",
        "screen_text": "#6e7889",
        "hud_bg": "#ffffff",
        "hud_border": "#e3e7ef",

        "scroll": "#c7cfdd",
        "scroll_hover": "#adb8cb",
        "selection": "#cfe0ff",
        "accent_rail": "#2f6bff",
    },
}

_TOKEN_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

# 需要按主题变色的图标：(名称, 取色令牌, 尺寸)
_ASSET_SPECS: dict[str, tuple[str, int]] = {
    "check": ("on_accent", 18),
    "dash": ("on_accent", 18),
    "chevron": ("text_2", 14),
    "arrow_up": ("text_2", 10),
    "arrow_down": ("text_2", 10),
}

_stylesheet_cache: dict[str, str] = {}
_asset_cache: dict[tuple[str, str], str] = {}
_unresolved: list[str] = []
_refresh_refs: list = []
_active_mode: str | None = None


# ====================================================================== 模式
def current_mode() -> str:
    """当前主题模式：优先取运行期设置，其次取持久化设置，最后回退默认。"""
    if _active_mode in PALETTES:
        return _active_mode
    value = config.get_setting(SETTING_KEY)
    return value if value in PALETTES else DEFAULT_MODE


def set_mode(mode: str, persist: bool = True) -> str:
    """
    切换主题。persist=False 只改运行期状态（自检 / 预览用，不写用户设置）。

    返回实际生效的模式。
    """
    global _active_mode
    mode = mode if mode in PALETTES else DEFAULT_MODE
    _active_mode = mode
    if persist:
        try:
            config.set_setting(SETTING_KEY, mode)
        except OSError:
            logger.warning("保存主题设置失败", exc_info=True)
    return mode


def palette(mode: str | None = None) -> dict[str, str]:
    """取指定（或当前）主题的色板副本。"""
    return dict(PALETTES.get(mode or current_mode(), PALETTES[DEFAULT_MODE]))


def color(token: str, mode: str | None = None) -> str:
    """按令牌名取色；未知令牌返回兜底色而不是抛异常（界面不该因此崩掉）。"""
    return palette(mode).get(token, "#888888")


# ====================================================================== 图形
def _asset_dir() -> str:
    path = os.path.join(config.CACHE_DIR, "theme")
    os.makedirs(path, exist_ok=True)
    return path


def _paint(kind: str, color: QColor, size: int, path: str) -> bool:
    """把一个小图形画到 PNG；失败返回 False（调用方降级为不显示图形）。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(color, max(1.4, size * 0.13))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    if kind == "check":
        painter.drawPolyline(
            QPointF(size * 0.26, size * 0.53),
            QPointF(size * 0.43, size * 0.70),
            QPointF(size * 0.75, size * 0.31),
        )
    elif kind == "dash":
        painter.drawLine(
            QPointF(size * 0.28, size * 0.5), QPointF(size * 0.72, size * 0.5)
        )
    elif kind == "chevron":
        painter.drawPolyline(
            QPointF(size * 0.27, size * 0.41),
            QPointF(size * 0.50, size * 0.64),
            QPointF(size * 0.73, size * 0.41),
        )
    elif kind == "arrow_up":
        painter.drawPolyline(
            QPointF(size * 0.26, size * 0.64),
            QPointF(size * 0.50, size * 0.36),
            QPointF(size * 0.74, size * 0.64),
        )
    elif kind == "arrow_down":
        painter.drawPolyline(
            QPointF(size * 0.26, size * 0.36),
            QPointF(size * 0.50, size * 0.64),
            QPointF(size * 0.74, size * 0.36),
        )
    else:
        painter.end()
        return False

    painter.end()

    try:
        return bool(pixmap.save(path, "PNG"))
    except Exception:  # noqa: BLE001 - 保存失败只影响图标，不该中断界面
        logger.warning("生成主题图形失败：%s", path, exc_info=True)
        return False


def asset_url(name: str, mode: str | None = None) -> str:
    """
    返回 QSS 用的 `url(...)`（或 `none` 表示不显示图形）。

    文件名带颜色值，因此深浅色各自缓存、互不覆盖；同色图形天然复用。
    """
    spec = _ASSET_SPECS.get(name)
    if spec is None:
        return "none"

    token, size = spec
    value = color(token, mode)
    key = (name, value)
    cached = _asset_cache.get(key)
    if cached is not None:
        return cached

    path = os.path.join(_asset_dir(), f"{name}_{value.lstrip('#')}.png")
    if not os.path.exists(path) and not _paint(name, QColor(value), size, path):
        result = "none"
    else:
        result = "url(" + path.replace("\\", "/") + ")"

    _asset_cache[key] = result
    return result


# ====================================================================== 样式表
def build_stylesheet(mode: str | None = None) -> str:
    """渲染 QSS 模板（带缓存）。模板缺失时返回空串，界面回退到 Qt 默认样式。"""
    mode = mode if mode in PALETTES else current_mode()
    if mode in _stylesheet_cache:
        return _stylesheet_cache[mode]

    if not os.path.exists(config.STYLE_FILE):
        logger.warning("样式文件不存在：%s", config.STYLE_FILE)
        return ""

    try:
        with open(config.STYLE_FILE, "r", encoding="utf-8") as fp:
            template = fp.read()
    except OSError as exc:
        logger.warning("读取样式文件失败：%s", exc)
        return ""

    tokens = palette(mode)

    def _replace(match: re.Match) -> str:
        name = match.group(1)
        if name in tokens:
            return tokens[name]
        if name.startswith("asset_"):
            return asset_url(name[len("asset_"):], mode)
        _unresolved.append(name)
        return "transparent"

    qss = _TOKEN_RE.sub(_replace, template)
    if _unresolved:
        logger.warning("样式模板存在未知占位符：%s", ", ".join(sorted(set(_unresolved))))
    _stylesheet_cache[mode] = qss
    return qss


def unresolved_tokens() -> list[str]:
    """上次渲染时未识别的占位符（自检用）。"""
    return sorted(set(_unresolved))


def apply(target=None, mode: str | None = None) -> str:
    """
    套用主题。默认作用于 QApplication —— 全局统一，连没有父窗口的独立窗口也能吃到样式。

    返回实际生效的模式。
    """
    mode = mode if mode in PALETTES else current_mode()
    app = target if target is not None else QApplication.instance()
    if app is None:
        logger.debug("QApplication 尚未创建，主题将在创建后应用")
        return mode

    qss = build_stylesheet(mode)
    if qss:
        app.setStyleSheet(qss)
    _refresh_icons()
    return mode


def register_refresh(callback: Callable[[], None]) -> None:
    """
    登记「切主题时需要重绘」的回调（用于程序化绘制的图标）。

    用弱引用保存，窗口被销毁后自动失效，不会造成内存泄漏。
    """
    try:
        ref = weakref.WeakMethod(callback) if _is_bound(callback) else weakref.ref(callback)
    except TypeError:
        return
    _refresh_refs.append(ref)


def _is_bound(callback) -> bool:
    return getattr(callback, "__self__", None) is not None


def _refresh_icons() -> None:
    alive = []
    for ref in _refresh_refs:
        callback = ref()
        if callback is None:
            continue
        alive.append(ref)
        try:
            callback()
        except Exception:  # noqa: BLE001 - 图标刷新失败不该影响主题切换
            logger.debug("主题图标刷新回调失败", exc_info=True)
    _refresh_refs[:] = alive


def clear_cache() -> None:
    """清空样式与图形缓存（自检 / 调试用）。"""
    _stylesheet_cache.clear()
    _asset_cache.clear()
    _unresolved.clear()


# ====================================================================== 工具
def set_prop(widget: QWidget, name: str, value) -> None:
    """
    设置动态属性并立即重新计算样式。

    Qt 的属性选择器（`#id[state="on"]`）不会自动随属性变化重绘，必须手动
    unpolish/polish 一次，否则改了属性样式不生效。
    """
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)
    widget.update()
