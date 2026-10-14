"""
通用小控件

目前只有分段控制器：用于「选项少且固定」的场景（如显示模式三选一），
比下拉框更直观，也更符合现代桌面应用的观感。

样式统一由 ui/style.qss 的 #segmented / #segmentButton 控制，
这里只负责行为，不写死颜色。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QButtonGroup, QHBoxLayout, QPushButton, QWidget


class SegmentedControl(QWidget):
    """
    分段控制器（互斥胶囊按钮组）。

    用法：
        control = SegmentedControl((("fit", "适应窗口"), ("stretch", "拉伸铺满")))
        control.current_changed.connect(self._on_changed)
        control.set_current("stretch")          # 静默切换
        control.set_current("fit", notify=True) # 切换并触发信号
    """

    current_changed = pyqtSignal(str)

    def __init__(self, options, parent=None, expand: bool = False):
        super().__init__(parent)
        self.setObjectName("segmented")

        row = QHBoxLayout(self)
        row.setContentsMargins(3, 3, 3, 3)
        row.setSpacing(3)

        self._buttons: dict[str, QPushButton] = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        for key, label in options:
            button = QPushButton(label)
            button.setObjectName("segmentButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(lambda _checked=False, k=key: self._on_clicked(k))
            # expand=True 时按钮平铺撑满整行（用作标签页）；False 时按内容收缩（用作工具栏分段）
            row.addWidget(button, 1 if expand else 0)
            self._group.addButton(button)
            self._buttons[key] = button

        self._keys = [key for key, _ in options]
        self._current = self._keys[0] if self._keys else ""
        if self._current:
            self._buttons[self._current].setChecked(True)

    # ------------------------------------------------------------------ 行为
    def _on_clicked(self, key: str) -> None:
        if key == self._current:
            return
        self._current = key
        self.current_changed.emit(key)

    def current_key(self) -> str:
        return self._current

    def set_current(self, key: str, notify: bool = False) -> bool:
        """切换选中项；notify=True 时触发 current_changed。返回是否发生了变化。"""
        if key not in self._buttons:
            return False
        changed = key != self._current
        self._current = key
        self._buttons[key].setChecked(True)
        if notify and changed:
            self.current_changed.emit(key)
        return changed

    def set_enabled(self, enabled: bool) -> None:
        for button in self._buttons.values():
            button.setEnabled(enabled)

    def count(self) -> int:
        return len(self._buttons)

    def keys(self) -> list[str]:
        return list(self._keys)
