"""
LAN Remote 客户端

纯 asyncio 实现，不依赖任何 GUI 框架：
    - 端口可配置（不再硬编码 9527）
    - 单次连接 connect() 与带自动重连的常驻 run()
    - 报文解析全程容错，坏包直接丢弃
    - 通过回调向上层抛出画面 / 状态 / 聊天事件
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Callable

import websockets

import config
from core.file_transfer import FileReceiver, FileSender, FileTransfer, stream_file
from core.protocol import MessageType, Protocol, ProtocolBuilder

logger = logging.getLogger("LAN_REMOTE")


class RemoteClient:
    """远程连接客户端。"""

    def __init__(self, port: int = None, download_dir: str = None):
        self.port = int(port or config.server_port())
        # 文件接收目录（默认 downloads，可注入以便自检隔离）
        self.download_dir = str(download_dir or config.download_dir())

        self.websocket = None
        self.server_ip: str | None = None
        self.device_id: str | None = None
        self.password: str | None = None

        self.running = False
        self.authenticated = False
        self.auth_rejected = False        # 服务端明确拒绝 -> 不再重试
        self.auth_mode = "password"       # password / approval
        self.server_device_id: str | None = None
        # 最近一次连接失败的分类与原因（供界面弹窗提示）：
        #   kind = "auth"（ID/密码不对）/ "offline"（对方不在线/网络不通）/
        #          "timeout"（等响应超时）/ "closed"（认证时被服务端关闭）
        self.last_fail_kind: str = ""
        self.last_fail_reason: str = ""
        self.last_delay: float = 0.0      # 最近一帧端到端延迟（毫秒）
        self.last_fps: float = 0.0
        self.last_bytes_per_sec: float = 0.0   # 画面带宽（字节/秒）

        # 回调（均在事件循环线程中被调用，GUI 层负责转换为 Qt 信号）
        self.on_screen: Callable[[bytes, int, int, float], None] | None = None
        self.on_status: Callable[[str, str], None] | None = None
        self.on_chat: Callable[[str, str], None] | None = None
        self.on_state: Callable[[bool], None] | None = None
        # 收到对端推来的剪贴板文本
        self.on_clipboard: Callable[[str], None] | None = None
        # 收到对端显示器列表：(monitors, current_index)
        self.on_monitor_list: Callable[[list, int], None] | None = None
        # 对端画质档位变化：(profile, fps, quality, max_width)
        self.on_quality_state: Callable[[str, int, int, int], None] | None = None
        # 文件传输事件：callback(event, payload)
        #   event = "offer" / "progress" / "done"
        #   payload.direction = "in"（收到）/ "out"（发出）
        self.on_file_event: Callable[[str, dict], None] | None = None
        # 首次连接失败（未成功过就失败，不重试直接结束）：callback(kind, reason)
        # 供界面弹出分类错误提示；断线后的自动重连失败不会触发此回调
        self.on_connect_failed: Callable[[str, str], None] | None = None

        # 文件传输会话（只在事件循环线程里读写，无需加锁）
        self._incoming_files: dict[str, FileReceiver] = {}
        self._outgoing_files: dict[str, FileSender] = {}
        self._file_reported: dict[str, int] = {}

        self._recv_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._send_lock: asyncio.Lock | None = None
        # run() 代际号：重复 start_session 时让旧的运行循环自行退出，
        # 避免两个 run() 同时抢一个客户端（重复重连 / 状态错乱）
        self._run_seq = 0
        self._frame_stamps: list[float] = []
        self._bw_bytes = 0
        self._bw_started = time.time()

    # ================================================================== 内部工具
    def _lock(self) -> asyncio.Lock:
        """延迟创建锁，确保绑定在真正运行的事件循环上。"""
        if self._send_lock is None:
            self._send_lock = asyncio.Lock()
        return self._send_lock

    def _notify(self, text: str, level: str = "info") -> None:
        if level == "error":
            logger.error("[客户端] %s", text)
        elif level == "warning":
            logger.warning("[客户端] %s", text)
        else:
            logger.info("[客户端] %s", text)
        if self.on_status is not None:
            try:
                self.on_status(text, level)
            except Exception:
                logger.exception("状态回调执行异常")

    def _set_state(self, connected: bool) -> None:
        if self.authenticated == connected:
            return
        self.authenticated = connected
        if self.on_state is not None:
            try:
                self.on_state(connected)
            except Exception:
                logger.exception("连接状态回调执行异常")

    def _url(self, ip: str) -> str:
        return f"ws://{ip}:{self.port}"

    def _fail(self, kind: str, reason: str) -> None:
        """记录最近一次失败（分类 + 原因），供界面弹窗提示。"""
        self.last_fail_kind = kind
        self.last_fail_reason = reason

    # ================================================================== 连接
    async def connect(
        self,
        ip: str,
        device_id: str,
        password: str,
        timeout: float = None,
        mode: str = None,
    ) -> bool:
        """
        单次连接 + 认证。返回是否成功；被拒绝时置位 auth_rejected。

        mode = "password"  密码直连
        mode = "approval"  仅凭设备 ID，等待对方点击授权（耗时较长）
        """
        self.server_ip = ip
        self.device_id = device_id
        self.password = password
        self.auth_rejected = False
        self.last_fail_kind = ""
        self.last_fail_reason = ""
        self.auth_mode = mode or ("approval" if not password else "password")

        connect_timeout = float(timeout or config.WS_OPEN_TIMEOUT)
        # 授权模式下对方可能过一会儿才点确认，等待窗口要放大
        recv_timeout = (
            connect_timeout if self.auth_mode == "password"
            else float(config.APPROVAL_TIMEOUT) + 15.0
        )

        if self.websocket is not None:
            await self._close_ws()

        if self.auth_mode == "approval":
            self._notify(f"正在请求 {ip} 授权，请提醒对方点击同意 …")
        else:
            self._notify(f"正在连接 {ip}:{self.port} …")

        try:
            self.websocket = await asyncio.wait_for(
                websockets.connect(
                    self._url(ip),
                    max_size=config.WS_MAX_SIZE,
                    ping_interval=config.WS_PING_INTERVAL,
                    ping_timeout=config.WS_PING_TIMEOUT,
                    open_timeout=config.WS_OPEN_TIMEOUT,
                ),
                timeout=connect_timeout,
            )
        except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException) as exc:
            self.websocket = None
            self.authenticated = False
            kind = "timeout" if isinstance(exc, asyncio.TimeoutError) else "offline"
            self._fail(kind, f"{ip}:{self.port} {exc}")
            self._notify(f"连接失败：{exc}", "error")
            return False

        try:
            await self.websocket.send(ProtocolBuilder.login(device_id, password, self.auth_mode))
        except (websockets.exceptions.ConnectionClosed, OSError) as exc:
            await self._close_ws()
            self.authenticated = False
            self._fail("offline", f"{ip}:{self.port} {exc}")
            self._notify(f"发送认证请求失败：{exc}", "error")
            return False

        # 收到认证结果前，服务端可能已经在推流，需要跳过画面帧
        deadline = time.monotonic() + recv_timeout
        response = None
        while time.monotonic() < deadline:
            try:
                remaining = max(0.1, deadline - time.monotonic())
                response = await asyncio.wait_for(self.websocket.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            except websockets.exceptions.ConnectionClosed as exc:
                await self._close_ws()
                self.authenticated = False
                self._fail("closed", str(exc.reason or exc.code))
                self._notify(f"连接被服务端关闭：{exc.reason or exc.code}", "warning")
                return False
            if Protocol.get_type(response) != MessageType.SCREEN:
                break

        msg_type = Protocol.get_type(response) if response is not None else None

        if msg_type == MessageType.LOGIN_SUCCESS:
            body = Protocol.get_data(response)
            self.server_device_id = body.get("device_id")
            self._set_state(True)
            if self.auth_mode == "approval":
                self._notify(f"{self.server_device_id or '对方'} 已授权接入")
            else:
                self._notify(f"已连接到 {ip}（{self.server_device_id or '未知设备'}）")
            return True

        if msg_type == MessageType.LOGIN_FAILED:
            # 被拒绝 / 超时属于正常业务结果，记为 warning 而非 error
            reason = Protocol.get_data(response).get("reason", "密码错误")
            self.auth_rejected = True
            self._fail("auth", str(reason))
            self._notify(f"接入失败：{reason}", "warning")
        elif response is None:
            self._fail("timeout", "等待对方响应超时")
            self._notify("接入失败：等待对方响应超时", "warning")
        else:
            self._fail("unknown", "服务端返回了无法识别的响应")
            self._notify("接入失败：服务端返回了无法识别的响应", "warning")

        self._set_state(False)
        await self._close_ws()
        return False

    async def _close_ws(self) -> None:
        ws = self.websocket
        self.websocket = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    # ================================================================== 常驻运行
    async def run(self, ip: str, device_id: str, password: str, mode: str = None) -> None:
        """
        连接并常驻，断线自动重连，直到 stop() 被调用。

        首次连接（本次 run 内从未成功过）就失败时：不重试，通过
        on_connect_failed(kind, reason) 上报后结束 —— 手动发起的连接应当
        立刻得到明确的结果反馈，而不是在后台默默重试。
        """
        self.running = True
        delay = config.CLIENT_RECONNECT_DELAY
        ever_connected = False
        self._run_seq += 1
        run_seq = self._run_seq

        while self.running and self._run_seq == run_seq:
            ok = await self.connect(ip, device_id, password, mode=mode)
            if not self.running:
                break

            if not ok:
                if not ever_connected:
                    # 首次就没连上：重试没有意义，把分类结果交给界面弹窗
                    self._emit_connect_failed()
                    break
                if self.auth_rejected:
                    # 密码错误：重试毫无意义，直接结束
                    break
                self._notify(f"{delay} 秒后重试…", "warning")
                await asyncio.sleep(delay)
                delay = min(delay * 2, config.CLIENT_RECONNECT_MAX_DELAY)
                continue

            ever_connected = True
            delay = config.CLIENT_RECONNECT_DELAY
            try:
                await self._serve()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("会话异常：%s", exc)

            if not self.running:
                break
            self._notify(f"连接已断开，{delay} 秒后重连…", "warning")
            await asyncio.sleep(delay)

        await self._teardown()
        self._notify("客户端已停止")

    def _emit_connect_failed(self) -> None:
        callback = self.on_connect_failed
        if callback is None:
            return
        try:
            callback(self.last_fail_kind or "offline", self.last_fail_reason or "未知原因")
        except Exception:
            logger.exception("连接失败回调异常")

    async def _serve(self) -> None:
        """运行接收循环与心跳，任一结束即整体结束。"""
        self._recv_task = asyncio.create_task(self._recv_loop(), name="recv-loop")
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="heartbeat-loop")

        done, pending = await asyncio.wait(
            {self._recv_task, self._heartbeat_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc:
                raise exc

    async def _recv_loop(self) -> None:
        ws = self.websocket
        if ws is None:
            return

        while self.running:
            message = await ws.recv()
            data = Protocol.parse(message)
            if not data:
                continue

            msg_type = data.get("type")

            if msg_type == MessageType.SCREEN:
                self._handle_screen(data)
            elif msg_type == MessageType.CHAT:
                body = data.get("data") or {}
                if self.on_chat is not None:
                    try:
                        self.on_chat(body.get("text", ""), body.get("sender", ""))
                    except Exception:
                        logger.exception("聊天回调异常")
            elif msg_type in (MessageType.CLIPBOARD_PUSH, MessageType.CLIPBOARD_DATA):
                body = data.get("data") or {}
                text = body.get("text")
                if isinstance(text, str) and self.on_clipboard is not None:
                    try:
                        self.on_clipboard(text)
                    except Exception:
                        logger.exception("剪贴板回调异常")
            elif msg_type == MessageType.MONITOR_LIST:
                body = data.get("data") or {}
                monitors = body.get("monitors")
                current = body.get("current", 0)
                if isinstance(monitors, list) and self.on_monitor_list is not None:
                    try:
                        self.on_monitor_list(monitors, int(current))
                    except Exception:
                        logger.exception("显示器列表回调异常")
            elif msg_type == MessageType.QUALITY_STATE:
                body = data.get("data") or {}
                if self.on_quality_state is not None:
                    try:
                        self.on_quality_state(
                            str(body.get("profile", "")),
                            int(body.get("fps", 0) or 0),
                            int(body.get("quality", 0) or 0),
                            int(body.get("max_width", 0) or 0),
                        )
                    except Exception:
                        logger.exception("画质状态回调异常")
            elif msg_type == MessageType.ERROR:
                body = data.get("data") or {}
                self._notify(body.get("text", "服务端返回错误"), "warning")
            # ------------------------------------------------------ 文件传输
            elif msg_type == MessageType.FILE_INFO:
                await self._handle_file_offer(data.get("data") or {})
            elif msg_type == MessageType.FILE_DATA:
                await self._handle_file_chunk(data.get("data") or {})
            elif msg_type == MessageType.FILE_FINISH:
                await self._handle_file_finish(data.get("data") or {})
            elif msg_type == MessageType.FILE_CANCEL:
                await self._handle_file_cancel(data.get("data") or {})
            elif msg_type == MessageType.FILE_PROGRESS:
                self._handle_file_progress(data.get("data") or {})

    # ================================================================== 文件传输
    async def send_file(self, path: str) -> str | None:
        """把本机文件发给对端；返回 transfer_id，失败返回 None。"""
        try:
            sender = FileSender(path, sender=self.device_id or "")
        except (FileNotFoundError, OSError) as exc:
            self._notify(f"无法读取文件：{exc}", "warning")
            return None

        if sender.size > config.file_max_size():
            self._notify(
                f"文件超过体积上限（{FileTransfer.format_size(config.file_max_size())}）", "warning"
            )
            return None

        if not self.authenticated:
            self._notify("尚未连接，无法发送文件", "warning")
            return None

        self._outgoing_files[sender.transfer_id] = sender
        self._emit_file_event("offer", self._file_payload(sender, "out"))
        self._notify(f"开始发送文件：{sender.name}（{FileTransfer.format_size(sender.size)}）")

        if not await self.send(sender.info_message()):
            self._outgoing_files.pop(sender.transfer_id, None)
            payload = self._file_payload(sender, "out")
            payload.update({"done": True, "ok": False, "message": "发送失败：连接不可用"})
            self._emit_file_event("done", payload)
            return None

        await stream_file(
            sender,
            self.send,
            lambda s: self._emit_file_event("progress", self._file_payload(s, "out")),
        )
        await self.send(sender.finish_message())
        self._emit_file_event("progress", self._file_payload(sender, "out"))
        return sender.transfer_id

    def file_transfers(self) -> list[dict]:
        """当前进行中的文件传输（供界面显示）。"""
        items = [self._file_payload(r, "in") for r in list(self._incoming_files.values())]
        items.extend(self._file_payload(s, "out") for s in list(self._outgoing_files.values()))
        return items

    def _emit_file_event(self, event: str, payload: dict) -> None:
        callback = self.on_file_event
        if callback is None:
            return
        try:
            callback(event, dict(payload))
        except Exception:
            logger.exception("文件传输事件回调异常")

    def _file_payload(
        self,
        session,
        direction: str,
        done: bool = False,
        ok: bool = True,
        path: str = "",
        message: str = "",
    ) -> dict:
        return {
            "transfer_id": session.transfer_id,
            "name": session.name,
            "received": int(session.received),
            "total": int(session.total),
            "direction": direction,
            "done": bool(done),
            "ok": bool(ok),
            "path": path,
            "message": message,
        }

    async def _handle_file_offer(self, body: dict) -> None:
        """对端要发文件过来：默认接收，存到下载目录。"""
        transfer_id = str(body.get("transfer_id") or "").strip()
        if not transfer_id:
            return

        size = int(body.get("size") or 0)
        name = FileTransfer.safe_name(body.get("name"))

        if size > config.file_max_size():
            reason = f"文件超过体积上限（{FileTransfer.format_size(config.file_max_size())}）"
            await self.send(ProtocolBuilder.file_cancel(transfer_id, reason))
            payload = {
                "transfer_id": transfer_id, "name": name, "received": 0, "total": size,
                "direction": "in", "done": True, "ok": False, "path": "", "message": reason,
            }
            self._notify(f"已拒绝 {name}：{reason}", "warning")
            self._emit_file_event("done", payload)
            return

        stale = self._incoming_files.pop(transfer_id, None)
        if stale is not None:
            stale.abort()

        try:
            receiver = FileReceiver(body, save_dir=self.download_dir)
        except OSError as exc:
            await self.send(ProtocolBuilder.error(f"无法创建接收文件：{exc}"))
            return

        self._incoming_files[transfer_id] = receiver
        self._file_reported[transfer_id] = 0
        self._notify(f"正在接收文件：{name}（{FileTransfer.format_size(size)}）")
        self._emit_file_event("offer", self._file_payload(receiver, "in"))
        await self.send(ProtocolBuilder.file_progress(transfer_id, 0, size))

    async def _handle_file_chunk(self, body: dict) -> None:
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
            await self.send(ProtocolBuilder.file_cancel(transfer_id, receiver.error))
            payload = {
                "transfer_id": transfer_id, "name": receiver.name, "received": received,
                "total": receiver.total, "direction": "in", "done": True, "ok": False,
                "path": "", "message": receiver.error,
            }
            self._emit_file_event("done", payload)
            return

        last = self._file_reported.get(transfer_id, 0)
        step = max(1, int(receiver.total * config.FILE_PROGRESS_STEP)) if receiver.total else 1
        if received - last >= step or received >= receiver.total:
            self._file_reported[transfer_id] = received
            await self.send(
                ProtocolBuilder.file_progress(transfer_id, received, receiver.total)
            )
            self._emit_file_event("progress", self._file_payload(receiver, "in"))

    async def _handle_file_finish(self, body: dict) -> None:
        transfer_id = str(body.get("transfer_id") or "")
        receiver = self._incoming_files.pop(transfer_id, None)
        self._file_reported.pop(transfer_id, None)
        if receiver is None:
            return

        result = receiver.finish()
        if result["ok"]:
            self._notify(f"文件已保存：{result['path']}")
        else:
            self._notify(f"接收 {receiver.name} 失败：{result['message']}", "warning")

        await self.send(
            ProtocolBuilder.file_progress(
                transfer_id, int(result["size"]), receiver.total, True,
                result["path"], bool(result["ok"]), result["message"],
            )
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
        transfer_id = str(body.get("transfer_id") or "")
        reason = str(body.get("reason") or "")

        receiver = self._incoming_files.pop(transfer_id, None)
        self._file_reported.pop(transfer_id, None)
        if receiver is not None:
            receiver.abort()
            self._notify(f"文件接收已中止：{receiver.name}", "warning")
            payload = {
                "transfer_id": transfer_id, "name": receiver.name, "received": receiver.received,
                "total": receiver.total, "direction": "in", "done": True, "ok": False,
                "path": "", "message": reason or "已中止",
            }
            self._emit_file_event("done", payload)

        sender = self._outgoing_files.pop(transfer_id, None)
        if sender is not None:
            sender.cancel()
            self._notify(f"文件发送已中止：{sender.name}", "warning")
            payload = {
                "transfer_id": transfer_id, "name": sender.name, "received": sender.sent,
                "total": sender.size, "direction": "out", "done": True, "ok": False,
                "path": "", "message": reason or "已中止",
            }
            self._emit_file_event("done", payload)

    def _handle_file_progress(self, body: dict) -> None:
        """本机发送时，对端回报的进度。"""
        transfer_id = str(body.get("transfer_id") or "")
        sender = self._outgoing_files.get(transfer_id)
        done = bool(body.get("done"))
        ok = bool(body.get("ok", True))
        message = str(body.get("message") or "")
        path = str(body.get("path") or "")

        if done:
            self._outgoing_files.pop(transfer_id, None)
            if ok:
                self._notify(f"文件已送达：{sender.name if sender else transfer_id}")
            else:
                self._notify(
                    f"文件发送失败：{sender.name if sender else transfer_id}"
                    f"（{message or '对端未接收'}）",
                    "warning",
                )

        payload = {
            "transfer_id": transfer_id,
            "name": sender.name if sender else transfer_id,
            "received": int(body.get("received") or 0),
            "total": int(sender.size if sender else (body.get("total") or 0)),
            "direction": "out",
            "done": done,
            "ok": ok,
            "path": path,
            "message": message,
        }
        self._emit_file_event("done" if done else "progress", payload)

    def _handle_screen(self, data: dict) -> None:
        body = data.get("data") or {}
        image = body.get("image")
        if not isinstance(image, str) or not image:
            return

        # 延迟统计
        timestamp = data.get("timestamp")
        if isinstance(timestamp, (int, float)):
            self.last_delay = round(max(0.0, (time.time() - timestamp) * 1000), 1)

        # 帧率统计（滑动 1 秒窗口）
        now = time.time()
        self._frame_stamps.append(now)
        cutoff = now - 1.0
        while len(self._frame_stamps) > 1 and self._frame_stamps[0] < cutoff:
            self._frame_stamps.pop(0)
        span = self._frame_stamps[-1] - self._frame_stamps[0]
        self.last_fps = round((len(self._frame_stamps) - 1) / span, 1) if span > 0 else 0.0

        # 带宽统计（滑动 1 秒窗口，按 base64 载荷长度计）
        self._bw_bytes += len(image)
        bw_span = now - self._bw_started
        if bw_span >= 1.0:
            self.last_bytes_per_sec = self._bw_bytes / bw_span
            self._bw_bytes = 0
            self._bw_started = now

        try:
            frame = base64.b64decode(image, validate=False)
        except (ValueError, TypeError):
            return

        width = int(body.get("width") or 0)
        height = int(body.get("height") or 0)

        if self.on_screen is not None:
            try:
                self.on_screen(frame, width, height, self.last_fps)
            except Exception:
                logger.exception("画面回调异常")

    async def _heartbeat_loop(self) -> None:
        while self.running:
            await asyncio.sleep(config.HEARTBEAT_INTERVAL)
            if not self.running:
                return
            await self.send(ProtocolBuilder.heartbeat())

    async def _teardown(self) -> None:
        for task in (self._recv_task, self._heartbeat_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._recv_task = None
        self._heartbeat_task = None
        self._set_state(False)
        await self._close_ws()
        self._abort_files()

    def _abort_files(self) -> None:
        """连接断开时清理未完成的文件传输，避免残留半截文件。"""
        for receiver in list(self._incoming_files.values()):
            receiver.abort()
        self._incoming_files.clear()
        for sender in list(self._outgoing_files.values()):
            sender.cancel()
        self._outgoing_files.clear()
        self._file_reported.clear()

    # ================================================================== 发送
    async def send(self, message: str) -> bool:
        """发送一条消息；连接不可用时返回 False（不抛异常）。"""
        ws = self.websocket
        if ws is None or not self.authenticated:
            return False
        try:
            async with self._lock():
                await ws.send(message)
            return True
        except (websockets.exceptions.ConnectionClosed, RuntimeError, OSError) as exc:
            logger.debug("发送失败：%s", exc)
            return False

    async def send_mouse_move(self, x, y) -> bool:
        return await self.send(ProtocolBuilder.mouse_move(x, y))

    async def send_mouse_click(self, button: str = "left", action: str = "click", count: int = 1) -> bool:
        return await self.send(ProtocolBuilder.mouse_click(button, action, count))

    async def send_mouse_scroll(self, value: int) -> bool:
        return await self.send(ProtocolBuilder.mouse_scroll(value))

    async def send_key(self, key: str, action: str = "press") -> bool:
        return await self.send(ProtocolBuilder.keyboard(key, action))

    async def send_chat(self, text: str) -> bool:
        return await self.send(ProtocolBuilder.chat(text, self.device_id or ""))

    async def send_clipboard(self, text: str) -> bool:
        return await self.send(ProtocolBuilder.clipboard(text))

    async def push_clipboard(self, text: str) -> bool:
        """把本机剪贴板推送给对端（对端会写入它自己的剪贴板）。"""
        return await self.send(ProtocolBuilder.clipboard_push(text))

    async def fetch_clipboard(self) -> bool:
        """请求对端把它的剪贴板发过来；内容经 on_clipboard 回调返回。"""
        return await self.send(ProtocolBuilder.clipboard_fetch())

    async def request_monitor_list(self) -> bool:
        return await self.send(ProtocolBuilder.monitor_list_request())

    async def select_monitor(self, index: int) -> bool:
        return await self.send(ProtocolBuilder.monitor_select(index))

    async def set_quality_profile(self, profile: str) -> bool:
        """请求被控端切换画质档位（对方会回 QUALITY_STATE 确认）。"""
        return await self.send(ProtocolBuilder.quality_set(profile))

    async def request_screen(self, enabled: bool = True) -> bool:
        msg_type = MessageType.SCREEN_REQUEST if enabled else MessageType.SCREEN_STOP
        return await self.send(Protocol.create(msg_type))

    # ================================================================== 停止
    async def close(self) -> None:
        """协作式优雅关闭（需在事件循环内调用）。"""
        self._run_seq += 1  # 让仍在运行的 run() 循环退出
        self.running = False
        await self._teardown()

    def stop(self) -> None:
        """线程安全地请求停止（供上层从其它线程调用）。"""
        self.running = False
