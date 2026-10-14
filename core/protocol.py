"""
LAN Remote 通信协议 V2.0

所有客户端与服务端数据均通过此协议封装。

报文结构：
    {
        "version": "2.0",
        "type": "screen",
        "session": "uuid",
        "timestamp": 1700000000.0,
        "data": {...}
    }
"""

from __future__ import annotations

import json
import time
import uuid

VERSION = "2.0"


class MessageType:
    """消息类型定义。"""

    # 登录认证
    LOGIN = "login"
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILED = "login_failed"

    # 心跳
    HEARTBEAT = "heartbeat"
    HEARTBEAT_REPLY = "heartbeat_reply"

    # 屏幕传输
    SCREEN = "screen"
    SCREEN_REQUEST = "screen_request"
    SCREEN_STOP = "screen_stop"

    # 鼠标键盘
    CONTROL = "control"
    MOUSE_MOVE = "mouse_move"
    MOUSE_CLICK = "mouse_click"
    MOUSE_SCROLL = "mouse_scroll"
    KEYBOARD = "keyboard"

    # 文件
    FILE_INFO = "file_info"
    FILE_DATA = "file_data"
    FILE_FINISH = "file_finish"
    FILE_CANCEL = "file_cancel"
    FILE_PROGRESS = "file_progress"

    # 聊天
    CHAT = "chat"

    # 剪贴板
    #   CLIPBOARD       推送本机剪贴板文本到对端（被控端会写入本机剪贴板）
    #   CLIPBOARD_FETCH 请求对端把它的剪贴板发给我
    #   CLIPBOARD_DATA  剪贴板内容响应（定向回给请求者）
    CLIPBOARD = "clipboard"
    CLIPBOARD_PUSH = "clipboard_push"
    CLIPBOARD_FETCH = "clipboard_fetch"
    CLIPBOARD_DATA = "clipboard_data"

    # 显示器
    MONITOR_LIST_REQUEST = "monitor_list_request"
    MONITOR_LIST = "monitor_list"
    MONITOR_SELECT = "monitor_select"

    # 画质档位（控制端可远程调整被控端的采集/编码参数）
    QUALITY_SET = "quality_set"
    QUALITY_STATE = "quality_state"

    # 权限
    PERMISSION_REQUEST = "permission_request"
    PERMISSION_RESPONSE = "permission_response"

    # 状态
    INFO = "info"
    ERROR = "error"


class Protocol:
    """协议封装 / 解析工具（全部方法均为纯函数，可跨线程调用）。"""

    VERSION = VERSION

    @staticmethod
    def create(msg_type: str, data: dict | None = None, session_id: str | None = None) -> str:
        """构造标准消息，返回 JSON 字符串。"""
        message = {
            "version": Protocol.VERSION,
            "type": msg_type,
            "session": session_id or str(uuid.uuid4()),
            "timestamp": time.time(),
            "data": data if data is not None else {},
        }
        return json.dumps(message, ensure_ascii=False)

    @staticmethod
    def parse(message) -> dict | None:
        """
        解析消息。

        兼容 bytes 与 str；解析失败或结构非法统一返回 None，
        调用方无需再包 try/except。
        """
        if isinstance(message, (bytes, bytearray)):
            try:
                message = message.decode("utf-8")
            except UnicodeDecodeError:
                return None
        if not isinstance(message, str):
            return None
        try:
            data = json.loads(message)
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def check(message) -> bool:
        """校验消息字段完整性。"""
        data = Protocol.parse(message)
        if not data:
            return False
        return all(key in data for key in ("version", "type", "timestamp", "data"))

    @staticmethod
    def get_type(message) -> str | None:
        """安全提取消息类型。"""
        data = Protocol.parse(message)
        if not data:
            return None
        msg_type = data.get("type")
        return msg_type if isinstance(msg_type, str) else None

    @staticmethod
    def get_data(message) -> dict:
        """安全提取消息负载，始终返回 dict。"""
        data = Protocol.parse(message)
        if not data:
            return {}
        body = data.get("data")
        return body if isinstance(body, dict) else {}


