"""
NAT 穿透接口占位实现

当前仅支持局域网直连，这里保留接口以便后续接入中继/打洞方案。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("LAN_REMOTE")


class NAT:
    """中继服务接口。"""

    def __init__(self):
        self.server = None

    def connect_server(self, address: str) -> None:
        """记录中继服务器地址。"""
        self.server = address
        logger.info("已配置中继服务器：%s", address)

    def relay(self, data):
        """透传数据。未配置中继时原样返回。"""
        if self.server is None:
            logger.debug("未配置中继服务器，数据未转发")
        return data

    @property
    def enabled(self) -> bool:
        return self.server is not None
