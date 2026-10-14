"""
文件传输

分成三部分：
    - FileTransfer：与网络无关的小工具（大小、分块读取、文件名清洗、重名避让）；
    - FileSender  ：一次「发送」会话，把本地文件按块产出 base64 报文；
    - FileReceiver：一次「接收」会话，边收边写临时文件，结束时校验 MD5 并落盘。

设计要点：
    - 传输与网络解耦：这里只产出 / 消费「报文」，收发由 client / server 负责，
      于是同一套逻辑控制端和被控端都能用，不需要两份实现；
    - 接收端先写 `.part` 临时文件，校验通过才改名，避免半截文件被当成完整文件；
    - 文件名经过清洗，杜绝 `../` 目录穿越；重名自动加序号，绝不覆盖已有文件；
    - 流式计算 MD5，不把整个文件读进内存（大文件也不怕）。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import uuid

import config

logger = logging.getLogger("LAN_REMOTE")

DEFAULT_CHUNK_SIZE = 1024 * 1024
PART_SUFFIX = ".part"


class FileTransfer:
    """文件传输辅助（静态方法）。"""

    @staticmethod
    def file_size(path: str) -> int:
        """返回文件字节数。"""
        return os.path.getsize(path)

    @staticmethod
    def read_chunk(path: str, size: int = DEFAULT_CHUNK_SIZE):
        """按块迭代读取文件内容。"""
        size = max(1, int(size))
        with open(path, "rb") as fp:
            for chunk in iter(lambda: fp.read(size), b""):
                yield chunk

    @staticmethod
    def safe_name(name: str) -> str:
        """清洗文件名，剥离路径分隔符，防止目录穿越。"""
        name = os.path.basename(str(name or "")).strip()
        for char in '<>:"/\\|?*':
            name = name.replace(char, "_")
        return name or "unnamed"

    @staticmethod
    def md5_of(path: str) -> str:
        """流式计算文件 MD5。"""
        digest = hashlib.md5()
        with open(path, "rb") as fp:
            for chunk in iter(lambda: fp.read(DEFAULT_CHUNK_SIZE), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def unique_path(directory: str, name: str) -> str:
        """在目录里找一个不冲突的路径：`a.txt` -> `a (1).txt`。"""
        name = FileTransfer.safe_name(name)
        candidate = os.path.join(directory, name)
        if not os.path.exists(candidate):
            return candidate

        stem, ext = os.path.splitext(name)
        for index in range(1, 10000):
            candidate = os.path.join(directory, f"{stem} ({index}){ext}")
            if not os.path.exists(candidate):
                return candidate
        return os.path.join(directory, f"{uuid.uuid4().hex}_{name}")

    @staticmethod
    def format_size(size: float) -> str:
        """人类可读大小。"""
        value = float(size or 0)
        if value < 1024:
            return f"{value:.0f} B"
        for unit in ("KB", "MB", "GB"):
            value /= 1024
            if value < 1024 or unit == "GB":
                return f"{value:.1f} {unit}"
        return f"{value:.1f} GB"


# ====================================================================== 发送
class FileSender:
    """一次文件发送会话。"""

    def __init__(
        self,
        path: str,
        transfer_id: str | None = None,
        chunk_size: int | None = None,
        sender: str = "",
    ):
        self.path = os.path.abspath(str(path))
        if not os.path.isfile(self.path):
            raise FileNotFoundError(f"文件不存在：{self.path}")

        self.size = os.path.getsize(self.path)
        self.name = os.path.basename(self.path)
        self.transfer_id = transfer_id or uuid.uuid4().hex[:12]
        self.chunk_size = int(chunk_size or config.file_chunk_size())
        self.sender = sender

        self.sent = 0
        self.cancelled = False
        self._md5: str | None = None

    # ------------------------------------------------------------------ 信息
    @property
    def received(self) -> int:
        """已发送字节数（与 FileReceiver.received 对齐，便于统一展示）。"""
        return self.sent

    @property
    def total(self) -> int:
        """文件总字节数。"""
        return self.size

    @property
    def progress(self) -> float:
        if self.size <= 0:
            return 1.0 if self.sent else 0.0
        return min(1.0, self.sent / self.size)

    def md5(self) -> str:
        """惰性计算（大文件耗时，调用方可以把它丢进线程池）。"""
        if self._md5 is None:
            self._md5 = FileTransfer.md5_of(self.path)
        return self._md5

    def info_message(self) -> str:
        """构造 FILE_INFO 报文。"""
        from core.protocol import ProtocolBuilder

        return ProtocolBuilder.file_info(
            self.transfer_id, self.name, self.size, self.md5(), self.sender
        )

    def cancel(self) -> None:
        self.cancelled = True

    # ------------------------------------------------------------------ 分块
    def chunks(self):
        """按块产出 FILE_DATA 报文；被 cancel() 后立即停止。"""
        from core.protocol import ProtocolBuilder

        seq = 0
        with open(self.path, "rb") as fp:
            while True:
                if self.cancelled:
                    return
                data = fp.read(self.chunk_size)
                if not data:
                    break
                self.sent += len(data)
                yield ProtocolBuilder.file_data(
                    self.transfer_id, seq, base64.b64encode(data).decode("ascii")
                )
                seq += 1

    def finish_message(self) -> str:
        from core.protocol import ProtocolBuilder

        return ProtocolBuilder.file_finish(self.transfer_id)

    def cancel_message(self, reason: str = "") -> str:
        from core.protocol import ProtocolBuilder

        return ProtocolBuilder.file_cancel(self.transfer_id, reason)


class FileReceiver:
    """一次文件接收会话：边收边写，收完校验。"""

    def __init__(
        self,
        info: dict,
        save_dir: str | None = None,
        max_size: int | None = None,
    ):
        self.transfer_id = str(info.get("transfer_id") or uuid.uuid4().hex[:12])
        self.name = FileTransfer.safe_name(info.get("name"))
        self.total = max(0, int(info.get("size") or 0))
        self.expect_md5 = str(info.get("md5") or "")
        self.sender = str(info.get("sender") or "")

        self.save_dir = os.path.abspath(str(save_dir or config.download_dir()))
        self.max_size = int(max_size if max_size is not None else config.file_max_size())

        self.received = 0
        self.finished = False
        self.ok = False
        self.error = ""
        self.path = ""

        os.makedirs(self.save_dir, exist_ok=True)
        # 先写隐藏的 .part 文件，校验通过再改成正式名字
        self._part_path = os.path.join(self.save_dir, f".{self.transfer_id}{PART_SUFFIX}")
        self._digest = hashlib.md5()
        self._fp = open(self._part_path, "wb")

    # ------------------------------------------------------------------ 属性
    @property
    def progress(self) -> float:
        if self.total <= 0:
            return 1.0 if self.finished else 0.0
        return min(1.0, self.received / self.total)

    # ------------------------------------------------------------------ 写入
    def write(self, seq: int, chunk_b64: str) -> int:
        """写入一块数据，返回累计已接收字节数。"""
        if self._fp is None:
            return self.received

        try:
            data = base64.b64decode(chunk_b64, validate=False)
        except Exception:
            self.error = "数据块解码失败"
            return self.received

        if self.total and self.received + len(data) > self.total:
            # 超出声明大小：立即停止，防塞满磁盘
            self.error = "收到的数据超过声明大小"
            return self.received
        if self.received + len(data) > self.max_size:
            self.error = "文件超过允许的最大体积"
            return self.received

        self._fp.write(data)
        self._digest.update(data)
        self.received += len(data)
        return self.received

    # ------------------------------------------------------------------ 收尾
    def finish(self) -> dict:
        """结束接收：校验并落盘。返回 {"ok","path","message","size","name"}。"""
        if self.finished:
            return {
                "ok": self.ok,
                "path": self.path,
                "message": self.error,
                "size": self.received,
                "name": self.name,
            }

        if self._fp is not None:
            try:
                self._fp.close()
            finally:
                self._fp = None

        self.finished = True

        if self.error:
            self._cleanup_part()
            return {
                "ok": False, "path": "", "message": self.error,
                "size": self.received, "name": self.name,
            }

        if self.total and self.received != self.total:
            self._cleanup_part()
            self.error = f"数据不完整（{self.received}/{self.total} 字节）"
            return {
                "ok": False, "path": "", "message": self.error,
                "size": self.received, "name": self.name,
            }

        actual_md5 = self._digest.hexdigest()
        if self.expect_md5 and actual_md5 != self.expect_md5:
            self._cleanup_part()
            self.error = "MD5 校验不一致，文件已丢弃"
            return {
                "ok": False, "path": "", "message": self.error,
                "size": self.received, "name": self.name,
            }

        target = FileTransfer.unique_path(self.save_dir, self.name)
        try:
            os.replace(self._part_path, target)
        except OSError as exc:
            self._cleanup_part()
            self.error = f"保存失败：{exc}"
            return {
                "ok": False, "path": "", "message": self.error,
                "size": self.received, "name": self.name,
            }

        self.ok = True
        self.path = target
        return {
            "ok": True,
            "path": target,
            "message": "",
            "size": self.received,
            "name": os.path.basename(target),
        }

    def abort(self) -> None:
        """中止接收并清理临时文件。"""
        if self._fp is not None:
            try:
                self._fp.close()
            except OSError:
                pass
            self._fp = None
        self.finished = True
        self.ok = False
        self._cleanup_part()

    # ------------------------------------------------------------------ 内部
    def _cleanup_part(self) -> None:
        if os.path.exists(self._part_path):
            try:
                os.remove(self._part_path)
            except OSError:
                logger.debug("清理临时文件失败：%s", self._part_path, exc_info=True)


# ====================================================================== 公共
async def stream_file(sender: FileSender, send, on_progress=None) -> int:
    """
    把 sender 的内容逐块交出去（控制端与被控端共用这一份发送逻辑）。

    send        异步可调用：`await send(message)`，负责把单条报文发出去；
    on_progress 可选回调：每块发出后调用一次 `on_progress(sender)`。

    逐块读取与 MD5 都在线程池里做，且每块之间让出事件循环，
    避免大文件传输把画面推流饿死。
    """
    import asyncio

    await asyncio.to_thread(sender.md5)

    def _next(chunks):
        try:
            return next(chunks)
        except StopIteration:
            return None

    chunks = sender.chunks()
    while True:
        message = await asyncio.to_thread(_next, chunks)
        if message is None:
            break
        await send(message)
        if on_progress is not None:
            on_progress(sender)
        await asyncio.sleep(0)

    return sender.sent
