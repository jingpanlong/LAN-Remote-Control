"""
LAN Remote - 全局配置

所有可调参数集中在此，并支持持久化到 cache/settings.json。
"""

from __future__ import annotations

import json
import os
import sys
import threading

APP_NAME = "LAN Remote"
VERSION = "2.0.0"

# ---------------------------------------------------------------- 开源仓库
# 关于对话框展示 / 安装包元信息引用的唯一来源
GITHUB_USER = "jingpanlong"
GITHUB_REPO = "LAN-Remote-Control"
GITHUB_URL = f"https://github.com/{GITHUB_USER}/{GITHUB_REPO}"
DOWNLOAD_URL = f"{GITHUB_URL}/releases/latest"

# ---------------------------------------------------------------- 路径
# 源码运行：以本文件所在目录为基准。
# 打包运行：资源（assets/ui/web）随 PyInstaller 解包到临时目录，用 sys._MEIPASS 定位；
#           可写数据（logs/cache）必须落在 exe 同级目录，否则退出即丢失。


def _frozen() -> bool:
    """是否为 PyInstaller 冻结后的运行环境。"""
    return bool(getattr(sys, "frozen", False))


def _resource_dir() -> str:
    """只读资源根目录。"""
    if _frozen():
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _writable_dir() -> str:
    """可写数据根目录（日志、缓存、配置）。"""
    if _frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _resource_dir()          # 只读资源基准
DATA_DIR = _writable_dir()          # 可写数据基准

LOG_DIR = os.path.join(DATA_DIR, "logs")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
UI_DIR = os.path.join(BASE_DIR, "ui")
WEB_DIR = os.path.join(BASE_DIR, "web")
ICON_FILE = os.path.join(ASSETS_DIR, "icon.ico")
STYLE_FILE = os.path.join(UI_DIR, "style.qss")
SETTINGS_FILE = os.path.join(CACHE_DIR, "settings.json")
# 文件传输的默认接收目录
DOWNLOAD_DIR = os.path.join(DATA_DIR, "downloads")

for _folder in (LOG_DIR, CACHE_DIR, DOWNLOAD_DIR):
    try:
        os.makedirs(_folder, exist_ok=True)
    except OSError:
        pass

# ---------------------------------------------------------------- 服务端
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 9527
# 首选端口被占用时，向后尝试的端口数量（0 表示不尝试备用端口）
SERVER_PORT_FALLBACK = 10

# ---------------------------------------------------------------- 手机网页访问
# 被控端额外开一个 HTTP 服务，手机浏览器访问该端口即可远程控制本机
WEB_PORT = 9528
WEB_PORT_FALLBACK = 10

# ---------------------------------------------------------------- 画面
SCREEN_FPS = 20
SCREEN_FPS_MIN = 1
SCREEN_FPS_MAX = 60            # 帧率上限，设置界面与之保持一致
SCREEN_QUALITY = 60
SCREEN_QUALITY_MIN = 20
SCREEN_QUALITY_MAX = 95
# 帧宽度上限，超过则等比缩小后再编码，显著降低带宽占用（0 表示不缩放）
SCREEN_MAX_WIDTH = 1920
# 默认采集的显示器：mss 中索引 0 表示所有屏幕合成，1 起为各物理显示器
SCREEN_MONITOR = 1

# 画质档位：一键联动「帧率 / JPEG 质量 / 最大宽度」
#   key -> (显示名, 帧率, JPEG 质量, 最大宽度)
QUALITY_PROFILES = {
    "smooth": ("流畅", 30, 45, 1280),
    "balanced": ("均衡", 30, 60, 1920),
    "hd": ("高清", 60, 75, 1920),
    "ultra": ("极致", 60, 90, 2560),
}
QUALITY_PROFILE_ORDER = ("smooth", "balanced", "hd", "ultra")
DEFAULT_QUALITY_PROFILE = "balanced"

# ---------------------------------------------------------------- 安全
PASSWORD_EXPIRE = 3600         # 临时密码有效期（秒），0 表示永不过期
PASSWORD_LENGTH = 8            # 临时密码位数（字母 + 数字混合）
ALLOW_CONTROL = True           # 默认允许控制端操作本机鼠标键盘

# ---------------------------------------------------------------- 网络
WS_MAX_SIZE = 32 * 1024 * 1024     # 单条 WebSocket 消息上限（画面帧较大）
WS_PING_INTERVAL = 20
WS_PING_TIMEOUT = 20
WS_OPEN_TIMEOUT = 10
HEARTBEAT_INTERVAL = 5             # 客户端心跳间隔（秒）
CLIENT_RECONNECT_DELAY = 3         # 断线重连初始退避（秒）
CLIENT_RECONNECT_MAX_DELAY = 30    # 断线重连最大退避（秒）

# ---------------------------------------------------------------- 设备发现 / 授权
DISCOVERY_PORT = 4527              # UDP 广播端口（与业务端口分离）
DISCOVERY_INTERVAL = 2.0           # 宣告间隔（秒）
DISCOVERY_TTL = 6.0                # 设备超过该秒数未宣告即视为离线
DISCOVERY_RESOLVE_WAIT = 3.0       # 按 ID 查找设备时的最长等待（秒）
APPROVAL_TIMEOUT = 60              # 授权模式下等待对方点击的最长时间（秒）

