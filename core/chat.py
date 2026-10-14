"""
聊天消息构造

统一走 core.protocol，避免出现第二套报文格式。
"""

from __future__ import annotations

import json

from core.protocol import MessageType, Protocol, ProtocolBuilder


class Chat:
    """聊天工具（静态方法）。"""

    @staticmethod
    def create(text: str, sender: str = "") -> str:
        """构造标准聊天消息。"""
        return ProtocolBuilder.chat(text, sender)

    @staticmethod
    def parse(message) -> dict | None:
        """解析聊天消息，返回 {"text":..., "sender":...}；非聊天消息返回 None。"""
        data = Protocol.parse(message)
        if not data or data.get("type") != MessageType.CHAT:
            return None
        body = data.get("data") or {}
        return {"text": body.get("text", ""), "sender": body.get("sender", "")}

    @staticmethod
    def legacy(text: str) -> str:
        """旧版 {"action":"chat"} 格式，仅供兼容历史客户端。"""
        return json.dumps({"action": "chat", "text": text}, ensure_ascii=False)