class ProtocolBuilder:
    """常用消息的快捷构造方法。"""

    @staticmethod
    def login(device_id: str, password: str = "", mode: str = "password") -> str:
        """
        构造登录消息。

        mode = "password"  设备 ID + 临时密码，直接认证
        mode = "approval"  仅设备 ID，由被控端用户点击授权
        """
        data: dict = {"device_id": device_id, "mode": mode, "version": Protocol.VERSION}
        if password:
            data["password"] = password
        return Protocol.create(MessageType.LOGIN, data)

    @staticmethod
    def heartbeat() -> str:
        return Protocol.create(MessageType.HEARTBEAT, {"time": time.time()})

    @staticmethod
    def screen(image: str, width: int, height: int, fps: int = 0) -> str:
        return Protocol.create(
            MessageType.SCREEN,
            {"image": image, "width": width, "height": height, "fps": fps},
        )

    @staticmethod
    def mouse_move(x, y) -> str:
        return Protocol.create(MessageType.MOUSE_MOVE, {"x": x, "y": y})

    @staticmethod
    def mouse_click(button: str = "left", action: str = "click", count: int = 1) -> str:
        return Protocol.create(
            MessageType.MOUSE_CLICK,
            {"button": button, "action": action, "count": count},
        )

    @staticmethod
    def mouse_scroll(value: int, x: int | None = None, y: int | None = None) -> str:
        payload: dict = {"value": value}
        if x is not None and y is not None:
            payload["x"] = x
            payload["y"] = y
        return Protocol.create(MessageType.MOUSE_SCROLL, payload)

    @staticmethod
    def keyboard(key: str, action: str = "press") -> str:
        return Protocol.create(MessageType.KEYBOARD, {"key": key, "action": action})

    @staticmethod
    def chat(text: str, sender: str = "") -> str:
        return Protocol.create(MessageType.CHAT, {"text": text, "sender": sender})

    @staticmethod
    def clipboard(text: str) -> str:
        return Protocol.create(MessageType.CLIPBOARD, {"text": text})

    @staticmethod
    def clipboard_push(text: str) -> str:
        """把本机剪贴板推送到对端（并被控端写入本机剪贴板）。"""
        return Protocol.create(MessageType.CLIPBOARD_PUSH, {"text": text})

    @staticmethod
    def clipboard_fetch() -> str:
        """请求对端把它的剪贴板内容发过来。"""
        return Protocol.create(MessageType.CLIPBOARD_FETCH)

    @staticmethod
    def clipboard_data(text: str) -> str:
        return Protocol.create(MessageType.CLIPBOARD_DATA, {"text": text})

    @staticmethod
    def monitor_list_request() -> str:
        return Protocol.create(MessageType.MONITOR_LIST_REQUEST)

    @staticmethod
    def monitor_list(monitors: list[dict], current: int) -> str:
        return Protocol.create(
            MessageType.MONITOR_LIST, {"monitors": monitors, "current": int(current)}
        )

    @staticmethod
    def monitor_select(index: int) -> str:
        return Protocol.create(MessageType.MONITOR_SELECT, {"index": int(index)})

    @staticmethod
    def quality_set(profile: str) -> str:
        return Protocol.create(MessageType.QUALITY_SET, {"profile": str(profile)})

    @staticmethod
    def quality_state(profile: str, fps: int, quality: int, max_width: int) -> str:
        return Protocol.create(
            MessageType.QUALITY_STATE,
            {
                "profile": str(profile),
                "fps": int(fps),
                "quality": int(quality),
                "max_width": int(max_width),
            },
        )

    # ------------------------------------------------------------ 文件传输
    # 一次传输用 transfer_id 标识，流程：
    #   发送方 FILE_INFO(offer) → FILE_DATA × N → FILE_FINISH
    #   接收方每收若干块回 FILE_PROGRESS（含最终完成态）
    #   任一方都可以用 FILE_CANCEL 中止
    @staticmethod
    def file_info(
        transfer_id: str,
        name: str,
        size: int,
        md5: str = "",
        sender: str = "",
    ) -> str:
        """文件传输发起（offer）：告知对端要发什么文件。"""
        return Protocol.create(
            MessageType.FILE_INFO,
            {
                "transfer_id": str(transfer_id),
                "name": str(name),
                "size": int(size),
                "md5": str(md5),
                "sender": str(sender),
            },
        )

    @staticmethod
    def file_data(transfer_id: str, seq: int, chunk: str) -> str:
        """一个数据分块（chunk 为 base64 文本）。"""
        return Protocol.create(
            MessageType.FILE_DATA,
            {"transfer_id": str(transfer_id), "seq": int(seq), "chunk": chunk},
        )

    @staticmethod
    def file_finish(transfer_id: str) -> str:
        """数据发送完毕，接收方可以校验并落盘。"""
        return Protocol.create(MessageType.FILE_FINISH, {"transfer_id": str(transfer_id)})

    @staticmethod
    def file_cancel(transfer_id: str, reason: str = "") -> str:
        """中止一次传输（任一方都可发起）。"""
        return Protocol.create(
            MessageType.FILE_CANCEL,
            {"transfer_id": str(transfer_id), "reason": str(reason)},
        )

    @staticmethod
    def file_progress(
        transfer_id: str,
        received: int,
        total: int,
        done: bool = False,
        path: str = "",
        ok: bool = True,
        message: str = "",
    ) -> str:
        """进度回报：接收方 → 发送方（done=True 表示已结束，ok 表示是否成功）。"""
        return Protocol.create(
            MessageType.FILE_PROGRESS,
            {
                "transfer_id": str(transfer_id),
                "received": int(received),
                "total": int(total),
                "done": bool(done),
                "path": str(path),
                "ok": bool(ok),
                "message": str(message),
            },
        )

    @staticmethod
    def info(text: str) -> str:
        return Protocol.create(MessageType.INFO, {"text": text})

    @staticmethod
    def error(text: str) -> str:
        return Protocol.create(MessageType.ERROR, {"text": text})
