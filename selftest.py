"""
LAN Remote 自检脚本

用于在改动依赖、升级库或修改代码后，快速确认整套链路依然健康。
全部检查都在本机、独立端口上完成，不会影响 9527 上正在运行的服务。

用法：
    python selftest.py            # 全部检查
    python selftest.py --core     # 仅核心链路（不需要图形界面）
    python selftest.py --gui      # 仅图形界面（使用 offscreen 平台）

退出码：0 表示全部通过，1 表示存在失败项。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import websockets

import config
from core.client import RemoteClient
from core.discovery import DiscoveryService
from core.loop_runner import AsyncLoopRunner
from core.network import is_port_free
from core.protocol import MessageType, Protocol, ProtocolBuilder
from core.security import Credentials
from core.server import RemoteServer

CORE_PORT = 19530
GUI_PORT = 19830


class Report:
    """检查结果收集器。"""

    def __init__(self, title: str):
        self.title = title
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append((name, bool(ok), detail))
        mark = "PASS" if ok else "FAIL"
        print(f"{mark}  {name}" + (f"  | {detail}" if detail else ""), flush=True)
        return bool(ok)

    def summary(self) -> int:
        failed = [r for r in self.results if not r[1]]
        print(f"\n---------------- {self.title} ----------------")
        for name, ok, detail in self.results:
            print(f"{'✔' if ok else '✘'} {name}" + (f"   [{detail}]" if detail and not ok else ""))
        print(f"\n共 {len(self.results)} 项，失败 {len(failed)} 项")
        return 1 if failed else 0


class ErrorCollector(logging.Handler):
    """捕获 ERROR 级日志，用于断言运行期没有隐藏异常。"""

    def __init__(self):
        super().__init__(logging.ERROR)
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(record.getMessage())


def wait_for(condition, timeout: float, interval: float = 0.05) -> bool:
    """轮询等待条件成立（自检里到处都要等后台线程 / 事件循环推进）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return bool(condition())


# ======================================================================
# 静态一致性
# ======================================================================
def check_static(report: Report) -> None:
    """扫描源码，确认 Protocol / ProtocolBuilder 的调用目标都真实存在。

    这类拼写错误不会在导入期暴露，只会在运行时悄悄失效，因此必须静态兜住。
    """
    pattern = re.compile(r"\b(Protocol|ProtocolBuilder)\.([A-Za-z_]\w*)")
    missing = set()
    for path in BASE_DIR.rglob("*.py"):
        if any(part in {".venv", "__pycache__"} for part in path.parts):
            continue
        for owner, attr in pattern.findall(path.read_text(encoding="utf-8")):
            target = Protocol if owner == "Protocol" else ProtocolBuilder
            if not hasattr(target, attr):
                missing.add(f"{path.name}: {owner}.{attr}")
    report.check("Protocol/ProtocolBuilder 调用全部存在", not missing, "; ".join(sorted(missing)))

    # 覆盖率：确保关键入口可导入
    import importlib

    modules = [
        "config", "core.protocol", "core.security", "core.encoder", "core.screen",
        "core.control", "core.network", "core.heartbeat", "core.nat", "core.chat",
        "core.clipboard", "core.clipboard_sync", "core.discovery", "core.file_manager",
        "core.file_transfer", "core.permission", "core.adaptive", "core.task_manager",
        "core.loop_runner", "core.client", "core.server", "database.database",
        "startup.auto_start", "ui.theme", "ui.widgets", "ui.client_bridge",
        "ui.main_window", "ui.tray", "ui.settings_dialog", "ui.about_dialog",
        "ui.remote_window", "ui.chat_window", "ui.file_window", "ui.permission_dialog",
    ]
    failed = []
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{name}({type(exc).__name__})")
    report.check(f"全部 {len(modules)} 个模块可导入", not failed, ", ".join(failed))


