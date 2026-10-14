"""
手机网页访问服务

被控端在局域网内额外开一个 HTTP 服务（默认 9528）：
    GET /      手机浏览器访问的控制页（与桌面客户端同一 WebSocket 协议）
    GET /qr    访问链接的二维码 PNG
    GET /health  健康检查

网页本身不含任何权限——真正的鉴权仍由 WebSocket 服务端的
登录认证（临时密码 / 固定密码 / 授权模式）完成。
"""

from __future__ import annotations

import io
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

import config

logger = logging.getLogger("LAN_REMOTE")

_PAGE_PATH = os.path.join(config.BASE_DIR, "assets", "web_remote.html")


def render_qr_png(text: str, box_size: int = 8) -> bytes:
    """把文本渲染成二维码 PNG（依赖 qrcode 库，缺失时返回空 bytes）。"""
    try:
        import qrcode
    except ImportError:
        logger.warning("未安装 qrcode 库，无法生成二维码（pip install qrcode）")
        return b""

    maker = qrcode.QRCode(box_size=box_size, border=2)
    maker.add_data(text)
    maker.make(fit=True)
    image = maker.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    try:
        image.save(buffer, format="PNG")
    except Exception:
        logger.exception("二维码渲染失败")
        return b""
    return buffer.getvalue()


class _Handler(BaseHTTPRequestHandler):
    """极简 HTTP 处理器：只读服务，不接收任何上传数据。"""

    def log_message(self, fmt, *args):  # noqa: N802
        logger.debug("[网页访问] %s", fmt % args)

    # ------------------------------------------------------------ 工具
    def _send(self, status: int, content_type: str, body: bytes) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ------------------------------------------------------------ 路由
    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            html = self.server.lan_remote.render_page()
            if html:
                self._send(200, "text/html; charset=utf-8", html)
            else:
                self._send(500, "text/plain; charset=utf-8", "控制页加载失败".encode("utf-8"))
        elif path == "/qr":
            url = self.server.lan_remote.access_url(self.headers.get("Host") or "")
            png = render_qr_png(url)
            if png:
                self._send(200, "image/png", png)
            else:
                self._send(501, "text/plain; charset=utf-8", "QR_CODE_UNAVAILABLE".encode("utf-8"))
        elif path == "/health":
            self._send(200, "text/plain; charset=utf-8", b"ok")
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")


class _WebHTTPServer(ThreadingHTTPServer):
    """持有 WebAccessServer 引用的 HTTP 服务。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, lan_remote: "WebAccessServer"):
        super().__init__(address, handler)
        self.lan_remote = lan_remote


class WebAccessServer:
    """手机网页访问的 HTTP 服务（线程化，独立于 WebSocket 服务）。"""

    def __init__(
        self,
        device_id: str,
        ws_port_provider: Callable[[], int],
        host: str = "0.0.0.0",
        port: int = None,
        port_fallback: int = None,
    ):
        self.device_id = str(device_id)
        # WebSocket 服务端口（可能因占用而回退，因此用 provider 动态取）
        self.ws_port_provider = ws_port_provider
        self.host = host
        self.preferred_port = int(port or config.web_port())
        self.port_fallback = int(port_fallback or config.web_port_fallback())
        self.actual_port = 0
        self.running = False

        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._page_cache: bytes | None = None

    # ============================================================== 页面
    def _load_template(self) -> str:
        try:
            with open(_PAGE_PATH, "r", encoding="utf-8") as fp:
                return fp.read()
        except OSError:
            logger.exception("网页控制页资源缺失：%s", _PAGE_PATH)
            return ""

    def render_page(self) -> bytes:
        """渲染控制页（每次请求实时替换 WS 端口，自适应端口回退）。"""
        if self._page_cache is None:
            template = self._load_template()
            self._page_cache = template.encode("utf-8")
        if not self._page_cache:
            return b""
        ws_port = int(self.ws_port_provider() or 0)
        html = (
            self._page_cache.decode("utf-8")
            .replace("{{WS_PORT}}", str(ws_port))
            .replace("{{DEVICE_ID}}", self.device_id)
            .replace("{{VERSION}}", config.VERSION)
        )
        return html.encode("utf-8")

    # ============================================================== 地址
    def access_url(self, host_header: str = "") -> str:
        """访问链接。host_header 非空（HTTP 请求场景）时以其主机名为准。"""
        if host_header:
            # Host 头可能带端口，统一换成网页端口
            host = host_header.split(":", 1)[0]
            if host:
                return f"http://{host}:{self.actual_port}/"
        return f"http://<本机IP>:{self.actual_port}/"

    # ============================================================== 生命周期
    def start(self) -> bool:
        if self.running:
            return True

        httpd = None
        for offset in range(0, max(1, self.port_fallback) + 1):
            port = self.preferred_port + offset
            try:
                httpd = _WebHTTPServer((self.host, port), _Handler, self)
                self.actual_port = port
                break
            except OSError:
                continue

        if httpd is None:
            logger.error(
                "[网页访问] 端口 %s~%s 均不可用，手机网页访问不可用",
                self.preferred_port,
                self.preferred_port + self.port_fallback,
            )
            return False

        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="WebAccess", daemon=True
        )
        self._thread.start()
        self.running = True
        logger.info("[网页访问] 已启动，监听 0.0.0.0:%s（手机浏览器可访问）", self.actual_port)
        return True

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                logger.exception("[网页访问] 停止失败")
        self._httpd = None
        self._thread = None
        logger.info("[网页访问] 已停止")