# ---------------------------------------------------------------- 文件传输
FILE_CHUNK_SIZE = 256 * 1024        # 单块大小（base64 后约 350KB/条）
FILE_MAX_SIZE = 2 * 1024 * 1024 * 1024   # 单文件上限 2GB
FILE_PROGRESS_STEP = 0.05           # 进度回报粒度（每 5% 回一次）

# ---------------------------------------------------------------- 其它
DEBUG = False

_lock = threading.RLock()
_cache: dict | None = None


def load_settings() -> dict:
    """读取本地设置（带缓存，线程安全）。"""
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        data: dict = {}
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as fp:
                    loaded = json.load(fp)
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, ValueError):
                data = {}
        _cache = data
        return _cache


def save_settings(data: dict) -> None:
    """原子写入本地设置，避免中断导致文件损坏。"""
    global _cache
    with _lock:
        _cache = dict(data)
        tmp = SETTINGS_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(_cache, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, SETTINGS_FILE)
        except OSError:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


def get_setting(key: str, default=None):
    return load_settings().get(key, default)


def set_setting(key: str, value) -> None:
    data = load_settings()
    data[key] = value
    save_settings(data)


def remove_setting(key: str) -> None:
    data = load_settings()
    if key in data:
        data.pop(key, None)
        save_settings(data)


def get_value(key: str, default):
    """读取用户设置；未设置或类型非法时回退到内置默认值。"""
    value = get_setting(key)
    if value is None:
        return default
    try:
        return type(default)(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 动态配置
# 以下参数可在「设置」里调整并持久化，读取时优先用用户设置。
def server_port() -> int:
    return int(get_value("server_port", SERVER_PORT))


def server_port_fallback() -> int:
    return int(get_value("server_port_fallback", SERVER_PORT_FALLBACK))


def web_port() -> int:
    return int(get_value("web_port", WEB_PORT))


def web_port_fallback() -> int:
    return int(get_value("web_port_fallback", WEB_PORT_FALLBACK))


def screen_fps() -> int:
    return int(get_value("screen_fps", SCREEN_FPS))


def screen_quality() -> int:
    return int(get_value("screen_quality", SCREEN_QUALITY))


def screen_max_width() -> int:
    return int(get_value("screen_max_width", SCREEN_MAX_WIDTH))


def password_expire() -> int:
    return int(get_value("password_expire", PASSWORD_EXPIRE))


def password_length() -> int:
    return int(get_value("password_length", PASSWORD_LENGTH))


def allow_control() -> bool:
    """被控端是否允许控制端操作本机鼠标键盘（关闭后只共享画面）。"""
    return bool(int(get_value("allow_control", 1 if ALLOW_CONTROL else 0)))


def screenshot_dir() -> str:
    """远程截图保存目录（downloads/screenshots），创建失败时退回 downloads。"""
    path = os.path.join(DOWNLOAD_DIR, "screenshots")
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:
        return DOWNLOAD_DIR


def screen_monitor() -> int:
    """默认采集的显示器索引（1 起为物理显示器）。"""
    return max(0, int(get_value("screen_monitor", SCREEN_MONITOR)))


def quality_profile() -> str:
    """当前画质档位标识；非法值回退默认档。"""
    value = get_setting("quality_profile")
    return str(value) if value in QUALITY_PROFILES else DEFAULT_QUALITY_PROFILE


def download_dir() -> str:
    """
    文件接收目录。

    默认为程序目录下的 downloads；允许通过设置覆盖（自检用它隔离临时目录）。
    """
    value = get_setting("download_dir")
    if isinstance(value, str) and value.strip():
        return value
    return DOWNLOAD_DIR


def file_chunk_size() -> int:
    return max(16 * 1024, int(get_value("file_chunk_size", FILE_CHUNK_SIZE)))


def file_max_size() -> int:
    return max(1024 * 1024, int(get_value("file_max_size", FILE_MAX_SIZE)))


def detect_quality_profile(fps=None, quality=None, max_width=None) -> str:
    """按实际参数反推最接近的档位（界面回显用）。"""
    if fps is None:
        fps = screen_fps()
    if quality is None:
        quality = screen_quality()
    if max_width is None:
        max_width = screen_max_width()

    best_key = DEFAULT_QUALITY_PROFILE
    best_score: int | None = None
    for key in QUALITY_PROFILE_ORDER:
        _, p_fps, p_quality, p_width = QUALITY_PROFILES[key]
        score = (
            abs(int(fps) - p_fps) * 4
            + abs(int(quality) - p_quality)
            + abs(int(max_width) - p_width) // 64
        )
        if best_score is None or score < best_score:
            best_score = score
            best_key = key
    return best_key


def apply_quality_profile(key: str) -> bool:
    """应用画质档位：一次写入帧率、质量、最大宽度三项。"""
    profile = QUALITY_PROFILES.get(key)
    if profile is None:
        return False
    _, fps, quality, max_width = profile
    data = load_settings()
    data["quality_profile"] = key
    data["screen_fps"] = int(fps)
    data["screen_quality"] = int(quality)
    data["screen_max_width"] = int(max_width)
    save_settings(data)
    return True