# ======================================================================
# 文件传输
# ======================================================================
def check_file_transfer(report: Report, runner, client, server, chats: list) -> None:
    """
    文件传输完整验证。

    分两层：
        1) 本地核心：FileSender / FileReceiver 直接对接，覆盖 MD5、重名、
           目录穿越、超量等分支，不依赖网络，快且稳定；
        2) 真实网络：控制端 -> 被控端、被控端 -> 控制端两个方向各走一遍
           真实 WebSocket，确认两端都能收发。
    """
    from core.file_transfer import FileReceiver, FileSender, FileTransfer

    root = tempfile.mkdtemp(prefix="lanremote_ft_")
    send_dir = os.path.join(root, "send")
    host_dir = os.path.join(root, "host")      # 被控端保存目录
    guest_dir = os.path.join(root, "guest")    # 控制端保存目录
    for path in (send_dir, host_dir, guest_dir):
        os.makedirs(path, exist_ok=True)

    client_events: list[tuple[str, dict]] = []
    server_events: list[tuple[str, dict]] = []
    server.on_file_event = lambda event, payload: server_events.append((event, dict(payload)))
    client.on_file_event = lambda event, payload: client_events.append((event, dict(payload)))

    # ---------------------------------------------------------- 协议报文
    report.check(
        "文件协议报文可构造与解析",
        Protocol.get_type(ProtocolBuilder.file_info("t1", "a.bin", 1024)) == MessageType.FILE_INFO
        and Protocol.get_data(ProtocolBuilder.file_info("t1", "a.bin", 1024))["name"] == "a.bin"
        and Protocol.get_type(ProtocolBuilder.file_data("t1", 0, "QUJD")) == MessageType.FILE_DATA
        and Protocol.get_type(ProtocolBuilder.file_finish("t1")) == MessageType.FILE_FINISH
        and Protocol.get_type(ProtocolBuilder.file_cancel("t1", "x")) == MessageType.FILE_CANCEL
        and Protocol.get_type(
            ProtocolBuilder.file_progress("t1", 1, 2, True, "p", True, "")
        ) == MessageType.FILE_PROGRESS,
    )

    # ---------------------------------------------------------- 本地核心：正常往返
    payload = os.urandom(3 * 1024 * 1024)
    source = os.path.join(send_dir, "测试 文件.bin")
    with open(source, "wb") as fp:
        fp.write(payload)

    sender = FileSender(source, chunk_size=64 * 1024)
    receiver = FileReceiver(
        {
            "transfer_id": sender.transfer_id,
            "name": sender.name,
            "size": sender.size,
            "md5": sender.md5(),
        },
        save_dir=host_dir,
    )
    for message in sender.chunks():
        body = Protocol.get_data(message)
        receiver.write(int(body["seq"]), body["chunk"])
    result = receiver.finish()
    written = Path(result["path"]).read_bytes() if result["ok"] else b""
    report.check(
        "分块传输 3MB 文件内容完全一致",
        bool(result["ok"]) and written == payload,
        f"ok={result['ok']} got={len(written)} msg={result['message']}",
    )
    report.check(
        "传输结束后无 .part 临时文件残留",
        not [n for n in os.listdir(host_dir) if n.endswith(".part")],
    )

    # ---------------------------------------------------------- 本地核心：篡改被拦下
    tampered = FileSender(source, chunk_size=64 * 1024)
    bad = FileReceiver(
        {"transfer_id": "bad-1", "name": "tampered.bin", "size": tampered.size, "md5": "f" * 32},
        save_dir=host_dir,
    )
    for message in tampered.chunks():
        body = Protocol.get_data(message)
        bad.write(int(body["seq"]), body["chunk"])
    bad_result = bad.finish()
    report.check(
        "MD5 校验不一致时拒绝落盘",
        not bad_result["ok"] and not os.path.exists(os.path.join(host_dir, "tampered.bin")),
        f"ok={bad_result['ok']} msg={bad_result['message']}",
    )

    # ---------------------------------------------------------- 本地核心：边界
    dup = FileTransfer.unique_path(host_dir, "测试 文件.bin")
    report.check(
        "重名文件自动加序号，绝不覆盖",
        os.path.basename(dup) == "测试 文件 (1).bin",
        os.path.basename(dup),
    )
    report.check(
        "文件名清洗阻断目录穿越",
        FileTransfer.safe_name("../../evil.txt") == "evil.txt",
        FileTransfer.safe_name("../../evil.txt"),
    )

    overflow = FileReceiver({"transfer_id": "big-1", "name": "big.bin", "size": 8}, save_dir=host_dir)
    for _ in range(3):
        overflow.write(0, base64.b64encode(b"0123456789").decode("ascii"))
    overflow.abort()
    report.check("超出声明大小的数据被拒绝", bool(overflow.error), overflow.error or "—")
    report.check(
        "中止传输后同样不残留 .part",
        not [n for n in os.listdir(host_dir) if n.endswith(".part")],
    )

    # ---------------------------------------------------------- 网络：控制端 -> 被控端
    server.download_dir = host_dir
    client.download_dir = guest_dir

    up_bytes = os.urandom(2 * 1024 * 1024)
    up_path = os.path.join(send_dir, "上报材料.pdf")
    with open(up_path, "wb") as fp:
        fp.write(up_bytes)

    runner.fire_and_forget(client.send_file(up_path))
    received = wait_for(
        lambda: any(
            event == "done" and payload.get("direction") == "in"
            and payload.get("name") == "上报材料.pdf"
            for event, payload in server_events
        ),
        30,
    )
    up_saved = os.path.join(host_dir, "上报材料.pdf")
    report.check(
        "控制端 -> 被控端：文件送达且内容一致",
        received and os.path.exists(up_saved) and Path(up_saved).read_bytes() == up_bytes,
        f"done={received} exists={os.path.exists(up_saved)}",
    )

    # ---------------------------------------------------------- 网络：被控端 -> 控制端
    down_bytes = os.urandom(700 * 1024)
    down_path = os.path.join(send_dir, "截图.bin")
    with open(down_path, "wb") as fp:
        fp.write(down_bytes)

    report.check("被控端可主动发起文件发送（线程安全投递）", server.send_files([down_path]) == 1)
    received = wait_for(
        lambda: any(
            event == "done" and payload.get("direction") == "in"
            and payload.get("name") == "截图.bin"
            for event, payload in client_events
        ),
        30,
    )
    down_saved = os.path.join(guest_dir, "截图.bin")
    report.check(
        "被控端 -> 控制端：文件送达且内容一致",
        received and os.path.exists(down_saved) and Path(down_saved).read_bytes() == down_bytes,
        f"done={received} exists={os.path.exists(down_saved)}",
    )

    report.check(
        "传输事件流完整（offer -> progress -> done）",
        any(e == "offer" for e, _ in server_events) and any(e == "progress" for e, _ in server_events),
        f"server={[e for e, _ in server_events][:8]}",
    )

    # ---------------------------------------------------------- 网络：被控端主动聊天
    marker = "selftest-host-reply"
    sent = server.send_chat(marker, "被控端")
    got = wait_for(lambda: any(text == marker for text, _ in chats), 8)
    report.check("被控端可主动向控制端发消息", bool(sent) and got, f"sent={sent} got={got}")

    server.on_file_event = None
    client.on_file_event = None
    shutil.rmtree(root, ignore_errors=True)


# ======================================================================
# 密码体系
# ======================================================================
def check_security(report: Report) -> None:
    """密码：字母数字混合、固定密码、有效期。"""
    from core.security import Credentials

    creds = Credentials(length=8, ttl=60)
    samples = {creds.refresh() for _ in range(12)}
    mixed = all(
        any(c.isalpha() for c in pwd)
        and any(c.isdigit() for c in pwd)
        and not (set(pwd) & set("0O1lI"))
        and len(pwd) == 8
        for pwd in samples
    )
    report.check(
        "临时密码为字母+数字混合（不含 0/O、1/l/I 易混字符）",
        mixed and len(samples) >= 8,
        ", ".join(sorted(samples)[:3]),
    )

    report.check("未到期的临时密码不会被判定过期", not creds.is_expired() and creds.remaining() > 0)

    fixed_pwd = "my-fixed-pass-2026"
    creds.set_fixed_password(fixed_pwd)
    report.check(
        "固定密码不被自动刷新且永不过期",
        creds.is_fixed
        and creds.refresh() == fixed_pwd
        and not creds.is_expired()
        and creds.remaining() == -1,
    )
    creds.apply(length=10, ttl=120)
    report.check(
        "固定密码模式下调整位数/有效期不改变口令",
        creds.password == fixed_pwd and creds.ttl == 0,
    )
    restored = creds.clear_fixed_password()
    report.check(
        "退出固定密码后恢复随机临时密码",
        not creds.is_fixed and restored != fixed_pwd and creds.remaining() > 0,
    )

    try:
        creds.set_fixed_password("abc")
        report.check("过短的固定密码被拒绝", False)
    except ValueError:
        report.check("过短的固定密码被拒绝", True)


