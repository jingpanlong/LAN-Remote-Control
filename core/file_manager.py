"""
文件信息与分块读取

仅做本地文件元数据计算与流式读取，不做网络传输。
"""

from __future__ import annotations

import hashlib
import logging
import os

logger = logging.getLogger("LAN_REMOTE")

CHUNK_SIZE = 1024 * 1024


class FileManager:
    """文件元信息与分块读取。"""

    CHUNK_SIZE = CHUNK_SIZE

    def file_info(self, path: str) -> dict:
        """返回 {"name", "size", "md5"}；文件不存在或不可读会抛 OSError。"""
        if not os.path.isfile(path):
            raise FileNotFoundError(f"文件不存在：{path}")
        return {
            "name": os.path.basename(path),
            "size": os.path.getsize(path),
            "md5": self.md5(path),
        }

    def md5(self, path: str) -> str:
        """流式计算 MD5，避免一次性把大文件读进内存。"""
        digest = hashlib.md5()
        with open(path, "rb") as fp:
            for chunk in iter(lambda: fp.read(CHUNK_SIZE), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def read_chunk(self, path: str, chunk_size: int = None):
        """按块迭代读取文件内容。"""
        size = int(chunk_size or CHUNK_SIZE)
        with open(path, "rb") as fp:
            for chunk in iter(lambda: fp.read(size), b""):
                yield chunk
