"""
文件传输面板

控制端与被控端共用同一个面板：
    - 往对端发文件（选择 / 拖拽），逐文件显示进度、大小与结果；
    - 对端发来的文件同样出现在列表里，接收完成会提示保存路径。

真正的传输逻辑在 core/file_transfer.py + core/server.py + core/client.py，
这里只负责交互与展示，并且**只通过桥接器的信号**更新界面（不跨线程动控件）。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.file_transfer import FileTransfer
from ui import theme
from ui.icons import app_icon, glyph

logger = logging.getLogger("LAN_REMOTE")

COL_NAME, COL_SIZE, COL_DIRECTION, COL_PROGRESS, COL_STATUS = range(5)
PROGRESS_SCALE = 1000


class FileWindow(QWidget):
    """文件传输面板。"""

    def __init__(self, bridge=None, parent=None, server=None):
        super().__init__(parent)
        self._bridge = bridge
        # 与控制端 bridge 相对：本机作为被控端时用它把文件发给控制端。
        # 二者同时可用时以控制端连接为准（与控制逻辑保持一致）。
        self._server = server

        self.setWindowTitle("文件传输")
        self.setWindowIcon(app_icon())
        self.resize(720, 460)
        self.setAcceptDrops(True)

        # transfer_id -> 行号 / 是否已结束
        self._rows: dict[str, int] = {}
        self._finished: dict[str, bool] = {}

        self._build_ui()
        self._bind_bridge()
        theme.register_refresh(self._refresh_row_colors)

    # ================================================================== 界面
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(10)

        self.hint = QLabel(self._hint_text())
        self.hint.setObjectName("fileHint")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        self.table = QTableWidget(0, 5)
        self.table.setObjectName("transferTable")
        self.table.setHorizontalHeaderLabels(["文件", "大小", "方向", "进度", "状态"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_SIZE, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_DIRECTION, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_PROGRESS, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setColumnWidth(COL_PROGRESS, 150)
        layout.addWidget(self.table, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        row = QHBoxLayout()
        row.setSpacing(8)

        choose = QPushButton("选择文件")
        choose.setObjectName("connectButton")
        choose.setIcon(glyph("folder", 15))
        choose.setCursor(Qt.CursorShape.PointingHandCursor)
        choose.clicked.connect(self.choose_files)

        open_dir = QPushButton("打开保存目录")
        open_dir.setObjectName("ghostButton")
        open_dir.setCursor(Qt.CursorShape.PointingHandCursor)
        open_dir.clicked.connect(self.open_download_dir)

        clear = QPushButton("清空已完成")
        clear.setObjectName("ghostButton")
        clear.setCursor(Qt.CursorShape.PointingHandCursor)
        clear.clicked.connect(self.clear_finished)

        self.saved_label = QLabel(self._download_dir())
        self.saved_label.setObjectName("fileHint")

        row.addWidget(choose)
        row.addWidget(open_dir)
        row.addWidget(clear)
        row.addStretch(1)
        row.addWidget(self.saved_label)
        layout.addLayout(row)

    def _download_dir(self) -> str:
        if self._bridge is not None:
            try:
                path = self._bridge.download_dir
                if path:
                    return path
            except Exception:
                pass
        if self._server is not None:
            try:
                return str(self._server.download_dir or "")
            except Exception:
                pass
        return ""

    def _hint_text(self) -> str:
        if self.control_connected:
            return "把文件拖到这里，或点「选择文件」发送给对方；对端发来的文件会自动保存到本机。"
        if self.hosting_clients:
            return "正在被远程控制：点「选择文件」可把本机文件发给控制端。"
        return "尚未连接：连接远端（或等待对方接入）后即可双向传文件。"

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

    # ================================================================== 桥接
    def _bind_bridge(self) -> None:
        if self._bridge is None:
            return
        try:
            self._bridge.file_event.disconnect(self.on_file_event)
        except (TypeError, RuntimeError):
            pass
        self._bridge.file_event.connect(self.on_file_event)

    # ================================================================== 发送
    def choose_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "选择要发送的文件")
        if files:
            self.add_files(files)

    def add_files(self, paths) -> int:
        """
        把文件加入发送队列；未连接时只提示，不改动已有列表。

        控制端连接优先；否则走被控端服务（把本机文件发给正在控制我的那一端）。
        """
        items = [str(p) for p in (paths or ()) if p and os.path.isfile(str(p))]
        if not items:
            return 0

        sender = None
        if self.control_connected:
            sender = self._bridge.send_files
        elif self.hosting_clients:
            sender = self._server.send_files

        if sender is None:
            self.hint.setText("尚未连接到远端，无法发送文件")
            return 0

        count = sender(items)
        if count:
            self.hint.setText(f"已加入 {count} 个文件，开始发送…")
        else:
            self.hint.setText("文件未能发送，请确认对方仍在线")
        return count

    def set_progress(self, value: int) -> None:
        """设置总体进度（0~100）。"""
        self.progress.setValue(max(0, min(100, int(value))))

    # ================================================================== 事件
    def on_file_event(self, event: str, payload: dict) -> None:
        """
        处理一次文件传输事件（Qt 主线程槽函数）。

        控制端的 bridge.file_event 与被控端的 MainWindow.server_file_event
        都接到这里，两条链路共用同一套展示逻辑。
        """
        row = self._ensure_row(payload)
        total = int(payload.get("total") or 0)
        received = int(payload.get("received") or 0)
        ratio = (received / total) if total else (1.0 if payload.get("done") else 0.0)
        ratio = max(0.0, min(1.0, ratio))

        bar = self.table.cellWidget(row, COL_PROGRESS)
        if isinstance(bar, QProgressBar):
            bar.setValue(int(ratio * PROGRESS_SCALE))

        size_item = self.table.item(row, COL_SIZE)
        if size_item is not None:
            size_item.setText(
                f"{FileTransfer.format_size(received)} / {FileTransfer.format_size(total)}"
            )

        status_item = self.table.item(row, COL_STATUS)
        transfer_id = str(payload.get("transfer_id") or "")

        if payload.get("done"):
            ok = bool(payload.get("ok", True))
            self._finished[transfer_id] = True
            if status_item is not None:
                if ok:
                    status_item.setText("已完成")
                    status_item.setData(Qt.ItemDataRole.UserRole, "ok")
                else:
                    status_item.setText(payload.get("message") or "已中止")
                    status_item.setData(Qt.ItemDataRole.UserRole, "fail")
            self._set_last_message(payload, ok)
        else:
            if status_item is not None:
                status_item.setText(f"{ratio * 100:.0f}%")
                status_item.setData(Qt.ItemDataRole.UserRole, "running")

        self._refresh_row_colors()
        self._refresh_summary()

    def _set_last_message(self, payload: dict, ok: bool) -> None:
        name = payload.get("name") or "文件"
        direction = payload.get("direction")
        if not ok:
            self.hint.setText(f"{name} 传输失败：{payload.get('message') or '未知原因'}")
        elif direction == "out":
            self.hint.setText(f"已发送：{name}")
        else:
            path = payload.get("path") or ""
            self.hint.setText(f"已接收：{name}　保存到 {path}" if path else f"已接收：{name}")

    def _ensure_row(self, payload: dict) -> int:
        transfer_id = str(payload.get("transfer_id") or "")
        row = self._rows.get(transfer_id)
        if row is not None and row < self.table.rowCount():
            return row

        row = self.table.rowCount()
        self.table.insertRow(row)
        self._rows[transfer_id] = row

        self.table.setItem(row, COL_NAME, QTableWidgetItem(payload.get("name") or "未命名文件"))
        self.table.setItem(row, COL_SIZE, QTableWidgetItem("—"))

        direction = QTableWidgetItem("发送" if payload.get("direction") == "out" else "接收")
        direction.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(row, COL_DIRECTION, direction)

        bar = QProgressBar()
        bar.setObjectName("rowProgress")
        bar.setRange(0, PROGRESS_SCALE)
        bar.setValue(0)
        bar.setTextVisible(False)
        self.table.setCellWidget(row, COL_PROGRESS, bar)

        status = QTableWidgetItem("传输中")
        status.setData(Qt.ItemDataRole.UserRole, "running")
        self.table.setItem(row, COL_STATUS, status)

        self.table.scrollToBottom()
        return row

    def _refresh_row_colors(self) -> None:
        """
        给整行上色（主题切换后也要重刷）。

        注意：状态列的颜色必须用 item.setForeground() 设置 —— QSS 的
        `QTableWidget::item { color: ... }` 会盖掉它，所以这条规则不能写进 QSS，
        普通列的默认文字色也在这里一并给出。
        """
        state_colors = {
            "ok": theme.color("success_text"),
            "fail": theme.color("danger_text"),
            "running": theme.color("text_2"),
        }
        plain = QColor(theme.color("text_2"))
        name_color = QColor(theme.color("text"))

        for row in range(self.table.rowCount()):
            for column, color in ((COL_NAME, name_color), (COL_SIZE, plain), (COL_DIRECTION, plain)):
                item = self.table.item(row, column)
                if item is not None:
                    item.setForeground(color)

            item = self.table.item(row, COL_STATUS)
            if item is None:
                continue
            state = item.data(Qt.ItemDataRole.UserRole) or "running"
            item.setForeground(QColor(state_colors.get(state, state_colors["running"])))

    def _refresh_summary(self) -> None:
        active = [
            row for transfer_id, row in self._rows.items()
            if not self._finished.get(transfer_id) and row < self.table.rowCount()
        ]
        if not active:
            self.progress.setValue(100 if self.table.rowCount() else 0)
            return

        total = 0.0
        for row in active:
            bar = self.table.cellWidget(row, COL_PROGRESS)
            if isinstance(bar, QProgressBar):
                total += bar.value() / PROGRESS_SCALE
        self.progress.setValue(int(total / len(active) * 100))

    # ================================================================== 操作
    def refresh_hint(self) -> None:
        """刷新顶部提示（连接状态可能在窗口关闭期间变化过）。"""
        if not self.table.rowCount():
            self.hint.setText(self._hint_text())
        self.saved_label.setText(self._download_dir())

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.refresh_hint()

    def open_download_dir(self) -> None:
        """在系统文件管理器里打开保存目录。"""
        path = self._download_dir()
        if not path:
            return
        try:
            os.makedirs(path, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            logger.exception("打开保存目录失败")
            self.hint.setText(f"保存目录：{path}")

    def clear_finished(self) -> None:
        """清掉列表里的传输记录（进行中的会由后续事件重新出现）。"""
        self._rebuild_from_state()
        self.hint.setText("已清理传输记录")
        self._refresh_summary()

    def clear_list(self) -> None:
        """清空整个列表（兼容旧接口）。"""
        self._rebuild_from_state()
        self.progress.setValue(0)
        self.hint.setText(self._hint_text())

    def _rebuild_from_state(self) -> None:
        self.table.setRowCount(0)
        self._rows.clear()
        self._finished.clear()

    # ================================================================== 拖拽
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.add_files(paths)
            event.acceptProposedAction()
