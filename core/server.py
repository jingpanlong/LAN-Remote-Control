"""
LAN Remote 服务端

职责：
    - WebSocket 服务（认证后才放行敏感指令）
    - 屏幕实时推流（仅推送给已认证且开启画面的客户端）
    - 鼠标 / 键盘控制执行
    - 聊天、剪贴板广播
    - 文件收发（分块 + MD5 校验）
    - 端口占用时自动向后寻找可用端口
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import logging
import os
import threading
import time
from dataclasses import dataclass, field

import websockets
from websockets.exceptions import ConnectionClosed

import config
from core.clipboard import Clipboard
from core.control import Control
from core.encoder import Encoder
from core.file_transfer import FileReceiver, FileSender, FileTransfer, stream_file
from core.protocol import MessageType, Protocol, ProtocolBuilder
from core.screen import ScreenCapture
from core.security import Credentials

logger = logging.getLogger("LAN_REMOTE")

MAX_LOGIN_ATTEMPTS = 5


@dataclass
class ClientSession:
    """单个客户端连接的状态。"""

    websocket: object
    address: str = ""
    authenticated: bool = False
    wants_screen: bool = True
    login_attempts: int = 0
    connected_at: float = field(default_factory=time.time)
    device_id: str = ""


class RemoteServer:
    """被控端服务。"""

    def __init__(
        self,
        device_id: str,
        credentials: Credentials,
        host: str = None,
        port: int = None,
        port_fallback: int = None,
        discovery=None,
        download_dir: str = None,
    ):
        if not isinstance(credentials, Credentials):
            # 兼容旧调用：直接传字符串密码
            plain = str(credentials or "")
            credentials = Credentials()
            if plain:
                credentials.set_password(plain)

        self.device_id = device_id
        self.credentials = credentials
        self.host = host or config.SERVER_HOST
        self.port = int(port or config.server_port())
        self.port_fallback = config.server_port_fallback() if port_fallback is None else int(port_fallback)

        # 实际监听端口（端口被占用时会与 self.port 不同）
        self.actual_port: int | None = None
        self.running = False

        self.screen = ScreenCapture()
        self.encoder = Encoder()

        # 推流节拍：缓存目标帧率，避免每轮循环都去读 settings.json
        self._target_fps = max(1, min(config.SCREEN_FPS_MAX, int(config.screen_fps())))
        self._frame_interval = 1.0 / self._target_fps
        # 推流统计（供被控端/控制端展示）
        self._sent_frames = 0
        self._dropped_frames = 0
        self._last_encode_ms = 0.0
        self._last_frame_bytes = 0
        self._stats_lock = threading.Lock()

        self._sessions: dict[object, ClientSession] = {}
        self._sessions_lock = asyncio.Lock()

        # 供 GUI 线程安全读取的已认证客户端计数
        self._count_lock = threading.Lock()
        self._authed_count = 0

        self._server = None
        self._screen_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        # 输入指令串行执行，保证顺序且不阻塞事件循环
        self._input_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="input")

        # 局域网设备发现（可选）：绑定成功后同步对外宣告的实际端口
        self.discovery = discovery

        # 授权模式回调：由 GUI 注入，info = {"device_id", "address", "respond"}
        self.on_authorize_request = None
        self.approval_timeout = float(config.APPROVAL_TIMEOUT)
        self._approval_pending = False

        # 状态回调（由 GUI 注入）：callback(text, level)
        self.on_status = None
        # 画质档位被远端修改时的回调：callback(profile, fps, quality, max_width)
        self.on_quality_changed = None
        # 文件传输事件回调（由 GUI 注入）：callback(event, payload)
        #   event: "offer" / "progress" / "done"
        self.on_file_event = None
        # 收到控制端聊天时的回调（由 GUI 注入）：callback(text, sender)
        # 与控制端不同，被控端既要显示对端消息，也要能主动回话
        self.on_chat = None
        # 有客户端认证成功时的回调（由 GUI 注入）：callback(device_id, address)
        # 供被控端弹「有设备接入」提醒
        self.on_client_connected = None
        # 已认证的客户端断开时的回调（由 GUI 注入）：callback(device_id, address)
        # 供被控端弹「控制端已断开连接」提醒
        self.on_client_disconnected = None

        # 文件传输会话（只在事件循环线程里读写，无需加锁）
        self._incoming_files: dict[str, FileReceiver] = {}
        self._outgoing_files: dict[str, FileSender] = {}
        self._file_reported: dict[str, int] = {}
        # 文件接收目录（默认 downloads，可注入以便自检隔离）
        self.download_dir = str(download_dir or config.download_dir())

    # ================================================================== 工具
    def _notify(self, text: str, level: str = "info") -> None:
        if level == "error":
            logger.error("[服务端] %s", text)
        elif level == "warning":
            logger.warning("[服务端] %s", text)
        else:
            logger.info("[服务端] %s", text)

        callback = self.on_status
        if callback is not None:
            try:
                callback(text, level)
            except Exception:
                logger.exception("状态回调执行异常")

    async def _send(self, websocket, message: str) -> None:
        """安全发送，失败静默（连接可能已断开）。"""
        try:
            await websocket.send(message)
        except (ConnectionClosed, RuntimeError, OSError):
            pass

    def _emit_chat(self, text: str, sender: str = "") -> None:
        """把收到的聊天转给 GUI（回调在事件循环线程执行，GUI 侧需转信号）。"""
        callback = self.on_chat
        if callback is None:
            return
        try:
            callback(str(text), str(sender))
        except Exception:
            logger.exception("聊天回调异常")

    def _emit_client_connected(self, device_id: str, address: str) -> None:
        """有客户端认证成功时通知 GUI（回调在事件循环线程执行）。"""
        callback = self.on_client_connected
        if callback is None:
            return
        try:
            callback(str(device_id), str(address))
        except Exception:
            logger.exception("客户端接入回调异常")

    def _emit_client_disconnected(self, device_id: str, address: str) -> None:
        """已认证的客户端断开时通知 GUI（回调在事件循环线程执行）。"""
        callback = self.on_client_disconnected
        if callback is None:
            return
        try:
            callback(str(device_id), str(address))
        except Exception:
            logger.exception("客户端断开回调异常")

    def _has_viewers(self) -> bool:
        return any(
            session.authenticated and session.wants_screen for session in list(self._sessions.values())
        )

    def _bump_authed(self, delta: int) -> None:
        with self._count_lock:
            self._authed_count = max(0, self._authed_count + delta)

    def client_count(self) -> int:
        """当前已认证客户端数量（线程安全）。"""
        with self._count_lock:
            return self._authed_count

    def _submit(self, coroutine) -> bool:
        """
        把协程投递到服务端事件循环（可从任意线程调用）。

        GUI 跑在 Qt 主线程，而所有网络 I/O 都必须在服务端的事件循环里执行，
        因此被控端主动发消息 / 发文件都要经由这里跨线程投递。
        投递失败时主动关闭协程，避免 "coroutine was never awaited" 告警。
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            try:
                coroutine.close()
            except Exception:
                pass
            return False
        try:
            asyncio.run_coroutine_threadsafe(coroutine, loop)
        except RuntimeError:
            try:
                coroutine.close()
            except Exception:
                pass
            return False
        return True

    # ================================================================== 连接处理
    async def handler(self, websocket) -> None:
        """每个客户端连接一个协程。"""
        try:
            address = str(getattr(websocket, "remote_address", ""))
        except Exception:
            address = ""

        session = ClientSession(websocket=websocket, address=address)
        self._sessions[websocket] = session
        logger.info("客户端接入：%s", address)

        try:
            async for message in websocket:
                await self._handle(websocket, session, message)
        except ConnectionClosed:
            pass
        except Exception:
            logger.exception("连接处理异常（%s）", address)
        finally:
            # pop(..., None) 避免重复移除导致 KeyError
            self._sessions.pop(websocket, None)
            if session.authenticated:
                self._bump_authed(-1)
                # 已认证的控制端离开：单独通知 GUI 弹「对方已断开」提醒
                self._emit_client_disconnected(session.device_id or address, address)
            # 连接断开：清理未完成的文件传输，避免残留半截文件
            self._abort_files()
            logger.info("客户端断开：%s", address)
            self._notify(f"客户端断开：{address}")

    def _abort_files(self) -> None:
        """中止所有进行中的文件传输（两端都会调用）。"""
        for receiver in list(self._incoming_files.values()):
            receiver.abort()
        self._incoming_files.clear()
        for sender in list(self._outgoing_files.values()):
            sender.cancel()
        self._outgoing_files.clear()
        self._file_reported.clear()

    async def _handle(self, websocket, session: ClientSession, message) -> None:
        """消息分发。"""
        msg_type = Protocol.get_type(message)

        if msg_type is None:
            await self._send(websocket, ProtocolBuilder.error("报文格式非法"))
            return

        body = Protocol.get_data(message)

        # ---------------------------------------------------------- 认证
        if msg_type == MessageType.LOGIN:
            await self._handle_login(websocket, session, body)
            return

        # 除登录外一律要求已认证，避免未授权控制
        if not session.authenticated:
            await self._send(websocket, ProtocolBuilder.error("尚未通过认证"))
            return

        # ---------------------------------------------------------- 心跳
        if msg_type == MessageType.HEARTBEAT:
            await self._send(websocket, Protocol.create(MessageType.HEARTBEAT_REPLY))
            return

        # ---------------------------------------------------------- 画面开关
        if msg_type == MessageType.SCREEN_REQUEST:
            session.wants_screen = True
            return
        if msg_type == MessageType.SCREEN_STOP:
            session.wants_screen = False
            return

        # ---------------------------------------------------------- 控制
        if msg_type in (
            MessageType.MOUSE_MOVE,
            MessageType.MOUSE_CLICK,
            MessageType.MOUSE_SCROLL,
            MessageType.KEYBOARD,
        ):
            await self._execute_control(msg_type, body)
            return

        # ---------------------------------------------------------- 剪贴板
        # 对端推来剪贴板：写入本机，并转发给其它已认证客户端（不回给发送者）
        if msg_type in (MessageType.CLIPBOARD, MessageType.CLIPBOARD_PUSH):
            await self._handle_clipboard_push(websocket, body)
            return
        # 对端索要剪贴板：读本机内容定向回发
        if msg_type == MessageType.CLIPBOARD_FETCH:
            await self._handle_clipboard_fetch(websocket)
            return

        # ---------------------------------------------------------- 显示器
        if msg_type == MessageType.MONITOR_LIST_REQUEST:
            await self._send(
                websocket,
                ProtocolBuilder.monitor_list(self.screen.monitors(), self.screen.monitor_index),
            )
            return
        if msg_type == MessageType.MONITOR_SELECT:
            await self._handle_monitor_select(body)
            return

        # ---------------------------------------------------------- 画质档位
        if msg_type == MessageType.QUALITY_SET:
            await self._handle_quality_set(body)
            return

        # ---------------------------------------------------------- 聊天
        if msg_type == MessageType.CHAT:
            await self._handle_chat(websocket, session, message, body)
            return

        # ---------------------------------------------------------- 文件传输
        if msg_type == MessageType.FILE_INFO:
            await self._handle_file_offer(websocket, body)
            return
        if msg_type == MessageType.FILE_DATA:
            await self._handle_file_chunk(websocket, body)
            return
        if msg_type == MessageType.FILE_FINISH:
            await self._handle_file_finish(websocket, body)
            return
        if msg_type == MessageType.FILE_CANCEL:
            await self._handle_file_cancel(body)
            return
        if msg_type == MessageType.FILE_PROGRESS:
            # 本机作为发送方时收到对端的进度回报
            self._handle_file_progress(body)
            return

        logger.debug("未处理的消息类型：%s", msg_type)

    # ================================================================== 聊天
    async def _handle_chat(self, websocket, session: ClientSession, message, body: dict) -> None:
        """
        收到控制端的聊天：先显示在本机界面，再转发给**其它**已认证客户端。

        不回给发送者，避免控制端看到自己刚发的那条重复显示。
        """
        text = str(body.get("text") or "").strip()
        if not text:
            return
        sender = str(body.get("sender") or "").strip() or session.address or "对端"
        self._notify(f"[聊天] {sender}：{text}")
        self._emit_chat(text, sender)
        await self._broadcast(message, predicate=self._is_authenticated, exclude=websocket)

    # ================================================================== 文件传输
    def _emit_file_event(self, event: str, payload: dict) -> None:
        """把文件传输事件转给 GUI（回调在事件循环线程执行，GUI 侧需转信号）。"""
        callback = self.on_file_event
        if callback is None:
            return
        try:
            callback(event, dict(payload))
        except Exception:
            logger.exception("文件传输事件回调异常")

    def _file_payload(
        self,
        receiver_or_sender,
        direction: str,
        done: bool = False,
        ok: bool = True,
        path: str = "",
        message: str = "",
    ) -> dict:
        return {
            "transfer_id": receiver_or_sender.transfer_id,
            "name": receiver_or_sender.name,
            "received": int(receiver_or_sender.received),
            "total": int(receiver_or_sender.total),
            "direction": direction,
            "done": bool(done),
            "ok": bool(ok),
            "path": path,
            "message": message,
        }

    async def _handle_file_offer(self, websocket, body: dict) -> None:
        """对端要发文件过来：先校验，再准备接收会话。"""
        transfer_id = str(body.get("transfer_id") or "").strip()
        if not transfer_id:
            await self._send(websocket, ProtocolBuilder.error("文件传输缺少 transfer_id"))
            return

        size = int(body.get("size") or 0)
        name = FileTransfer.safe_name(body.get("name"))

        if size > config.file_max_size():
            reason = f"文件超过体积上限（{FileTransfer.format_size(config.file_max_size())}）"
            await self._send(websocket, ProtocolBuilder.file_cancel(transfer_id, reason))
            self._notify(f"已拒绝 {name}：{reason}", "warning")
            self._emit_file_event(
                "done",
                {"transfer_id": transfer_id, "name": name, "received": 0, "total": size,
                 "direction": "in", "done": True, "ok": False, "path": "", "message": reason},
            )
            return

        # 同一 transfer_id 重复发起：丢弃旧会话
        stale = self._incoming_files.pop(transfer_id, None)
        if stale is not None:
            stale.abort()

        try:
            receiver = FileReceiver(body, save_dir=self.download_dir)
        except OSError as exc:
            await self._send(websocket, ProtocolBuilder.error(f"无法创建接收文件：{exc}"))
            return

        self._incoming_files[transfer_id] = receiver
        self._file_reported[transfer_id] = 0
        self._notify(f"开始接收文件：{name}（{FileTransfer.format_size(size)}）")
        self._emit_file_event("offer", self._file_payload(receiver, "in"))
        await self._send(websocket, ProtocolBuilder.file_progress(transfer_id, 0, size))

    async def _handle_file_chunk(self, websocket, body: dict) -> None:
        """收到一个数据块：写入并（按粒度）回报进度。"""
        transfer_id = str(body.get("transfer_id") or "")
        receiver = self._incoming_files.get(transfer_id)
        if receiver is None:
            return

        received = receiver.write(int(body.get("seq") or 0), str(body.get("chunk") or ""))

        if receiver.error:
            self._incoming_files.pop(transfer_id, None)
            self._file_reported.pop(transfer_id, None)
            receiver.abort()
            self._notify(f"接收 {receiver.name} 失败：{receiver.error}", "warning")
            await self._send(websocket, ProtocolBuilder.file_cancel(transfer_id, receiver.error))
            self._emit_file_event(
                "done",
                {"transfer_id": transfer_id, "name": receiver.name, "received": received,
                 "total": receiver.total, "direction": "in", "done": True, "ok": False,
                 "path": "", "message": receiver.error},
            )
            return

        # 进度回报做节流：每变化 FILE_PROGRESS_STEP 或总共只回一次收尾
        last = self._file_reported.get(transfer_id, 0)
        step = max(1, int(receiver.total * config.FILE_PROGRESS_STEP)) if receiver.total else 1
        if received - last >= step or received >= receiver.total:
            self._file_reported[transfer_id] = received
            await self._send(
                websocket, ProtocolBuilder.file_progress(transfer_id, received, receiver.total)
            )
            self._emit_file_event("progress", self._file_payload(receiver, "in"))

    async def _handle_file_finish(self, websocket, body: dict) -> None:
        """数据发完：校验 MD5 并落盘，最后把结果回给发送方。"""
        transfer_id = str(body.get("transfer_id") or "")
        receiver = self._incoming_files.pop(transfer_id, None)
        self._file_reported.pop(transfer_id, None)

        if receiver is None:
            await self._send(
                websocket,
                ProtocolBuilder.file_progress(
                    transfer_id, 0, 0, True, "", False, "未找到对应的传输会话"
                ),
            )
            return

        result = receiver.finish()
        if result["ok"]:
            self._notify(f"文件已保存：{result['path']}")
        else:
            self._notify(f"接收 {receiver.name} 失败：{result['message']}", "warning")

        await self._send(
            websocket,
            ProtocolBuilder.file_progress(
                transfer_id,
                int(result["size"]),
                receiver.total,
                True,
                result["path"],
                bool(result["ok"]),
                result["message"],
            ),
        )
        self._emit_file_event(
            "done",
            {
                "transfer_id": transfer_id,
                "name": result["name"] or receiver.name,
                "received": int(result["size"]),
                "total": receiver.total,
                "direction": "in",
                "done": True,
                "ok": bool(result["ok"]),
                "path": result["path"],
                "message": result["message"],
            },
        )

    async def _handle_file_cancel(self, body: dict) -> None:
        """任一方中止传输：清理接收会话与发送会话。"""
        transfer_id = str(body.get("transfer_id") or "")
        reason = str(body.get("reason") or "")

        receiver = self._incoming_files.pop(transfer_id, None)
        self._file_reported.pop(transfer_id, None)
        if receiver is not None:
            receiver.abort()
            self._notify(f"文件接收已中止：{receiver.name}{f'（{reason}）' if reason else ''}",
                         "warning")
            self._emit_file_event(
                "done",
                {"transfer_id": transfer_id, "name": receiver.name, "received": receiver.received,
                 "total": receiver.total, "direction": "in", "done": True, "ok": False,
                 "path": "", "message": reason or "已中止"},
            )

        sender = self._outgoing_files.pop(transfer_id, None)
        if sender is not None:
            sender.cancel()
            self._notify(f"文件发送已中止：{sender.name}{f'（{reason}）' if reason else ''}",
                         "warning")
            self._emit_file_event(
                "done",
                {"transfer_id": transfer_id, "name": sender.name, "received": sender.sent,
                 "total": sender.size, "direction": "out", "done": True, "ok": False,
                 "path": "", "message": reason or "已中止"},
            )

    def _handle_file_progress(self, body: dict) -> None:
        """本机发送文件时，对端回报的进度。"""
        transfer_id = str(body.get("transfer_id") or "")
        sender = self._outgoing_files.get(transfer_id)
        done = bool(body.get("done"))
        received = int(body.get("received") or 0)
        ok = bool(body.get("ok", True))
        message = str(body.get("message") or "")
        path = str(body.get("path") or "")
        total = sender.size if sender is not None else int(body.get("total") or 0)
        name = sender.name if sender is not None else transfer_id

        if done:
            self._outgoing_files.pop(transfer_id, None)

        if done:
            if ok:
                self._notify(f"文件已送达：{name} → {path or '对端'}")
            else:
                self._notify(f"文件发送失败：{name}（{message or '对端未接收'}）", "warning")

        self._emit_file_event(
            "done" if done else "progress",
            {
                "transfer_id": transfer_id,
                "name": name,
                "received": received,
                "total": total,
                "direction": "out",
                "done": done,
                "ok": ok,
                "path": path,
                "message": message,
            },
        )

    async def send_file(self, path: str, websocket=None) -> str | None:
        """
        把本机文件发给对端。

        websocket 为空表示广播给所有已认证客户端；返回 transfer_id，失败返回 None。
        """
        try:
            sender = FileSender(path, sender=self.device_id)
        except (FileNotFoundError, OSError) as exc:
            self._notify(f"无法读取文件：{exc}", "warning")
            return None

        if sender.size > config.file_max_size():
            self._notify(
                f"文件超过体积上限（{FileTransfer.format_size(config.file_max_size())}）", "warning"
            )
            return None

        if websocket is not None:
            targets = [websocket]
        else:
            targets = [
                session.websocket
                for session in list(self._sessions.values())
                if session.authenticated
            ]
        if not targets:
            self._notify("当前没有已连接的客户端，无法发送文件", "warning")
            return None

        self._outgoing_files[sender.transfer_id] = sender
        self._emit_file_event("offer", self._file_payload(sender, "out"))
        self._notify(f"开始发送文件：{sender.name}（{FileTransfer.format_size(sender.size)}）")

        async def _send_all(message: str) -> None:
            for ws in targets:
                await self._send(ws, message)

        await _send_all(sender.info_message())
        await stream_file(
            sender,
            _send_all,
            lambda s: self._emit_file_event("progress", self._file_payload(s, "out")),
        )
        await _send_all(sender.finish_message())

        self._emit_file_event("progress", self._file_payload(sender, "out"))
        return sender.transfer_id

    def file_transfers(self) -> list[dict]:
        """当前进行中的文件传输（供界面显示）。"""
        items = [self._file_payload(r, "in") for r in list(self._incoming_files.values())]
        items.extend(self._file_payload(s, "out") for s in list(self._outgoing_files.values()))
        return items

    # ================================================================== 被控端主动发送
    # 被控端本身没有「客户端连接」对象，所有出站动作都要跨线程投递到事件循环；
    # 这两个方法就是 GUI 唯一允许调用的入口（内部已处理线程安全与协程回收）。
    def send_chat(self, text: str, sender: str = "") -> bool:
        """被控端主动给所有已认证客户端发一条聊天（可从任意线程调用）。"""
        text = str(text or "").strip()
        if not text or self.client_count() <= 0:
            return False
        message = ProtocolBuilder.chat(text, sender or self.device_id or "被控端")
        return self._submit(self._broadcast(message, predicate=self._is_authenticated))

    def send_files(self, paths) -> int:
        """被控端主动把若干文件发给所有已认证客户端，返回入队数量（线程安全）。"""
        count = 0
        for path in paths or ():
            text = str(path or "").strip()
            if not text or not os.path.isfile(text):
                continue
            if self._submit(self.send_file(text)):
                count += 1
        return count

    # ================================================================== 剪贴板
    async def _handle_clipboard_push(self, websocket, body: dict) -> None:
        """把对端发来的剪贴板文本写入本机剪贴板。"""
        text = body.get("text")
        if not isinstance(text, str):
            return
        if Clipboard.set(text):
            preview = text.strip().replace("\n", " ")[:40]
            self._notify(f"已同步剪贴板到本机：{preview}{'…' if len(text) > 40 else ''}")
        else:
            self._notify("写入本机剪贴板失败", "warning")

        # 广播给其它观察者，让多客户端看到同一份剪贴板
        await self._broadcast(
            ProtocolBuilder.clipboard_push(text),
            predicate=lambda s: s.authenticated and s.websocket is not websocket,
        )

    async def _handle_clipboard_fetch(self, websocket) -> None:
        """把本机剪贴板内容定向回发给请求方。"""
        text = Clipboard.get()
        await self._send(websocket, ProtocolBuilder.clipboard_data(text))
        self._notify(f"已把本机剪贴板发送给对方（{len(text)} 字符）")

    # ================================================================== 显示器
    async def _handle_monitor_select(self, body: dict) -> None:
        """切换被采集的显示器，并把最新列表同步给所有客户端。"""
        index = body.get("index")
        if not self.screen.set_monitor(index):
            self._notify(f"切换显示器失败：非法索引 {index}", "warning")
            return
        self._notify(f"已切换到显示器 {self.screen.monitor_index}")
        await self._broadcast(
            ProtocolBuilder.monitor_list(self.screen.monitors(), self.screen.monitor_index),
            predicate=self._is_authenticated,
        )

    # ================================================================== 画质档位
    async def _handle_quality_set(self, body: dict) -> None:
        """控制端远程切换画质档位：联动帧率、JPEG 质量与分辨率上限。"""
        profile = body.get("profile")
        if not isinstance(profile, str) or profile not in config.QUALITY_PROFILES:
            self._notify(f"收到非法画质档位：{profile}", "warning")
            return

        label, fps, quality, max_width = config.QUALITY_PROFILES[profile]
        config.apply_quality_profile(profile)
        self.apply_screen_settings(fps=fps, quality=quality, max_width=max_width)

        callback = self.on_quality_changed
        if callback is not None:
            try:
                callback(profile, fps, quality, max_width)
            except Exception:
                logger.exception("画质变更回调执行异常")

        self._notify(f"控制端已切换画质档位：{label}（{fps} FPS / 质量 {quality} / 宽 {max_width}）")
        await self._broadcast(
            ProtocolBuilder.quality_state(profile, fps, quality, max_width),
            predicate=self._is_authenticated,
        )

    async def _handle_login(self, websocket, session: ClientSession, body: dict) -> None:
        if session.authenticated:
            await self._send(websocket, Protocol.create(MessageType.LOGIN_SUCCESS, {"device_id": self.device_id}))
            return

        # 两种接入方式：
        #   password  设备 ID + 临时密码 -> 直接放行
        #   approval  仅设备 ID          -> 由被控端用户点击授权
        if body.get("mode") == "approval":
            await self._handle_approval_login(websocket, session, body)
            return

        session.login_attempts += 1
        if session.login_attempts > MAX_LOGIN_ATTEMPTS:
            await self._send(websocket, ProtocolBuilder.error("认证失败次数过多，连接已关闭"))
            await self._close(websocket)
            return

        ok, reason = self.credentials.verify(body.get("password"))
        if not ok:
            await self._send(websocket, Protocol.create(MessageType.LOGIN_FAILED, {"reason": reason}))
            self._notify(f"认证失败（{session.address}）：{reason}", "warning")
            # 密码错误直接断开，避免被暴力枚举
            await self._close(websocket)
            return

        session.authenticated = True
        session.wants_screen = bool(body.get("screen", True))
        session.device_id = str(body.get("device_id") or "")
        self._bump_authed(1)
        peer_id = body.get("device_id") or "未知设备"
        await self._send(
            websocket,
            Protocol.create(
                MessageType.LOGIN_SUCCESS,
                {"device_id": self.device_id, "screen": session.wants_screen, "mode": "password"},
            ),
        )
        self._notify(f"认证通过：{peer_id} @ {session.address}")
        self._emit_client_connected(str(peer_id), session.address)

    async def _handle_approval_login(self, websocket, session: ClientSession, body: dict) -> None:
        """授权模式：不校验密码，弹出确认框由被控端用户决定。"""
        peer_id = str(body.get("device_id") or "未知设备")

        # 授权请求不占密码重试次数，但同一时刻只处理一条，避免刷屏
        if self._approval_pending:
            await self._send(
                websocket,
                Protocol.create(MessageType.LOGIN_FAILED, {"reason": "正在处理另一条授权请求，请稍后再试"}),
            )
            await self._close(websocket)
            return

        if self.on_authorize_request is None:
            await self._send(
                websocket,
                Protocol.create(MessageType.LOGIN_FAILED, {"reason": "对方程序未开启授权确认，无法接入"}),
            )
            self._notify(f"收到 {peer_id}（{session.address}）的授权请求，但未配置确认界面", "warning")
            await self._close(websocket)
            return

        loop = asyncio.get_running_loop()
        future = loop.create_future()

        def respond(approved: bool) -> None:
            """线程安全地把用户点击结果送回事件循环。"""
            def _set() -> None:
                if not future.done():
                    future.set_result(bool(approved))

            try:
                loop.call_soon_threadsafe(_set)
            except RuntimeError:
                pass

        self._approval_pending = True
        self._notify(f"{peer_id}（{session.address}）请求远程访问，等待确认…", "warning")
        approved = None
        try:
            self.on_authorize_request({"device_id": peer_id, "address": session.address, "respond": respond})
            approved = await asyncio.wait_for(future, timeout=self.approval_timeout)
        except asyncio.TimeoutError:
            approved = None
        except Exception:
            logger.exception("授权流程异常")
            approved = False
        finally:
            self._approval_pending = False

        if approved is True:
            session.authenticated = True
            session.wants_screen = bool(body.get("screen", True))
            session.device_id = peer_id
            self._bump_authed(1)
            await self._send(
                websocket,
                Protocol.create(
                    MessageType.LOGIN_SUCCESS,
                    {"device_id": self.device_id, "screen": session.wants_screen, "mode": "approval"},
                ),
            )
            self._notify(f"已授权 {peer_id}（{session.address}）接入")
            self._emit_client_connected(peer_id, session.address)
            return

        reason = "等待授权超时，对方未确认" if approved is None else "对方拒绝了本次远程访问"
        await self._send(websocket, Protocol.create(MessageType.LOGIN_FAILED, {"reason": reason}))
        self._notify(f"{peer_id}（{session.address}）接入失败：{reason}", "warning")
        await self._close(websocket)

    async def _close(self, websocket) -> None:
        try:
            await websocket.close()
        except Exception:
            pass

    # ================================================================== 控制执行
    async def _execute_control(self, msg_type: str, data: dict) -> None:
        """把控制指令投递到单线程池，保证顺序执行且不阻塞事件循环。"""
        # 「只看画面」模式：被控端关闭键鼠控制后，这里直接丢弃所有输入指令
        if not config.allow_control():
            return
        frame_width, frame_height = self.screen.frame_size()

        try:
            if msg_type == MessageType.MOUSE_MOVE:
                if "x" not in data or "y" not in data:
                    return
                fn = lambda: Control.mouse_move(  # noqa: E731
                    data["x"], data["y"], frame_width or None, frame_height or None
                )
            elif msg_type == MessageType.MOUSE_CLICK:
                fn = lambda: Control.mouse_click(  # noqa: E731
                    data.get("button", "left"),
                    data.get("action", "click"),
                    data.get("count", 1),
                )
            elif msg_type == MessageType.MOUSE_SCROLL:
                fn = lambda: Control.scroll(  # noqa: E731
                    data.get("value", 0), data.get("x"), data.get("y")
                )
            elif msg_type == MessageType.KEYBOARD:
                key = data.get("key")
                if not key:
                    return
                fn = lambda: Control.keyboard(key, data.get("action", "press"))  # noqa: E731
            else:
                return
        except (TypeError, ValueError) as exc:
            logger.debug("控制参数非法：%s", exc)
            return

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(self._input_pool, fn)
        except Exception as exc:
            logger.warning("控制执行失败（%s）：%s", msg_type, exc)

    # ================================================================== 广播
    async def _broadcast(self, message, predicate=None, exclude=None) -> None:
        """
        按条件广播。先快照再发送，避免迭代中集合被修改。

        predicate(session) -> bool，为 None 时广播给所有连接。
        exclude 为某个 websocket 时，跳过它（用于「转发给除发送者以外的所有人」）。
        """
        sessions = list(self._sessions.values())
        targets = [
            s.websocket
            for s in sessions
            if (predicate is None or predicate(s)) and s.websocket is not exclude
        ]
        if not targets:
            return

        results = await asyncio.gather(
            *(self._safe_send(ws, message) for ws in targets), return_exceptions=True
        )
        for ws, result in zip(targets, results):
            if result is False:
                self._sessions.pop(ws, None)

    @staticmethod
    def _want_screen(session: ClientSession) -> bool:
        return session.authenticated and session.wants_screen

    @staticmethod
    def _is_authenticated(session: ClientSession) -> bool:
        return session.authenticated

    @staticmethod
    async def _safe_send(websocket, message) -> bool:
        try:
            await websocket.send(message)
            return True
        except (ConnectionClosed, RuntimeError, OSError):
            return False

    # ================================================================== 屏幕推流
    def _encode_payload(self, frame) -> tuple[str, int] | None:
        """
        缩放 -> JPEG -> Base64 -> 完整协议报文。

        纯 CPU 操作，统一在线程池中执行，绝不占用事件循环。
        """
        result = self.encoder.encode_scaled(frame)
        if result is None:
            return None
        jpg, width, height = result
        image = base64.b64encode(jpg).decode("ascii")
        return ProtocolBuilder.screen(image, width, height, self._target_fps), len(jpg)

    async def _push_frame(self, frame) -> None:
        """编码并推送单帧；异常内部消化，不打断推流主循环。"""
        try:
            started = time.perf_counter()
            encoded = await asyncio.to_thread(self._encode_payload, frame)
            if not encoded:
                return
            message, size = encoded
            await self._broadcast(message, predicate=self._want_screen)
            with self._stats_lock:
                self._sent_frames += 1
                self._last_frame_bytes = size
                self._last_encode_ms = (time.perf_counter() - started) * 1000.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("画面编码 / 发送失败：%s", exc)

    async def _screen_loop(self) -> None:
        """
        画面推流主循环。

        为支撑最高 60 FPS：
            1. 只编码「新帧」—— 用 ScreenCapture.frame_seq 判别，
               同一张图绝不重复编码（旧实现会空转烧 CPU）；
            2. 缩放 + JPEG + Base64 全部丢进线程池，事件循环只负责收发；
            3. 上一帧尚未编码完就丢弃当前帧 —— 宁可掉帧也要压住延迟，
               否则队列越堆越长，画面会越来越滞后。
        """
        in_flight: asyncio.Task | None = None
        last_seq = -1

        while self.running:
            try:
                if not self._has_viewers():
                    await asyncio.sleep(0.2)
                    continue

                cycle_start = time.perf_counter()
                seq = self.screen.frame_seq

                if seq == last_seq:
                    # 没有新帧：等半个节拍再看，避免空转
                    await asyncio.sleep(min(0.01, self._frame_interval * 0.5))
                    continue

                if in_flight is not None and not in_flight.done():
                    # 上一帧仍在编码：本帧直接丢弃，优先保证实时性
                    with self._stats_lock:
                        self._dropped_frames += 1
                    last_seq = seq
                else:
                    in_flight = None
                    last_seq = seq
                    frame = self.screen.get_frame()
                    if frame is not None:
                        in_flight = asyncio.create_task(self._push_frame(frame))

                cost = time.perf_counter() - cycle_start
                sleep_for = self._frame_interval - cost
                await asyncio.sleep(sleep_for if sleep_for > 0 else 0)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("屏幕推流异常：%s", exc)
                await asyncio.sleep(0.5)

        if in_flight is not None and not in_flight.done():
            in_flight.cancel()
            try:
                await in_flight
            except (asyncio.CancelledError, Exception):
                pass

    # ================================================================== 画面配置
    def set_fps(self, fps) -> int:
        """设置推流帧率（1 ~ SCREEN_FPS_MAX），并同步给采集线程。"""
        try:
            value = int(fps)
        except (TypeError, ValueError):
            value = self._target_fps
        self._target_fps = max(config.SCREEN_FPS_MIN, min(config.SCREEN_FPS_MAX, value))
        self._frame_interval = 1.0 / self._target_fps
        self.screen.set_fps(self._target_fps)
        return self._target_fps

    def apply_screen_settings(self, fps=None, quality=None, max_width=None) -> None:
        """一次性应用画面参数（设置对话框 / 画质档位调用）。"""
        if fps is not None:
            self.set_fps(fps)
        if quality is not None:
            self.encoder.set_quality(quality)
        if max_width is not None:
            self.encoder.set_max_width(max_width)

    @property
    def target_fps(self) -> int:
        return self._target_fps

    def stream_stats(self) -> dict:
        """推流运行统计（线程安全）。"""
        with self._stats_lock:
            return {
                "fps": self._target_fps,
                "sent": self._sent_frames,
                "dropped": self._dropped_frames,
                "encode_ms": round(self._last_encode_ms, 2),
                "frame_bytes": self._last_frame_bytes,
            }

    def reset_stream_stats(self) -> None:
        with self._stats_lock:
            self._sent_frames = 0
            self._dropped_frames = 0

    # ================================================================== 启动 / 停止
    def _candidate_ports(self) -> list[int]:
        ports = [self.port]
        for offset in range(1, max(0, self.port_fallback) + 1):
            candidate = self.port + offset
            if candidate <= 65535:
                ports.append(candidate)
        return ports

    async def _bind(self):
        """尝试绑定端口，返回 server 对象。"""
        last_error: Exception | None = None
        for port in self._candidate_ports():
            try:
                server = await websockets.serve(
                    self.handler,
                    self.host,
                    port,
                    max_size=config.WS_MAX_SIZE,
                    ping_interval=config.WS_PING_INTERVAL,
                    ping_timeout=config.WS_PING_TIMEOUT,
                    open_timeout=config.WS_OPEN_TIMEOUT,
                )
            except OSError as exc:
                last_error = exc
                self._notify(f"端口 {port} 不可用（{exc.strerror or exc}），尝试下一个…", "warning")
                continue
            self.actual_port = port
            return server
        raise OSError(
            f"无法绑定任何可用端口（已尝试 {self._candidate_ports()}）。"
            f"请关闭占用 {self.port} 的程序后重试。最后错误：{last_error}"
        )

    async def start(self) -> None:
        """启动服务并常驻，直到 stop() 被调用。"""
        self._loop = asyncio.get_running_loop()
        try:
            self._server = await self._bind()
        except OSError as exc:
            logger.error("%s", exc)
            self._notify(str(exc), "error")
            self.running = False
            return

        self.running = True
        self.screen.start()
        if self.discovery is not None:
            try:
                self.discovery.set_advertised_port(self.actual_port)
            except Exception:
                logger.exception("同步发现服务端口失败")
        self._screen_task = asyncio.create_task(self._screen_loop(), name="screen-loop")
        self._notify(f"服务已启动，监听 {self.host}:{self.actual_port}")

        try:
            await self._server.wait_closed()
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        self.running = False
        if self._screen_task and not self._screen_task.done():
            self._screen_task.cancel()
            try:
                await self._screen_task
            except (asyncio.CancelledError, Exception):
                pass
        self._screen_task = None

        for websocket in list(self._sessions.keys()):
            await self._close(websocket)
        self._sessions.clear()
        with self._count_lock:
            self._authed_count = 0

        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None

        self.screen.stop()
        self._input_pool.shutdown(wait=False, cancel_futures=True)
        logger.info("服务端已停止")

    def stop(self) -> None:
        """线程安全地请求停止（可从任意线程调用）。"""
        self.running = False
        self.screen.stop(timeout=1.0)
        server = self._server
        loop = self._loop
        if server is not None and loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(server.close)
            except RuntimeError:
                pass
