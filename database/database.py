"""
LAN Remote 本地数据库

用于记录最近连接过的设备（历史设备），支持快速重连。

要点：
    - 数据库文件使用绝对路径，避免受工作目录影响
    - 建表后会自动迁移旧库（补齐 port / note 字段），升级不丢数据
    - 所有写操作都在锁内完成，可从任意线程调用
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime

import config

logger = logging.getLogger("LAN_REMOTE")

DB_FILE = os.path.join(config.CACHE_DIR, "remote.db")

# 需要保证存在的列：(列名, 追加时的 DDL 片段)
_EXTRA_COLUMNS = (
    ("port", "INTEGER DEFAULT 0"),
    ("note", "TEXT DEFAULT ''"),
    ("favorite", "INTEGER DEFAULT 0"),
)


def now_text() -> str:
    """统一的时间戳格式，保证按文本排序等价于按时间排序。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Database:
    """SQLite 封装（线程安全）。"""

    def __init__(self, db_file: str = None):
        self.db_file = db_file or DB_FILE
        os.makedirs(os.path.dirname(self.db_file), exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_file, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.Error as exc:
            logger.warning("设置 SQLite 参数失败：%s", exc)
        self.create_table()

    # ------------------------------------------------------------------ 建表
    def create_table(self) -> None:
        sql = """
            CREATE TABLE IF NOT EXISTS devices (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                name      TEXT,
                ip        TEXT,
                port      INTEGER DEFAULT 0,
                note      TEXT DEFAULT '',
                last_time TEXT
            )
        """
        with self._lock:
            try:
                self.conn.execute(sql)
                self.conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_devices_device_id ON devices(device_id)"
                )
                self.conn.commit()
            except sqlite3.Error as exc:
                logger.error("初始化数据表失败：%s", exc)
                return
        self._migrate()

    def _migrate(self) -> None:
        """为旧版本数据库补齐新增字段。"""
        with self._lock:
            try:
                rows = self.conn.execute("PRAGMA table_info(devices)").fetchall()
                existing = {row["name"] for row in rows}
            except sqlite3.Error as exc:
                logger.error("读取表结构失败：%s", exc)
                return

            for column, ddl in _EXTRA_COLUMNS:
                if column in existing:
                    continue
                try:
                    # column / ddl 均为本模块内的硬编码常量，不存在注入风险
                    self.conn.execute(f"ALTER TABLE devices ADD COLUMN {column} {ddl}")
                    self.conn.commit()
                    logger.info("设备表已补充字段：%s", column)
                except sqlite3.Error as exc:
                    logger.error("补充字段 %s 失败：%s", column, exc)

    # ------------------------------------------------------------------ 写入
    def add_device(
        self,
        device_id: str,
        name: str = "",
        ip: str = "",
        port: int = 0,
        last_time: str = "",
    ) -> int | None:
        """新增一条设备记录，返回记录 id。"""
        sql = """
            INSERT INTO devices (device_id, name, ip, port, note, last_time)
            VALUES (?, ?, ?, ?, '', ?)
        """
        with self._lock:
            try:
                cursor = self.conn.execute(
                    sql, (str(device_id), name, ip, int(port or 0), last_time or now_text())
                )
                self.conn.commit()
                return cursor.lastrowid
            except sqlite3.Error as exc:
                logger.error("写入设备记录失败：%s", exc)
                return None

    def upsert_device(
        self,
        device_id: str,
        name: str = "",
        ip: str = "",
        port: int = 0,
        last_time: str = "",
        note: str | None = None,
    ) -> None:
        """
        按 device_id 去重写入：存在则更新，不存在则插入。

        note 传 None 表示保留原有备注（连接记录不该覆盖用户手写的备注）。
        """
        device_id = str(device_id)
        last_time = last_time or now_text()
        try:
            port_value = int(port or 0)
        except (TypeError, ValueError):
            port_value = 0

        with self._lock:
            try:
                row = self.conn.execute(
                    "SELECT id FROM devices WHERE device_id = ?", (device_id,)
                ).fetchone()
                if row:
                    if note is None:
                        self.conn.execute(
                            "UPDATE devices SET name = ?, ip = ?, port = ?, last_time = ? WHERE id = ?",
                            (name, ip, port_value, last_time, row["id"]),
                        )
                    else:
                        self.conn.execute(
                            "UPDATE devices SET name = ?, ip = ?, port = ?, last_time = ?, note = ? WHERE id = ?",
                            (name, ip, port_value, last_time, note, row["id"]),
                        )
                else:
                    self.conn.execute(
                        "INSERT INTO devices (device_id, name, ip, port, note, last_time) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (device_id, name, ip, port_value, note or "", last_time),
                    )
                self.conn.commit()
            except sqlite3.Error as exc:
                logger.error("更新设备记录失败：%s", exc)

    def set_note(self, record_id: int, note: str) -> bool:
        """更新某条记录的备注。"""
        with self._lock:
            try:
                self.conn.execute(
                    "UPDATE devices SET note = ? WHERE id = ?", (str(note or ""), int(record_id))
                )
                self.conn.commit()
                return True
            except (sqlite3.Error, TypeError, ValueError) as exc:
                logger.error("更新备注失败：%s", exc)
                return False

    def set_favorite(self, record_id: int, favorite: bool) -> bool:
        """收藏 / 取消收藏某条设备记录（收藏的会排在列表最前面）。"""
        with self._lock:
            try:
                self.conn.execute(
                    "UPDATE devices SET favorite = ? WHERE id = ?",
                    (1 if favorite else 0, int(record_id)),
                )
                self.conn.commit()
                return True
            except (sqlite3.Error, TypeError, ValueError) as exc:
                logger.error("更新收藏状态失败：%s", exc)
                return False

    # ------------------------------------------------------------------ 删除
    def delete_device(self, record_id: int) -> bool:
        with self._lock:
            try:
                self.conn.execute("DELETE FROM devices WHERE id = ?", (int(record_id),))
                self.conn.commit()
                return True
            except (sqlite3.Error, TypeError, ValueError) as exc:
                logger.error("删除设备记录失败：%s", exc)
                return False

    def delete_by_device_id(self, device_id: str) -> bool:
        with self._lock:
            try:
                self.conn.execute("DELETE FROM devices WHERE device_id = ?", (str(device_id),))
                self.conn.commit()
                return True
            except sqlite3.Error as exc:
                logger.error("删除设备记录失败：%s", exc)
                return False

    def clear_devices(self) -> bool:
        """清空全部历史设备。"""
        with self._lock:
            try:
                self.conn.execute("DELETE FROM devices")
                self.conn.commit()
                return True
            except sqlite3.Error as exc:
                logger.error("清空设备记录失败：%s", exc)
                return False

    # ------------------------------------------------------------------ 查询
    def get_devices(self, limit: int | None = None) -> list[dict]:
        """按「收藏优先 + 最近连接时间倒序」返回历史设备。"""
        sql = "SELECT * FROM devices ORDER BY favorite DESC, last_time DESC, id DESC"
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (int(limit),)
        with self._lock:
            try:
                rows = self.conn.execute(sql, params).fetchall()
                return [dict(row) for row in rows]
            except sqlite3.Error as exc:
                logger.error("查询设备记录失败：%s", exc)
                return []

    def count(self) -> int:
        with self._lock:
            try:
                row = self.conn.execute("SELECT COUNT(*) AS n FROM devices").fetchone()
                return int(row["n"]) if row else 0
            except sqlite3.Error:
                return 0

    # ------------------------------------------------------------------ 关闭
    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
