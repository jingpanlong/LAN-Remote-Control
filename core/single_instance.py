"""
单实例守护

要不要单实例，直接决定托盘图标会不会"长出好几个"：
    程序默认允许被反复启动，而**每个实例都会注册一个系统托盘图标**。
    onefile 打包的 exe 启动较慢（要解压上百 MB），用户双击后以为没反应
    又点两下，托盘上就会并排出现三个图标 —— 现象就是这么来的。
    附带问题：多个实例还会互相抢占监听端口，导致端口一直向后回退。

做法：
    用 QLocalServer 抢占一个本地命名管道。抢到的就是"主实例"；
    后来的实例连上该管道、通知主实例把窗口显示出来，然后自己退出。

设计取舍：
    抢锁失败（例如管道被占且无法清理）时**不阻塞启动**，
    宁可多开也要保证程序能打开 —— 单实例只是体验优化，不是硬依赖。
"""

from __future__ import annotations

import logging

from PyQt6.QtNetwork import QLocalServer, QLocalSocket

logger = logging.getLogger("LAN_REMOTE")

SERVER_NAME = "LANRemote.SingleInstance.v2"
CONNECT_TIMEOUT_MS = 500


class SingleInstanceGuard:
    """保证同一时间只有一个实例在运行。"""

    def __init__(self, key: str = SERVER_NAME):
        self._key = key
        self._server: QLocalServer | None = None
        self._activated_callback = None

    # ------------------------------------------------------------------ 抢锁
    def try_acquire(self) -> bool:
        """
        尝试抢占单实例锁。

        返回 True  —— 本进程是主实例，可以继续初始化界面；
        返回 False —— 已有实例在运行，本进程应当立刻退出。
        """
        probe = QLocalSocket()
        probe.connectToServer(self._key)
        if probe.waitForConnected(CONNECT_TIMEOUT_MS):
            # 已有实例：通知它把主窗口显示出来，然后自己退场
            try:
                probe.write(b"show")
                probe.flush()
                probe.waitForBytesWritten(CONNECT_TIMEOUT_MS)
            except Exception:
                pass
            finally:
                try:
                    probe.disconnectFromServer()
                except Exception:
                    pass
            logger.info("检测到已有实例在运行，本实例退出")
            return False

        # 连接不上：可能是上一次异常退出留下的僵尸管道，先清理再监听
        try:
            QLocalServer.removeServer(self._key)
        except Exception:
            pass

        self._server = QLocalServer()
        # 仅允许当前用户连接，避免多用户会话互相干扰
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not self._server.listen(self._key):
            logger.warning(
                "单实例监听失败（%s），本次不启用单实例保护",
                self._server.errorString(),
            )
            self._server = None
            return True

        self._server.newConnection.connect(self._on_new_connection)
        return True

    def on_activated(self, callback) -> None:
        """注册「又有新实例被启动」时的回调（通常用来把主窗口显示到前台）。"""
        self._activated_callback = callback

    # ------------------------------------------------------------------ 内部
    def _on_new_connection(self) -> None:
        if self._server is None:
            return

        activated = False
        while self._server.hasPendingConnections():
            connection = self._server.nextPendingConnection()
            if connection is None:
                break
            activated = True
            try:
                connection.readAll()
            except Exception:
                pass
            try:
                connection.disconnectFromServer()
                connection.deleteLater()
            except Exception:
                pass

        if not activated:
            return

        logger.info("收到第二个实例的激活请求，显示主窗口")
        if self._activated_callback is not None:
            try:
                self._activated_callback()
            except Exception:
                logger.exception("激活回调执行失败")

    # ------------------------------------------------------------------ 释放
    def release(self) -> None:
        """退出时释放管道，避免下次启动被残留服务挡住。"""
        if self._server is None:
            return
        try:
            self._server.close()
        except Exception:
            pass
        self._server = None
