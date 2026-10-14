"""
客户端 <-> Qt 界面 的桥梁

网络代码运行在独立线程的 asyncio 事件循环中，界面运行在 Qt 主线程。
两者之间**只通过 Qt 信号**通信，绝不跨线程直接操作控件，
这样既避免了崩溃，也消除了反复 asyncio.run() 造成的事件循环绑定错误。
"""

from __future__ import annotations

import logging
import threading

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QImage

from core.client import RemoteClient
from core.loop_runner import AsyncLoopRunner

logger = logging.getLogger("LAN_REMOTE")


class ClientBridge(QObject):
    """把 RemoteClient 的事件转发为 Qt 信号。"""

    # (画面, 远端宽, 远端高, 帧率, 延迟ms)
    screen_ready = pyqtSignal(QImage, int, int, float, float)
    # (文本, 级别)
    status_changed = pyqtSignal(str, str)
    chat_received = pyqtSignal(str, str)
    connected_changed = pyqtSignal(bool)
    # 收到对端剪贴板内容
    clipboard_received = pyqtSignal(str)
    # 对端显示器列表变化：(monitors, current_index)
    monitor_list_changed = pyqtSignal(list, int)
    # 对端画质档位确认：(profile, fps, quality, max_width)
    quality_state_changed = pyqtSignal(str, int, int, int)
    # 文件传输事件：(event, payload)
    #   event = "offer" / "progress" / "done"
    #   payload 含 direction(in/out)、name、received、total、done、ok、path、message
    file_event = pyqtSignal(str, dict)
    # 首次连接失败（未成功过即失败）：(kind, reason)
    # kind = "auth"（ID/密码不对）/ "offline"（对方不在线）/ "timeout" / "closed"
    connect_failed = pyqtSignal(str, str)

    def __init__(self, port: int = None, parent=None):
        super().__init__(parent)
        self._runner = AsyncLoopRunner("ClientLoop")
        self._client = RemoteClient(port=port)

        self._client.on_screen = self._on_screen
        self._client.on_status = self._on_status
        self._client.on_chat = self._on_chat
        self._client.on_state = self._on_state
        self._client.on_clipboard = self._on_clipboard
        self._client.on_monitor_list = self._on_monitor_list
        self._client.on_quality_state = self._on_quality_state
        self._client.on_file_event = self._on_file_event
        self._client.on_connect_failed = self._on_connect_failed

        self._frame_pending = False
        self._frame_lock = threading.Lock()
        self._started = False

    # ================================================================== 属性
    @property
    def client(self) -> RemoteClient:
        return self._client

    @property
    def connected(self) -> bool:
        return self._client.authenticated

    def set_port(self, port: int) -> None:
        """在发起连接前设置目标端口（服务端可能因端口占用而换端口）。"""
        try:
            self._client.port = int(port)
        except (TypeError, ValueError):
            pass

    # ================================================================== 启动 / 停止
    def start(self) -> bool:
        if not self._started:
            self._started = self._runner.start()
            if not self._started:
                logger.error("客户端事件循环启动失败")
        return self._started

    def shutdown(self) -> None:
        """程序退出时调用：先停客户端，再停事件循环。"""
        try:
            self._runner.fire_and_forget(self._client.close())
        except Exception:
            pass
        self._runner.stop()

    # ================================================================== 会话控制
    def start_session(self, ip: str, device_id: str, password: str, mode: str = "password") -> bool:
        """
        连接并保持（断线自动重连）。mode = password / approval。

        若该桥接器已在会话中（重复连接同一目标），先优雅停掉旧会话再发起，
        避免出现两个 run() 循环互相抢连接。
        """
        if not self.start():
            self.status_changed.emit("客户端事件循环未就绪", "error")
            return False

        async def _restart() -> None:
            await self._client.close()
            await self._client.run(ip, device_id, password, mode)

        self._runner.fire_and_forget(_restart())
        return True

    def disconnect(self) -> None:
        """断开当前会话（不会关闭事件循环，可再次连接）。"""
        self._runner.fire_and_forget(self._client.close())

    # ================================================================== 输入转发
    def send_mouse_move(self, x: float, y: float) -> None:
        self._runner.fire_and_forget(self._client.send_mouse_move(x, y))

    def send_mouse_button(self, button: str, action: str, count: int = 1) -> None:
        self._runner.fire_and_forget(self._client.send_mouse_click(button, action, count))

    def send_mouse_scroll(self, value: int) -> None:
        self._runner.fire_and_forget(self._client.send_mouse_scroll(value))

    def send_key(self, key: str, action: str = "press") -> None:
        self._runner.fire_and_forget(self._client.send_key(key, action))

    def send_chat(self, text: str) -> None:
        self._runner.fire_and_forget(self._client.send_chat(text))

    def send_clipboard(self, text: str) -> None:
        self._runner.fire_and_forget(self._client.send_clipboard(text))

    # ================================================================== 高级功能
    def push_clipboard(self, text: str) -> None:
        """把本机剪贴板推送到对端。"""
        self._runner.fire_and_forget(self._client.push_clipboard(text))

    def fetch_clipboard(self) -> None:
        """请求对端把它的剪贴板发过来（内容经 clipboard_received 信号返回）。"""
        self._runner.fire_and_forget(self._client.fetch_clipboard())

    def request_monitor_list(self) -> None:
        self._runner.fire_and_forget(self._client.request_monitor_list())

    def select_monitor(self, index: int) -> None:
        self._runner.fire_and_forget(self._client.select_monitor(index))

    def set_quality_profile(self, profile: str) -> None:
        """请求被控端切换画质档位。"""
        self._runner.fire_and_forget(self._client.set_quality_profile(profile))

    def set_screen_enabled(self, enabled: bool) -> None:
        """暂停 / 恢复接收画面（服务端会停止采集与推流）。"""
        self._runner.fire_and_forget(self._client.request_screen(enabled))

    # ================================================================== 文件传输
    def send_files(self, paths) -> int:
        """
        把若干本地文件发给对端，返回入队数量。

        进度与结果通过 file_event 信号回传（在 Qt 主线程里处理）。
        """
        count = 0
        for path in paths or ():
            text = str(path or "").strip()
            if not text:
                continue
            self._runner.fire_and_forget(self._client.send_file(text))
            count += 1
        return count

    def send_file(self, path: str) -> bool:
        return self.send_files([path]) == 1

    def file_transfers(self) -> list[dict]:
        """当前进行中的文件传输（只读快照）。"""
        try:
            return self._client.file_transfers()
        except Exception:
            logger.debug("读取文件传输列表失败", exc_info=True)
            return []

    @property
    def download_dir(self) -> str:
        """本机接收文件的保存目录。"""
        return self._client.download_dir

    def reconnect(self) -> None:
        """按上次的参数重新发起会话（断线重连由客户端自身负责）。"""
        ip = self._client.server_ip
        if not ip:
            self.status_changed.emit("尚未连接过任何设备", "warning")
            return
        self.start_session(
            ip,
            self._client.device_id or "",
            self._client.password or "",
            self._client.auth_mode,
        )

    def stats(self) -> dict:
        """当前会话的实时统计（跨线程读取，均为原子数值）。"""
        client = self._client
        return {
            "fps": float(client.last_fps),
            "delay": float(client.last_delay),
            "bytes_per_sec": float(client.last_bytes_per_sec),
            "connected": bool(client.authenticated),
            "server_device_id": client.server_device_id or "",
            "mode": client.auth_mode,
            "ip": client.server_ip or "",
            "port": int(client.port),
        }

    # ================================================================== 背压
    def frame_consumed(self) -> None:
        """界面绘制完一帧后调用，允许投递下一帧。"""
        with self._frame_lock:
            self._frame_pending = False

    # ================================================================== 回调实现
    def _on_screen(self, data: bytes, width: int, height: int, fps: float) -> None:
        """在事件循环线程中被调用。"""
        with self._frame_lock:
            if self._frame_pending:
                # 上一帧尚未绘制完成：丢弃本帧，形成天然背压
                return
            self._frame_pending = True

        image = QImage.fromData(data, b"JPG")
        if image.isNull():
            with self._frame_lock:
                self._frame_pending = False
            return

        # 服务端未上报尺寸时回退到图片自身尺寸
        real_width = width or image.width()
        real_height = height or image.height()
        self.screen_ready.emit(image, real_width, real_height, float(fps or 0), float(self._client.last_delay))

    def _on_status(self, text: str, level: str = "info") -> None:
        self.status_changed.emit(text, level)

    def _on_chat(self, text: str, sender: str) -> None:
        self.chat_received.emit(text, sender)

    def _on_clipboard(self, text: str) -> None:
        self.clipboard_received.emit(text)

    def _on_monitor_list(self, monitors: list, current: int) -> None:
        self.monitor_list_changed.emit(list(monitors), int(current))

    def _on_quality_state(self, profile: str, fps: int, quality: int, max_width: int) -> None:
        self.quality_state_changed.emit(str(profile), int(fps), int(quality), int(max_width))

    def _on_state(self, connected: bool) -> None:
        self.connected_changed.emit(connected)

    def _on_file_event(self, event: str, payload: dict) -> None:
        self.file_event.emit(str(event), dict(payload or {}))

    def _on_connect_failed(self, kind: str, reason: str) -> None:
        self.connect_failed.emit(str(kind), str(reason))

    def fail_info(self) -> dict:
        """最近一次连接失败的分类与原因（供界面弹窗）。"""
        return {
            "kind": str(self._client.last_fail_kind or ""),
            "reason": str(self._client.last_fail_reason or ""),
        }