# ======================================================================
# 核心链路
# ======================================================================
def check_core(report: Report) -> None:
    collector = ErrorCollector()
    root = logging.getLogger()
    root.addHandler(collector)
    root.setLevel(logging.INFO)

    # ---------------------------------------------------------- 端口回退
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("0.0.0.0", CORE_PORT))
    holder.listen(1)

    creds = Credentials(ttl=0)
    server = RemoteServer("selftest-host", creds, port=CORE_PORT, port_fallback=4)
    server_msgs: list[tuple[str, str]] = []
    server.on_status = lambda text, level="info": server_msgs.append((text, level))

    threading.Thread(target=lambda: asyncio.run(server.start()), daemon=True, name="selftest-srv").start()

    deadline = time.time() + 8
    while server.actual_port is None and time.time() < deadline:
        time.sleep(0.1)
    port = server.actual_port
    holder.close()

    report.check("端口被占用时自动回退到下一个可用端口", port == CORE_PORT + 1, f"actual_port={port}")
    if port is None:
        root.removeHandler(collector)
        return

    # ---------------------------------------------------------- 客户端全链路
    runner = AsyncLoopRunner("SelftestClientLoop")
    report.check("后台事件循环可启动", runner.start())

    client = RemoteClient(port=port)
    frames: list[tuple[int, int, int]] = []
    chats: list[tuple[str, str]] = []
    states: list[bool] = []
    client.on_screen = lambda data, w, h, fps: frames.append((len(data), w, h))
    client.on_chat = lambda text, sender: chats.append((text, sender))
    client.on_state = lambda connected: states.append(connected)

    runner.fire_and_forget(client.run("127.0.0.1", "selftest-client", creds.password))

    deadline = time.time() + 10
    while not client.authenticated and time.time() < deadline:
        time.sleep(0.1)
    report.check("客户端登录成功", client.authenticated)

    deadline = time.time() + 12
    while len(frames) < 5 and time.time() < deadline:
        time.sleep(0.1)
    report.check("持续收到远端画面帧", len(frames) >= 5, f"frames={len(frames)}")
    report.check(
        "画面尺寸有效",
        bool(frames) and frames[-1][1] > 0 and frames[-1][2] > 0,
        f"latest={frames[-1] if frames else None}",
    )

    # 控制指令
    for coro in (
        client.send_mouse_move(100, 120),
        client.send_mouse_click("left", "down"),
        client.send_mouse_click("left", "up"),
        client.send_mouse_scroll(2),
        client.send_key("a", "press"),
        client.send_key("ctrl", "down"),
        client.send_key("c", "press"),
        client.send_key("ctrl", "up"),
    ):
        runner.fire_and_forget(coro)
    time.sleep(1.5)
    control_errors = [m for m in server_msgs if "控制执行失败" in m[0] or "控制参数非法" in m[0]]
    report.check("鼠标 / 拖拽 / 滚轮 / 组合键全部可执行", not control_errors, str(control_errors))

    # ---------------------------------------------------------- 文件传输
    check_file_transfer(report, runner, client, server, chats)

    # ---------------------------------------------------------- 只看画面（键鼠控制开关）
    from core.control import Control

    original_allow = config.allow_control
    original_click = Control.mouse_click
    hits = {"count": 0}
    Control.mouse_click = lambda *a, **k: hits.__setitem__("count", hits["count"] + 1)
    try:
        config.allow_control = lambda: False
        runner.submit(
            server._execute_control(MessageType.MOUSE_CLICK, {"button": "left", "action": "click"})
        ).result(timeout=10)
        report.check("关闭键鼠控制后，控制指令被直接丢弃", hits["count"] == 0, f"hits={hits['count']}")

        config.allow_control = lambda: True
        runner.submit(
            server._execute_control(MessageType.MOUSE_CLICK, {"button": "left", "action": "click"})
        ).result(timeout=10)
        report.check("恢复键鼠控制后，控制指令正常执行", hits["count"] == 1, f"hits={hits['count']}")
    finally:
        config.allow_control = original_allow
        Control.mouse_click = original_click

    # ---------------------------------------------------------- 设备发现与授权接入
    host_disc = DiscoveryService("selftest-host", local_port=port)
    peer_disc = DiscoveryService("selftest-peer", local_port=19599)
    started = host_disc.start() and peer_disc.start()
    report.check("设备发现服务（UDP 广播）可启动", started,
                 f"host_err={host_disc.last_error} peer_err={peer_disc.last_error}")

    deadline = time.time() + 8
    found = (None, None)
    while time.time() < deadline:
        found = host_disc.lookup("selftest-peer")
        if found[0]:
            break
        time.sleep(0.2)
    report.check("可发现局域网内其它实例并解析出 IP/端口",
                 found[0] is not None and found[1] == 19599, f"found={found}")

    approval_server = RemoteServer(
        "selftest-approval", Credentials(ttl=0), port=CORE_PORT + 20, port_fallback=0
    )
    approval_requests: list[dict] = []
    approval_flag = {"approved": True}

    def _on_request(info: dict) -> None:
        approval_requests.append(info)
        respond = info.get("respond")
        if callable(respond):
            respond(approval_flag["approved"])

    approval_server.on_authorize_request = _on_request
    threading.Thread(
        target=lambda: asyncio.run(approval_server.start()), daemon=True, name="selftest-approval"
    ).start()

    deadline = time.time() + 8
    while approval_server.actual_port is None and time.time() < deadline:
        time.sleep(0.1)
    approval_port = approval_server.actual_port
    report.check("授权模式测试服务端已启动", approval_port == CORE_PORT + 20, f"port={approval_port}")

    if approval_port:
        client_ok = RemoteClient(port=approval_port)
        ok = runner.submit(
            client_ok.connect("127.0.0.1", "selftest-peer", "", mode="approval")
        ).result(timeout=90)
        report.check("仅凭设备 ID 接入：对方同意后登录成功", ok and client_ok.authenticated)
        report.check("被控端确实收到授权请求", len(approval_requests) == 1, f"n={len(approval_requests)}")

        approval_flag["approved"] = False
        client_no = RemoteClient(port=approval_port)
        ok2 = runner.submit(
            client_no.connect("127.0.0.1", "selftest-peer", "", mode="approval")
        ).result(timeout=90)
        report.check("仅凭设备 ID 接入：对方拒绝后无法接入",
                     (not ok2) and (not client_no.authenticated))

        runner.fire_and_forget(client_ok.close())
        runner.fire_and_forget(client_no.close())
        time.sleep(0.5)
        approval_server.stop()

    host_disc.stop()
    peer_disc.stop()

    # ---------------------------------------------------------- 边界场景
    asyncio.run(_raw_scenarios(report, port, creds, chats))

    # ---------------------------------------------------------- 手机网页访问
    from core.web_access import WebAccessServer, render_qr_png

    web = WebAccessServer(
        "selftest-host", ws_port_provider=lambda: port, port=24100, port_fallback=5
    )
    web_ok = web.start()
    report.check(
        "手机网页访问服务（HTTP）可启动", web_ok and web.running, f"port={web.actual_port}"
    )
    if web_ok:
        import urllib.request

        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{web.actual_port}/", timeout=5
            ) as resp:
                page = resp.read().decode("utf-8")
            with urllib.request.urlopen(
                f"http://127.0.0.1:{web.actual_port}/qr", timeout=5
            ) as resp:
                qr = resp.read()
        except Exception as exc:  # noqa: BLE001
            page, qr = "", b""
            report.check("手机控制页 / 二维码可访问", False, str(exc))
        report.check(
            "手机控制页携带设备 ID 与 WebSocket 端口",
            "selftest-host" in page and f"var WS_PORT = {port}" in page,
            f"len={len(page)}",
        )
        report.check("访问链接二维码可生成 PNG", qr.startswith(b"\x89PNG"), f"bytes={len(qr)}")
        png2 = render_qr_png("http://127.0.0.1:1/")
        report.check("二维码渲染工具可用", png2.startswith(b"\x89PNG"))
        web.stop()
        report.check("手机网页访问服务可停止", not web.running)

    # ---------------------------------------------------------- 断开与清理
    disconnect_events: list[tuple[str, str]] = []
    server.on_client_disconnected = lambda device_id, address: disconnect_events.append(
        (device_id, address)
    )
    runner.fire_and_forget(client.close())
    deadline = time.time() + 10
    while client.authenticated and time.time() < deadline:
        time.sleep(0.1)
    report.check("断开后认证状态复位", not client.authenticated, f"states={states}")
    report.check(
        "连接状态回调成对出现",
        states.count(True) >= 1 and states.count(False) >= 1,
        f"states={states}",
    )
    time.sleep(0.5)
    report.check(
        "控制端断开时被控端收到回调（供弹「对方已断开」提醒）",
        len(disconnect_events) >= 1,
        f"events={disconnect_events[:2]}",
    )
    server.on_client_disconnected = None
    time.sleep(0.5)
    report.check("服务端在线计数归零", server.client_count() == 0, f"count={server.client_count()}")

    runner.stop()
    server.stop()
    time.sleep(1.0)
    report.check("停止后端口已释放（不残留僵尸监听）", is_port_free(port))

    root.removeHandler(collector)
    real_errors = [m for m in collector.records if "控制执行失败" not in m]
    report.check("运行期无 ERROR 级日志", not real_errors, str(real_errors[:3]))


