"""
LAN Remote 主入口

修复要点：
    - 统一日志配置（文件轮转 + 控制台），不再重复挂载 handler
    - 同时捕获主线程与子线程未处理异常，避免线程静默死亡
    - 托盘模式下关闭窗口不退出程序，退出时统一释放端口与线程
    - 端口被占用时给出明确提示并自动切换可用端口
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import socket
import sys
import threading
import uuid

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication, QMessageBox

import config

logger = logging.getLogger("LAN_REMOTE")

_LOGGING_READY = False


# ======================================================================
# 日志
# ======================================================================
def setup_logging() -> None:
    global _LOGGING_READY
    if _LOGGING_READY:
        return

    level = logging.DEBUG if config.DEBUG else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)

    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(threadName)-12s | %(message)s"
    )

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            os.path.join(config.LOG_DIR, "app.log"),
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        # 安装到只读目录（如 Program Files）时无法写日志，降级为仅控制台输出
        pass

    # 打包为窗口程序（console=False）后 sys.stdout 为 None，必须判空
    if sys.stdout is not None:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)

    # 降低第三方库噪音
    for noisy in ("websockets.server", "websockets.client", "asyncio", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _LOGGING_READY = True


def install_exception_hooks() -> None:
    """捕获所有未处理异常，保证问题可追溯且不静默退出。"""

    def _excepthook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical("未捕获异常", exc_info=(exc_type, exc_value, exc_tb))

    def _thread_excepthook(args):
        if issubclass(args.exc_type, SystemExit):
            return
        logger.critical(
            "子线程 %s 未捕获异常",
            args.thread.name if args.thread else "unknown",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook


# ======================================================================
# 设备信息
# ======================================================================
def get_device_id() -> str:
    """设备唯一标识；优先使用本地记录，保证长期稳定。"""
    saved = config.get_setting("device_id")
    if saved:
        return str(saved)

    try:
        node = uuid.getnode()
    except Exception:
        node = 0

    if not node:
        node = int.from_bytes(os.urandom(6), "big") | (1 << 40)

    device_id = str(node)[-9:]
    config.set_setting("device_id", device_id)
    return device_id


# ======================================================================
# 服务端 / 客户端线程
# ======================================================================
def start_remote_service(server) -> None:
    """在独立线程中运行服务端事件循环。"""
    try:
        asyncio.run(server.start())
    except Exception:
        logger.exception("服务端异常退出")
    finally:
        logger.info("服务端线程结束")


class StatusRelay(QObject):
    """把后台线程的状态消息安全地转发到 Qt 主线程。"""

    message = pyqtSignal(str, str)


class AuthorizeRelay(QObject):
    """把后台线程的授权请求安全地转发到 Qt 主线程弹窗。"""

    request = pyqtSignal(dict)


# ======================================================================
# 主函数
# ======================================================================
def main() -> int:
    setup_logging()
    install_exception_hooks()

    logger.info("===== %s v%s 启动 =====", config.APP_NAME, config.VERSION)

    # ---------------------------------------------------------- Qt 应用
    # 必须先于单实例检查与服务启动：QLocalServer 依赖 QCoreApplication 实例
    app = QApplication(sys.argv)
    app.setApplicationName(config.APP_NAME)
    # 置空显示名，避免 Windows 在窗口标题后追加 “ - LAN Remote” 造成重复
    app.setApplicationDisplayName("")
    # 托盘常驻：关闭所有窗口不退出进程
    app.setQuitOnLastWindowClosed(False)
    # Fusion 基础样式：跨平台行为一致，且与自绘 QSS 配合最好
    app.setStyle("Fusion")

    # 全局主题（QSS + 程序化图标），必须早于任何窗口创建
    from ui import theme

    theme.apply(app)

    from ui.icons import app_icon

    # 全局应用图标：任何未单独设置图标的窗口都会继承它
    app.setWindowIcon(app_icon())

    # ---------------------------------------------------------- 单实例
    # 必须抢在启动服务与注册托盘之前，否则第二个实例会先占用端口、
    # 并多注册一个托盘图标（用户看到的"托盘里三个图标"就是这么来的）
    from core.single_instance import SingleInstanceGuard

    guard = SingleInstanceGuard()
    if not guard.try_acquire():
        logger.info("程序已在运行，本次启动直接退出")
        return 0

    # ---------------------------------------------------------- 环境检查
    try:
        from core.network import get_local_ip
    except Exception:
        logger.exception("加载网络模块失败")
        guard.release()
        return 1

    ip = get_local_ip()
    device_id = get_device_id()

    from core.discovery import DiscoveryService
    from core.security import Credentials
    from core.server import RemoteServer

    credentials = Credentials()
    discovery = DiscoveryService(device_id, config.server_port())
    discovery.start()
    # 注意：QMessageBox 只能在 QApplication 创建之后弹出，这里先记标志
    discovery_failed = not discovery.running

    server = RemoteServer(device_id, credentials, port=config.server_port(), discovery=discovery)

    logger.info("设备 ID：%s", device_id)
    logger.info("本机 IP：%s", ip)

    from ui.main_window import MainWindow
    from ui.tray import TrayManager

    # QApplication 就绪后，才允许弹出需要窗口环境的提示框
    if discovery_failed:
        QMessageBox.warning(
            None,
            "设备发现不可用",
            "局域网设备发现服务未能启动，将无法使用「只填设备 ID」的授权方式连接。\n"
            "仍可通过「目标 IP + 访问密码」正常连接。",
        )

    window = MainWindow(device_id, credentials, ip, server=server, discovery=discovery)

    relay = StatusRelay()
    relay.message.connect(window._on_status)

    auth_relay = AuthorizeRelay()
    auth_relay.request.connect(window._on_authorize_request)

    def on_server_status(text: str, level: str = "info") -> None:
        relay.message.emit(text, level)

    def on_authorize_request(info: dict) -> None:
        auth_relay.request.emit(dict(info))

    server.on_status = on_server_status
    server.on_authorize_request = on_authorize_request

    # 服务端先跑起来，配置好回调后再启动
    server_thread = threading.Thread(
        target=start_remote_service, args=(server,), name="RemoteServer", daemon=True
    )
    server_thread.start()

    tray = TrayManager(window)
    window.tray = tray
    window.show()

    # 用户重复双击 exe 时，把已有实例的主窗口唤到前台，而不是再开一个
    def _focus_window() -> None:
        window.showNormal()
        window.show()
        window.raise_()
        window.activateWindow()

    guard.on_activated(_focus_window)

    # 端口被占用时给出提示（不阻塞启动，用户可在“监听端口”处看到实际端口）
    from core.network import is_port_free

    preferred_port = config.server_port()
    if not is_port_free(preferred_port):
        message = (
            f"端口 {preferred_port} 已被占用（可能是 LAN Remote 的旧版本仍在运行），"
            "程序会自动切换到可用端口，连接方需填写相同的端口。"
        )
        logger.warning(message)
        window._append_log(message, "warning")
        tray.notify("端口被占用", message)

    # ---------------------------------------------------------- 退出收尾
    def on_about_to_quit() -> None:
        logger.info("正在退出…")
        server.stop()
        discovery.stop()
        window._cleanup()
        server_thread.join(timeout=3)
        guard.release()
        logger.info("===== 已退出 =====")

    app.aboutToQuit.connect(on_about_to_quit)

    exit_code = app.exec()
    logger.info("主循环结束，退出码 %s", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
