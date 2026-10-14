"""
安全模块

- Security   : 基于 Fernet 的对称加密，密钥持久化到 cache/secret.key
- Credentials: 带有效期的一次性访问密码（线程安全）
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time

from cryptography.fernet import Fernet, InvalidToken

import config

logger = logging.getLogger("LAN_REMOTE")


class SecurityError(Exception):
    """加解密失败。"""


class Security:
    """对称加密工具。密钥首次生成后落盘复用，保证重启后仍可解密历史数据。"""

    def __init__(self, key_file: str | None = None):
        self._key_file = key_file or os.path.join(config.CACHE_DIR, "secret.key")
        self._lock = threading.Lock()
        self._key = self._load_or_create_key()
        self._crypto = Fernet(self._key)

    def _load_or_create_key(self) -> bytes:
        try:
            if os.path.exists(self._key_file):
                with open(self._key_file, "rb") as fp:
                    key = fp.read().strip()
                if key:
                    return key
        except OSError as exc:
            logger.warning("读取密钥文件失败，将重新生成：%s", exc)

        key = Fernet.generate_key()
        try:
            with open(self._key_file, "wb") as fp:
                fp.write(key)
        except OSError as exc:
            logger.warning("写入密钥文件失败（本次仅内存有效）：%s", exc)
        return key

    def encrypt(self, data: bytes) -> bytes:
        with self._lock:
            return self._crypto.encrypt(data)

    def decrypt(self, data: bytes) -> bytes:
        with self._lock:
            try:
                return self._crypto.decrypt(data)
            except InvalidToken as exc:
                raise SecurityError("解密失败：密钥不匹配或数据已损坏") from exc

    def try_decrypt(self, data: bytes) -> bytes | None:
        try:
            return self.decrypt(data)
        except SecurityError:
            return None


class Credentials:
    """
    访问密码容器。

    服务端与界面共享同一个实例，因此界面点击“刷新密码”会立即生效。

    两种模式：
        - 临时密码：字母 + 数字随机生成，按 ttl 到期自动刷新（ttl <= 0 表示永不过期）；
        - 固定密码：由用户在设置里手动指定，永不过期，也不会被自动刷新。
    """

    # 去掉 0/O、1/l/I 这类肉眼易混的字符，口头转述密码时不容易错
    ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"

    def __init__(self, length: int = None, ttl: int = None):
        self._length = config.password_length() if length is None else int(length)
        self._ttl = config.password_expire() if ttl is None else int(ttl)
        self._fixed = False
        self._lock = threading.Lock()
        self._password = ""
        self._created = 0.0
        self.refresh()

    def refresh(self) -> str:
        """生成新密码并重置有效期，返回密码；固定密码模式下保持不变。"""
        with self._lock:
            if self._fixed:
                return self._password
            self._password = self._generate(self._length)
            self._created = time.time()
            return self._password

    @classmethod
    def _generate(cls, length: int) -> str:
        """字母 + 数字混合随机密码，保证至少各含一位字母与数字。"""
        length = max(4, int(length))
        letters = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ"
        digits = "23456789"
        chars = [secrets.choice(letters), secrets.choice(digits)]
        chars.extend(secrets.choice(cls.ALPHABET) for _ in range(length - 2))
        # Fisher-Yates 洗牌，避免字母/数字固定出现在固定位置
        for index in range(len(chars) - 1, 0, -1):
            swap = secrets.randbelow(index + 1)
            chars[index], chars[swap] = chars[swap], chars[index]
        return "".join(chars)

    def set_password(self, value: str) -> str:
        """显式设置密码（主要用于兼容外部传入的固定密码）。"""
        value = str(value)
        if not value:
            raise ValueError("密码不能为空")
        with self._lock:
            self._password = value
            self._created = time.time()
            return self._password

    def set_fixed_password(self, value: str) -> str:
        """
        设置固定密码：永不过期，且不会被自动刷新。

        固定密码允许任意可见字符（包括纯数字），长度至少 4 位。
        """
        value = str(value or "").strip()
        if len(value) < 4:
            raise ValueError("固定密码至少需要 4 位")
        with self._lock:
            self._fixed = True
            self._password = value
            self._created = time.time()
            self._ttl = 0
        return value

    def clear_fixed_password(self) -> str:
        """
        退出固定密码模式，恢复为随机临时密码。

        有效期回到用户在设置里配置的值（固定模式下 apply() 不会改写 ttl）。
        """
        with self._lock:
            self._fixed = False
            self._ttl = config.password_expire()
            self._created = time.time()
            self._password = self._generate(self._length)
            return self._password

    def apply(self, length: int = None, ttl: int = None) -> str:
        """
        应用设置里的密码位数与有效期，并立即刷新密码。

        固定密码模式下只更新参数、不改口令（退出固定模式时才会重新随机）。
        供设置界面保存后调用，保证新配置马上生效。
        """
        with self._lock:
            if length is not None:
                try:
                    value = int(length)
                    if 4 <= value <= 16:
                        self._length = value
                except (TypeError, ValueError):
                    pass
            if self._fixed:
                # 固定密码的语义就是「永不过期」：位数可以更新（退出固定后生效），
                # 但口令与有效期都不动
                return self._password
            if ttl is not None:
                try:
                    self._ttl = max(0, int(ttl))
                except (TypeError, ValueError):
                    pass
            self._password = self._generate(self._length)
            self._created = time.time()
            return self._password

    @property
    def password(self) -> str:
        with self._lock:
            return self._password

    @property
    def ttl(self) -> int:
        return self._ttl

    @property
    def is_fixed(self) -> bool:
        """是否处于固定密码模式。"""
        return self._fixed

    def is_expired(self) -> bool:
        if self._fixed or self._ttl <= 0:
            return False
        with self._lock:
            return (time.time() - self._created) >= self._ttl

    def remaining(self) -> int:
        """剩余有效秒数，永不过期返回 -1。"""
        if self._fixed or self._ttl <= 0:
            return -1
        with self._lock:
            return max(0, int(self._ttl - (time.time() - self._created)))

    def verify(self, value) -> tuple[bool, str]:
        """
        校验密码。

        返回 (是否通过, 失败原因)。
        """
        if not isinstance(value, str) or not value:
            return False, "密码为空"
        if self.is_expired():
            return False, "密码已过期，请在本机刷新后重试"
        with self._lock:
            ok = secrets.compare_digest(value, self._password)
        return (True, "") if ok else (False, "密码错误")