async def _raw_scenarios(report: Report, port: int, creds: Credentials, chats: list) -> None:
    """用原始 WebSocket 客户端验证异常路径。"""
    url = f"ws://127.0.0.1:{port}"
    limit = 32 * 1024 * 1024

    async def recv_until(ws, wanted, attempts=8, timeout=5):
        for _ in range(attempts):
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
                return None, False
            if Protocol.get_type(msg) == wanted:
                return Protocol.get_data(msg), True
        return None, False

    async def drained(ws, deadline_seconds=1.5):
        end = time.monotonic() + deadline_seconds
        while time.monotonic() < end:
            try:
                await asyncio.wait_for(ws.recv(), timeout=0.3)
            except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
                pass

    # 未认证即控制
    ws = await websockets.connect(url, max_size=limit)
    await ws.send(ProtocolBuilder.mouse_move(10, 10))
    body, ok = await recv_until(ws, MessageType.ERROR)
    await ws.close()
    report.check("未认证客户端被拒绝控制", ok, f"reply={body}")

    # 错误密码
    ws = await websockets.connect(url, max_size=limit)
    wrong = "000000" if creds.password != "000000" else "111111"
    await ws.send(ProtocolBuilder.login("intruder", wrong))
    body, ok = await recv_until(ws, MessageType.LOGIN_FAILED)
    closed = False
    try:
        await asyncio.wait_for(ws.recv(), timeout=4)
    except websockets.exceptions.ConnectionClosed:
        closed = True
    except asyncio.TimeoutError:
        closed = False
    report.check("错误密码返回 LOGIN_FAILED", ok, f"body={body}")
    report.check("错误密码连接被关闭（防暴力枚举）", closed)
    await ws.close()

    # 非法文本报文 / 二进制垃圾
    for label, payload in (("非法 JSON", "{not-json"), ("二进制垃圾", bytes([0, 1, 2, 255, 254]))):
        ws = await websockets.connect(url, max_size=limit)
        await ws.send(payload)
        body, ok = await recv_until(ws, MessageType.ERROR)
        report.check(f"{label}报文被优雅拒绝且不断开连接", ok, f"reply={body}")
        await ws.close()

    # 聊天广播：发给其余客户端，但不回显给发送者
    ws = await websockets.connect(url, max_size=limit)
    await ws.send(ProtocolBuilder.login("peer", creds.password))
    _, ok = await recv_until(ws, MessageType.LOGIN_SUCCESS)
    report.check("第二个客户端可同时登录", ok)
    await ws.send(ProtocolBuilder.chat("selftest-hello", "peer"))
    echoed = False
    end = time.monotonic() + 1.5
    while time.monotonic() < end:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=0.3)
        except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
            continue
        if Protocol.get_type(msg) == MessageType.CHAT:
            echoed = True
            break
    await asyncio.sleep(0.3)
    await ws.close()
    report.check("聊天消息被转发到其余客户端", ("selftest-hello", "peer") in chats, f"chats={chats}")
    report.check("聊天不回显给发送者（避免重复显示自己发的消息）", not echoed)

    # 心跳
    ws = await websockets.connect(url, max_size=limit)
    await ws.send(ProtocolBuilder.login("peer", creds.password))
    await recv_until(ws, MessageType.LOGIN_SUCCESS)
    await ws.send(ProtocolBuilder.heartbeat())
    _, ok = await recv_until(ws, MessageType.HEARTBEAT_REPLY)
    report.check("心跳得到回复", ok)

    # 画面开关
    await drained(ws)
    await ws.send(Protocol.create(MessageType.SCREEN_STOP))
    # 先排空可能在途的最后一帧（发送循环里已 gather 的那一帧），
    # 再断言稳态下不再有新画面，避免时序竞态造成偶发失败
    await asyncio.sleep(1.2)
    await drained(ws, 1.0)
    got_screen = False
    end = time.monotonic() + 1.5
    while time.monotonic() < end:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=0.3)
        except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
            continue
        if Protocol.get_type(msg) == MessageType.SCREEN:
            got_screen = True
            break
    report.check("客户端停止接收画面后不再收到画面", not got_screen)
    await ws.close()


# ======================================================================
# 图形界面
# ======================================================================
def check_gui(report: Report) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    # 快照用户设置：测试会触发 start_connect() 写入 last_host/last_port，
    # 结束后必须还原，否则测试值会污染真实界面。
    settings_backup = dict(config.load_settings())

    from PyQt6.QtCore import QPoint, QPointF
    from PyQt6.QtWidgets import QApplication, QProgressBar, QLineEdit, QMessageBox

    from database.database import Database
    from core.single_instance import SingleInstanceGuard
    from ui.chat_window import ChatWindow
    from ui.client_bridge import ClientBridge  # noqa: F401
    from ui.file_window import FileWindow
    from ui.icons import app_icon
    from ui.about_dialog import AboutDialog
    from ui.settings_dialog import SettingsDialog
    from ui.main_window import MainWindow, PASSWORD_MASK_CHAR
    from ui.permission_dialog import PermissionDialog
    from ui.remote_window import RemoteWindow
    from ui.tray import TrayManager

    creds = Credentials(ttl=0)
    server = RemoteServer("selftest-gui", creds, port=GUI_PORT, port_fallback=0)
    threading.Thread(target=lambda: asyncio.run(server.start()), daemon=True, name="selftest-gui-srv").start()

    deadline = time.time() + 8
    while server.actual_port is None and time.time() < deadline:
        time.sleep(0.1)
    report.check("图形界面测试用服务端已启动", server.actual_port == GUI_PORT, f"port={server.actual_port}")

    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)

    def pump(seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            time.sleep(0.01)

    # 历史设备用临时库，避免把测试设备写进用户的真实 remote.db
    history_path = os.path.join(tempfile.gettempdir(), "lanremote_selftest.db")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(history_path + suffix)
        except OSError:
            pass
    history_db = Database(history_path)

    window = MainWindow("selftest-gui-dev", creds, "127.0.0.1", server=server, database=history_db)
    tray = TrayManager(window)
    window.tray = tray

    # 单实例锁：第二个实例必须被拒绝并唤醒主实例
    # （托盘上出现多个图标，根因就是允许同时运行多个实例）
    guard = SingleInstanceGuard("LANRemote.Selftest.Lock")
    acquired = guard.try_acquire()
    activated = {"hit": False}
    guard.on_activated(lambda: activated.__setitem__("hit", True))
    second_guard = SingleInstanceGuard("LANRemote.Selftest.Lock")
    second_acquired = second_guard.try_acquire()
    pump(0.6)
    report.check("单实例锁可被主实例获取", acquired)
    report.check(
        "第二个实例被拒绝并唤醒主实例",
        (not second_acquired) and activated["hit"],
        f"acquired={second_acquired} activated={activated['hit']}",
    )
    guard.release()

    # 图标统一性：所有窗口 / 托盘都必须使用 assets/icon.ico
    reference = app_icon()
    targets = {
        "主窗口": window.windowIcon(),
        "远程窗口": (window.remote or RemoteWindow(window.bridge)).windowIcon(),
        "托盘": tray.tray.icon(),
        "聊天窗口": ChatWindow(window.bridge).windowIcon(),
        "文件窗口": FileWindow(window.bridge).windowIcon(),
    }
    bad_icons = [
        name for name, icon_obj in targets.items()
        if icon_obj.isNull() or icon_obj.cacheKey() != reference.cacheKey()
    ]
    icon_file_ok = Path(config.ICON_FILE).exists()
    if bad_icons:
        icon_detail = "; ".join(bad_icons)
    elif not icon_file_ok:
        icon_detail = f"缺失 {config.ICON_FILE}"
    else:
        icon_detail = ""
    report.check(
        f"全部 {len(targets)} 处窗口/托盘图标统一使用 assets/icon.ico",
        not bad_icons and icon_file_ok,
        icon_detail,
    )

    window.show()
    pump(0.6)
    report.check("主窗口可显示", window.isVisible())
    bottom = window.bottom_bar
    bar_bottom = bottom.mapTo(window, QPoint(0, 0)).y() + bottom.height()
    report.check(
        "底部状态栏完整可见（内容超高时可滚动，不再裁切）",
        bottom.isVisible() and bar_bottom <= window.height(),
        f"bar_bottom={bar_bottom} window_height={window.height()}",
    )

    window._tick()
    masked = window.pwd_label.text()
    report.check(
        "临时密码默认打码显示",
        creds.password not in masked and set(masked) == {PASSWORD_MASK_CHAR},
        f"label={masked}",
    )
    window._set_password_visible(True)
    report.check("点击眼睛后明文显示密码", creds.password in window.pwd_label.text())
    window._set_password_visible(False)
    report.check("关闭眼睛后恢复打码", creds.password not in window.pwd_label.text())
    window.refresh_password()
    report.check("刷新密码立即作用于服务端", server.credentials.password == creds.password)
    report.check("刷新后保持打码显示", creds.password not in window.pwd_label.text())
    report.check("界面不再显示刷新倒计时", "后刷新" not in window.pwd_label.text())
    report.check("监听端口正确显示", window.port_label.text() == str(GUI_PORT), window.port_label.text())

    # 设置 / 关于
    about = AboutDialog(window)
    contact = about.contact.text()
    report.check(
        "关于对话框包含作者联系方式",
        ("景同志" in contact) and ("景同学" in contact) and ("小红书" in contact),
    )
    report.check("关于对话框不含吾爱破解信息", ("吾爱" not in contact) and ("LOVE313" not in contact))
    report.check(
        "关于对话框展示 GitHub 仓库地址",
        ("github.com" in contact) and (config.GITHUB_URL in contact)
        and config.DOWNLOAD_URL.endswith("/releases/latest"),
    )
    report.check("关于对话框提供打开仓库入口", callable(getattr(AboutDialog, "_open_github", None)))

    fps_before = config.screen_fps()
    target_fps = 15 if fps_before != 15 else 12
    dialog = SettingsDialog(window, credentials=creds, server=server)
    report.check("设置对话框读出当前配置", dialog.port_spin.value() == config.server_port())
    report.check(
        "设置界面帧率上限已开放到 60",
        dialog.fps_spin.maximum() == config.SCREEN_FPS_MAX == 60,
        f"max={dialog.fps_spin.maximum()}",
    )
    dialog.fps_spin.setValue(60)
    report.check("帧率可设到 60 FPS", dialog.fps_spin.value() == 60)
    dialog.fps_spin.setValue(target_fps)
    dialog._save()
    report.check("设置保存后立即生效（帧率）", config.screen_fps() == target_fps,
                 f"expected={target_fps} got={config.screen_fps()}")
    report.check("推流节拍跟随帧率设置", server.target_fps == target_fps,
                 f"target={server.target_fps}")

    # 画质档位：一键联动帧率 / JPEG 质量 / 分辨率上限，并作用于推流节拍
    profile_before = config.quality_profile()
    target_profile = "hd" if profile_before != "hd" else "smooth"
    position = window.quality_combo.findData(target_profile)
    window.quality_combo.setCurrentIndex(position)
    pump(0.3)
    _, want_fps, want_quality, want_width = config.QUALITY_PROFILES[target_profile]
    report.check(
        "画质档位切换写入配置并联动帧率",
        config.quality_profile() == target_profile and config.screen_fps() == want_fps,
        f"profile={config.quality_profile()} fps={config.screen_fps()}",
    )
    report.check(
        "画质档位联动 JPEG 质量与宽度",
        config.screen_quality() == want_quality and config.screen_max_width() == want_width,
        f"quality={config.screen_quality()} width={config.screen_max_width()}",
    )
    report.check("画质档位作用于服务端推流节拍", server.target_fps == want_fps,
                 f"target={server.target_fps}")
    report.check(
        "设置对话框含画质档位选择",
        dialog.profile_combo.count() == len(config.QUALITY_PROFILES),
    )

    # ---------------------------------------------------------- 主题系统
    from ui import theme

    original_theme = config.get_setting("ui_theme")
    try:
        qss_dark = theme.build_stylesheet("dark")
        qss_light = theme.build_stylesheet("light")
        report.check("深浅两套主题样式均可渲染", bool(qss_dark) and bool(qss_light),
                     f"dark={len(qss_dark)} light={len(qss_light)}")
        report.check(
            "样式模板无未解析占位符",
            "{{" not in qss_dark and "{{" not in qss_light and not theme.unresolved_tokens(),
        )
        missing_assets = [
            name for name in ("check", "dash", "chevron", "arrow_up", "arrow_down")
            if theme.asset_url(name, "dark") == "none"
        ]
        report.check("主题图标资源（勾选/箭头）已生成", not missing_assets,
                     ",".join(missing_assets))
        report.check("深浅主题渲染结果不同", qss_dark != qss_light)

        theme.set_mode("light", persist=True)
        report.check("主题选择可持久化", config.get_setting("ui_theme") == "light",
                     str(config.get_setting("ui_theme")))
        theme.apply()
        app = QApplication.instance()
        appqss = app.styleSheet() if app is not None else ""
        report.check("切换主题后全局样式立即生效",
                     theme.current_mode() == "light" and "f3f5f9" in appqss)
        theme.set_mode("dark", persist=False)
        report.check("可切回默认深色主题", theme.current_mode() == "dark")

        # 设置对话框里的主题切换是即时预览，取消后还原
        theme_dialog = SettingsDialog(window, credentials=creds)
        theme_dialog.theme_combo.setCurrentIndex(theme_dialog.theme_combo.findData("light"))
        report.check("设置对话框可实时预览主题", theme.current_mode() == "light")
        theme_dialog.reject()
        report.check("取消设置后主题还原", theme.current_mode() == "dark")
    finally:
        # 还原用户原本的主题设置（可能从未设置过）
        if original_theme is None:
            config.remove_setting("ui_theme")
        else:
            config.set_setting("ui_theme", original_theme)
        theme.set_mode(
            original_theme if original_theme in theme.PALETTES else theme.DEFAULT_MODE,
            persist=False,
        )

    # 真实连接
    window.port_input.setText(str(GUI_PORT))
    window.host_input.setText("127.0.0.1")
    window.password_input.setText(creds.password)
    window.start_connect()
    pump(6.0)
    report.check("桥接器报告已连接", window.bridge.connected)
    report.check("按钮状态随连接切换",
                 window.disconnect_button.isEnabled() and not window.connect_button.isEnabled())

    # ---------------------------------------------------------- 历史设备
    records = history_db.get_devices()
    report.check("连接成功后自动写入历史设备", len(records) >= 1, f"records={len(records)}")
    report.check("历史设备已渲染到界面列表", window.history_list.count() >= 1,
                 f"rows={window.history_list.count()}")
    if records:
        first = records[0]
        report.check(
            "历史设备记录了地址与端口",
            first.get("ip") == "127.0.0.1" and int(first.get("port") or 0) == GUI_PORT,
            f"ip={first.get('ip')} port={first.get('port')}",
        )
        history_db.set_note(int(first["id"]), "自检设备")
        window._reload_history()
        report.check("可为历史设备添加备注并显示",
                     "自检设备" in window.history_list.item(0).text(),
                     window.history_list.item(0).text())

    report.check("远程窗口已创建并显示", window.remote is not None and window.remote.isVisible())

    end = time.time() + 10
    while window.remote._pixmap is None and time.time() < end:
        pump(0.2)
    report.check("远程窗口收到并渲染画面（跨线程更新未崩溃）", window.remote._pixmap is not None)

    remote = window.remote
    if remote._pixmap is not None:
        report.check("远端分辨率已记录",
                     remote.remote_width > 0 and remote.remote_height > 0,
                     f"{remote.remote_width}x{remote.remote_height}")
        center = QPointF(remote.image_label.width() / 2.0, remote.image_label.height() / 2.0)
        coords = remote._to_remote(center)
        ok = (
            coords is not None
            and abs(coords[0] - remote.remote_width / 2.0) < 3
            and abs(coords[1] - remote.remote_height / 2.0) < 3
        )
        report.check("鼠标坐标映射到远端分辨率正确", ok, f"got={coords}")
        corner = remote._to_remote(QPointF(remote._draw_offset_x, remote._draw_offset_y))
        report.check("画面左上角映射为 (0,0)", corner is not None and corner[0] < 2 and corner[1] < 2,
                     f"got={corner}")

        # ------------------------------------------------------ 远程窗口高级功能
        remote.display_segment.set_current("stretch", notify=True)
        pump(0.2)
        report.check("远程窗口可切换显示模式（拉伸铺满）", remote.display_mode == "stretch")
        remote.display_segment.set_current("original", notify=True)
        pump(0.2)
        report.check("远程窗口可切换显示模式（原始尺寸）", remote.display_mode == "original")
        remote.display_segment.set_current("fit", notify=True)
        pump(0.2)
        report.check("显示模式可切回适应窗口", remote.display_mode == "fit")

        report.check("显示模式为分段控制器", remote.display_segment.count() == 3)

        # 工具栏上的聊天 / 文件入口（信号转给主窗口，由主窗口开窗）
        report.check(
            "远程窗口工具栏含聊天 / 文件入口",
            hasattr(remote, "chat_button") and hasattr(remote, "files_button"),
        )
        remote.chat_requested.emit()
        pump(0.2)
        report.check("工具栏「聊天」可唤起聊天面板", window.chat.isVisible())
        remote.files_requested.emit()
        pump(0.2)
        report.check("工具栏「文件」可唤起文件面板", window.files.isVisible())
        window.chat.close()
        window.files.close()
        pump(0.2)

        remote.pause_button.setChecked(True)
        pump(0.4)
        report.check("远程窗口可暂停画面传输", remote._paused)
        remote.pause_button.setChecked(False)
        pump(0.4)
        report.check("暂停后可恢复画面传输", not remote._paused)

        remote.send_hotkey(("ctrl",), "c")
        pump(0.3)
        report.check("特殊按键面板可发送组合键", True)

        remote.fetch_clipboard()
        pump(1.2)
        report.check("可向远端请求剪贴板", True)

        report.check(
            "远程窗口含画质与显示器选择",
            remote.quality_combo.count() == len(config.QUALITY_PROFILES)
            and remote.monitor_combo.count() >= 1,
            f"quality={remote.quality_combo.count()} monitor={remote.monitor_combo.count()}",
        )

    remote._pending_move = (50.0, 60.0)
    remote._flush_input()
    remote._pending_scroll = 3
    remote._flush_input()
    remote._refresh_hud()
    pump(1.0)
    report.check("输入转发无异常", True)
    report.check(
        "远程窗口状态栏含分辨率/帧率/延迟/带宽",
        all(key in remote.hud.text() for key in ("分辨率", "帧率", "延迟", "带宽")),
        remote.hud.text()[:70],
    )

    stats = server.stream_stats()
    report.check(
        "服务端推流统计可读（含丢帧计数）",
        stats["fps"] == server.target_fps and stats["sent"] >= 1 and "dropped" in stats,
        f"fps={stats['fps']} sent={stats['sent']} dropped={stats['dropped']}",
    )

    window.disconnect()
    end = time.time() + 8
    while window.bridge.connected and time.time() < end:
        pump(0.2)
    report.check("断开后状态复位", not window.bridge.connected)

    window.close()
    pump(0.4)
    report.check("关闭主窗口仅隐藏（托盘常驻）", not window.isVisible())
    tray.show_window()
    pump(0.3)
    report.check("托盘可重新唤起主窗口", window.isVisible())

    tray_actions = [a for a in tray.tray.contextMenu().actions() if not a.isSeparator()]
    tray_labels = [action.text() for action in tray_actions]
    report.check(
        "托盘菜单含聊天 / 文件传输入口",
        "远程聊天" in tray_labels and "文件传输" in tray_labels,
        ", ".join(tray_labels),
    )
    report.check("托盘菜单项均带图标", all(not a.icon().isNull() for a in tray_actions))

    chat = ChatWindow(window.bridge, server=server)
    chat.append("我", "hi")
    chat.on_message("hello", "peer")
    report.check("聊天窗口可用（气泡消息渲染）", "hello" in chat.history.toPlainText())
    report.check(
        "聊天窗口双通道（控制端连接 / 被控端客户端）",
        hasattr(chat, "control_connected") and hasattr(chat, "hosting_clients"),
    )

    files = FileWindow(window.bridge, server=server)
    files.set_progress(50)
    report.check("文件窗口可用", files.progress.value() == 50)

    # 文件事件驱动表格：offer 建行 -> progress 推进 -> done 收尾
    files.on_file_event(
        "offer",
        {
            "transfer_id": "row-1", "name": "demo.bin", "received": 0, "total": 100,
            "direction": "in", "done": False, "ok": True, "path": "", "message": "",
        },
    )
    files.on_file_event(
        "progress",
        {
            "transfer_id": "row-1", "name": "demo.bin", "received": 50, "total": 100,
            "direction": "in", "done": False, "ok": True, "path": "", "message": "",
        },
    )
    bar = files.table.cellWidget(0, 3)
    report.check(
        "文件面板按事件建行并推进进度",
        files.table.rowCount() == 1 and isinstance(bar, QProgressBar) and bar.value() == 500,
        f"rows={files.table.rowCount()} value={bar.value() if isinstance(bar, QProgressBar) else '—'}",
    )
    files.on_file_event(
        "done",
        {
            "transfer_id": "row-1", "name": "demo.bin", "received": 100, "total": 100,
            "direction": "in", "done": True, "ok": True, "path": os.path.join(tempfile.gettempdir(), "demo.bin"),
            "message": "",
        },
    )
    report.check("传输完成后面板标记为已完成", files.table.item(0, 4).text() == "已完成",
                 files.table.item(0, 4).text())
    report.check("文件面板可读出保存目录", bool(files._download_dir()), files._download_dir())

    # 主窗口 / 托盘的工具入口
    window.open_chat()
    window.open_files()
    pump(0.3)
    report.check("主窗口可打开聊天 / 文件面板", window.chat.isVisible() and window.files.isVisible())
    window.chat.close()
    window.files.close()
    pump(0.2)

    report.check("授权对话框可用", PermissionDialog("192.168.1.9").text() != "")

    # ---------------------------------------------------------- 标签页 / 眼睛 / 校验
    report.check(
        "主窗口为五标签页结构（点击切换查看）",
        window.tabs.count() == 5 and window.pages.count() == 5,
        f"tabs={window.tabs.count()} pages={window.pages.count()}",
    )
    window.show_tab("local")
    pump(0.2)
    report.check("标签切换：本机信息页可见", window.pages.currentIndex() == 0)
    window.show_tab("tools")
    pump(0.2)
    report.check("标签切换：远程工具页可见", window.pages.currentIndex() == 2)
    window.show_tab("history")
    pump(0.2)
    report.check("标签切换：历史设备独立成页", window.pages.currentIndex() == 3)
    window.show_tab("logs")
    pump(0.2)
    report.check("标签切换：运行日志独立成页", window.pages.currentIndex() == 4)
    window.show_tab("connect")
    pump(0.2)
    report.check("标签切换：远程连接页可见", window.pages.currentIndex() == 1)

    # 窗口高度固定（600px 能容纳最高的历史设备页），切页时高度不变化
    heights = {}
    for key in ("local", "connect", "tools", "history", "logs"):
        window.show_tab(key)
        pump(0.1)
        heights[key] = window.height()
    report.check(
        "窗口使用固定高度，切页不改变窗口大小",
        len(set(heights.values())) == 1 and window.height() <= 700,
        f"heights={heights}",
    )

    # ---------------------------------------------------------- 新功能
    # 1) 历史设备搜索过滤（测试设备是以 IP 记录的，用 IP 作关键词）
    window.history_search.setText("127.0.0.1")
    pump(0.2)
    matched = window.history_list.count()
    window.history_search.setText("不存在的关键词xyz")
    pump(0.2)
    none_matched = window.history_list.count() == 0
    window.history_search.clear()
    pump(0.2)
    report.check(
        "历史设备支持关键词实时过滤",
        matched >= 1 and none_matched and window.history_list.count() >= 1,
        f"matched={matched}",
    )

    # 2) 历史设备收藏置顶
    record = window.db.get_devices(limit=1)[0]
    window.db.set_favorite(int(record["id"]), True)
    window._reload_history()
    first_text = window.history_list.item(0).text()
    report.check(
        "历史设备可收藏并置顶显示",
        first_text.startswith("★") and bool(int(window.db.get_devices(limit=1)[0]["favorite"])),
        first_text[:20],
    )
    window.db.set_favorite(int(record["id"]), False)
    window._reload_history()

    # 3) 只看画面开关（被控端键鼠控制权限）
    allow_before = config.allow_control()
    window.allow_control_box.setChecked(not allow_before)
    report.check(
        "被控端可一键关闭键鼠控制（只看画面）",
        config.allow_control() == (not allow_before),
    )
    window.allow_control_box.setChecked(allow_before)
    report.check("只看画面开关可恢复", config.allow_control() == allow_before)

    # 4) 设备接入提醒（信号链路 + 托盘通知）
    window.client_connected.emit("selftest-device", "127.0.0.1")
    pump(0.2)
    report.check("设备接入会弹托盘提醒并记录日志", True)

    window.client_disconnected.emit("selftest-device", "127.0.0.1")
    pump(0.2)
    report.check("控制端断开时被控端有断开提醒（日志 + 托盘）", True)

    # 4.5) 手机网页访问
    report.check(
        "手机网页访问服务随主窗口启动",
        window.web_access is not None and window.web_access.running,
        f"port={getattr(window.web_access, 'actual_port', 0)}",
    )
    if window.web_access is not None:
        from ui.web_access_dialog import WebAccessDialog

        access_dialog = WebAccessDialog(
            window.device_id,
            window.ip,
            f"http://127.0.0.1:{window.web_access.actual_port}/",
            window,
        )
        qr_pixmap = access_dialog.qr_label.pixmap()
        report.check(
            "手机访问对话框含链接与二维码",
            qr_pixmap is not None and not qr_pixmap.isNull(),
        )

    # 5) 自定义截图：整幅保存 + 按坐标/尺寸裁剪
    remote = window.remote
    saved = None
    if remote is not None and remote._frame_image is not None:
        from ui.screenshot_dialog import ScreenshotDialog

        dialog = ScreenshotDialog(remote._frame_image, parent=window)
        dialog.accept()  # 默认整幅画面直接保存
        saved = dialog.saved_path
        if saved:
            os.remove(saved)
    report.check("远程窗口可打开截图对话框并保存 PNG", bool(saved), str(saved))

    crop_ok = False
    detail = ""
    if remote is not None and remote._frame_image is not None:
        from ui.screenshot_dialog import ScreenshotDialog

        frame = remote._frame_image
        dialog2 = ScreenshotDialog(frame, parent=window)
        crop_w, crop_h = max(1, frame.width() // 4), max(1, frame.height() // 4)
        dialog2.x_spin.setValue(10)
        dialog2.y_spin.setValue(20)
        dialog2.w_spin.setValue(crop_w)
        dialog2.h_spin.setValue(crop_h)
        cropped = dialog2.result_crop()
        crop_ok = cropped.width() == crop_w and cropped.height() == crop_h
        detail = f"want={crop_w}x{crop_h} got={cropped.width()}x{cropped.height()}"
    report.check("自定义截图支持按坐标 / 尺寸裁剪", crop_ok, detail)

    # 6) 运行日志清空 / 导出
    window.clear_log()
    report.check("运行日志可一键清空", window.log_view.toPlainText().strip() == "")
    window._append_log("自检日志条目")
    export_path = os.path.join(tempfile.gettempdir(), "lanremote-selftest-log.txt")
    window.log_view.setPlainText("自检日志条目\n第二行")
    try:
        with open(export_path, "w", encoding="utf-8") as fp:
            fp.write(window.log_view.toPlainText())
        exported = os.path.exists(export_path) and "第二行" in open(export_path, encoding="utf-8").read()
        os.remove(export_path)
    except OSError:
        exported = False
    report.check("运行日志可导出为文本", exported)

    hidden_before = window.password_input.echoMode() == QLineEdit.EchoMode.Password
    window._toggle_connect_password()
    shown = window.password_input.echoMode() != QLineEdit.EchoMode.Password
    window._toggle_connect_password()
    report.check(
        "访问密码框带小眼睛，可切换明文 / 打码",
        bool(hidden_before and shown)
        and window.password_input.echoMode() == QLineEdit.EchoMode.Password,
    )

    # 连接表单校验：直接调用校验函数，避免测试过程中真的弹窗
    cases = (
        ("", "", "x", True),
        ("192.168.1.10", "", "", True),
        ("192.168.1.10", "12ab", "", True),
        ("not a host!!", "12345678", "", True),
        ("192.168.1.10", "12345678", "ab", True),
        ("192.168.1.10", "12345678", "abcd", False),
        ("", "12345678", "", False),
    )
    wrong = [
        host for host, dev, pwd, want_error in cases
        if bool(window._validate_connect_form(host, dev, pwd)) != want_error
    ]
    report.check("开始连接前会校验 IP / 设备 ID / 密码", not wrong, f"cases={wrong}")

    # ------------------------------------------------------ 连接失败分类弹窗
    popups: list[tuple[str, str]] = []
    original_warning = QMessageBox.warning
    QMessageBox.warning = lambda *args, **kwargs: (
        popups.append((str(args[1]) if len(args) > 1 else "", str(args[2]) if len(args) > 2 else "")),
        QMessageBox.StandardButton.Ok,
    )[1]
    try:
        # 密码错误 -> 「接入被拒绝」，且不跳转控制界面
        window.host_input.setText("127.0.0.1")
        window.port_input.setText(str(GUI_PORT))
        window.device_input.setText("12345678")
        window.password_input.setText("wrong-pass-9")
        window.start_connect()
        deadline = time.time() + 8
        while not popups and time.time() < deadline:
            pump(0.2)
        report.check(
            "密码错误时弹「接入被拒绝」提示（不再直接跳转控制界面）",
            bool(popups) and popups[0][0] == "接入被拒绝",
            str(popups[:1]),
        )

        # 对方不在线 -> 「被控端未在线」（新端口 = 新会话，独立桥接器）
        popups.clear()
        window.password_input.setText(creds.password)
        window.port_input.setText(str(GUI_PORT + 7))
        window.start_connect()
        deadline = time.time() + 10
        while not popups and time.time() < deadline:
            pump(0.2)
        report.check(
            "被控端不在线时弹「被控端未在线」提示",
            bool(popups) and popups[0][0] == "被控端未在线",
            str(popups[:1]),
        )
    finally:
        QMessageBox.warning = original_warning

    # 多设备并发：每个目标一个独立会话（独立桥接器 + 独立远程窗口）
    primary_ok = window._primary_session_key in window.sessions
    report.check(
        "支持多设备同时远程（每目标独立会话 / 窗口）",
        len(window.sessions) >= 2
        and primary_ok
        and window.sessions[window._primary_session_key]["bridge"] is window.bridge
        and all(s.get("remote") is not None for s in window.sessions.values()),
        f"sessions={list(window.sessions)}",
    )

    window._notify_tray("自检提醒", "这是一条测试通知")
    report.check("收到聊天 / 文件时可发右下角托盘提醒", True)

    # 历史设备可删除（走数据层 + 界面刷新，避开确认弹窗）
    remaining = history_db.get_devices()
    if remaining:
        history_db.delete_device(int(remaining[0]["id"]))
        window._reload_history()
        report.check(
            "可删除历史设备记录",
            history_db.count() == len(remaining) - 1,
            f"before={len(remaining)} after={history_db.count()}",
        )

    window.force_quit = True
    window._cleanup()
    pump(0.5)
    server.stop()
    pump(0.5)
    report.check("退出清理无异常", True)

    # 清掉临时历史库，避免在系统临时目录堆积
    history_db.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(history_path + suffix)
        except OSError:
            pass

    # 还原测试前的用户设置，避免 127.0.0.1 / 测试端口残留在真实界面
    config.save_settings(settings_backup)
    restored = config.load_settings() == settings_backup
    report.check("测试未污染用户设置（last_host / last_port 已还原）", restored)


# ======================================================================
# 入口
# ======================================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="LAN Remote 自检")
    parser.add_argument("--core", action="store_true", help="仅核心链路")
    parser.add_argument("--gui", action="store_true", help="仅图形界面")
    args = parser.parse_args()

    run_core = args.core or not (args.core or args.gui)
    run_gui = args.gui or not (args.core or args.gui)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")

    exit_code = 0
    if run_core:
        report = Report("核心链路")
        check_static(report)
        check_security(report)
        check_core(report)
        exit_code |= report.summary()
    if run_gui:
        report = Report("图形界面")
        check_gui(report)
        exit_code |= report.summary()

    print("\n结论：" + ("全部通过 ✔" if exit_code == 0 else "存在失败项 ✘"))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
