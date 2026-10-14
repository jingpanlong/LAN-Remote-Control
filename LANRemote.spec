# -*- mode: python ; coding: utf-8 -*-
"""
LAN Remote 打包配置（PyInstaller 6.x，单文件模式）

用法（在项目根目录执行）：
    python -m PyInstaller LANRemote.spec --noconfirm --clean

------------------------------------------------------------------
为什么需要显式 excludes？
------------------------------------------------------------------
本机环境同时安装了 PyQt5 / PyQt6 / PySide6 四种 Qt 绑定。

第三方包 qtpy（由 PyQt6 / cv2 的 hook 链带入分析图）在其源码
（qtpy/__init__.py 以及 qtpy/Qt*.py）中按顺序 try-import 了全部四种绑定，
PyInstaller 的静态分析会扫描到 `from PyQt5.QtCore import ...`，
进而去执行 hook-PyQt5，而此时 hook-PyQt6 已经运行过，
于是 PyInstaller 的 ensure_single_qt_bindings_package() 直接抛错中止构建：

    ERROR: Aborting build process due to attempt to collect multiple Qt
    bindings packages: attempting to run hook for 'PyQt5', while hook for
    'PyQt6' has already been run!

解法就是这里把 PyQt5 / PySide2 / PySide6 以及 qtpy / pyqtgraph 全部排除，
让分析图里只剩 PyQt6 一种绑定。
"""

import os

# PyInstaller 在 spec 命名空间中注入 SPECPATH（spec 文件所在目录）
try:
    PROJECT_ROOT = os.path.abspath(SPECPATH)  # noqa: F821
except NameError:  # pragma: no cover - 直接以模块方式运行 spec 时的兜底
    PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

APP_NAME = "LANRemote"
ICON_PATH = os.path.join(PROJECT_ROOT, "assets", "icon.ico")


# ==================================================================
# 排除模块
# ==================================================================
EXCLUDES = [
    # ---------------- Qt 绑定冲突（关键，缺一不可） ----------------
    "PyQt5",
    "PyQt5.sip",
    "PyQt5.QtCore",
    "PyQt5.QtGui",
    "PyQt5.QtWidgets",
    "PyQtWebEngine",
    "PySide2",
    "PySide6",
    # qtpy 是 PyQt5 被拉入分析图的直接原因；pyqtgraph 会顺带引入多种绑定
    "qtpy",
    "pyqtgraph",
    # ---------------- 未使用的重型依赖，排除以减小体积 ----------------
    "tkinter",
    "_tkinter",
    "Tkinter",
    "matplotlib",
    "pandas",
    "scipy",
    "sympy",
    "IPython",
    "jupyter",
    "notebook",
    "pytest",
    "pygame",
    "cv2.gapi",          # OpenCV 的 G-API，本项目未使用
]


# ==================================================================
# 隐藏导入
# ==================================================================
# websockets 17.x 采用 PEP 562 模块级 __getattr__ 实现懒加载，
# `websockets.serve` / `websockets.connect` 并不在源码里直接 import，
# 静态分析追踪不到真实实现模块，必须显式收集，
# 否则打包后调用 `websockets.serve()` 会抛 AttributeError。
from PyInstaller.utils.hooks import collect_submodules  # noqa: E402

WEBSOCKETS_SUBMODULES = collect_submodules(
    "websockets",
    filter=lambda name: "trio" not in name,   # trio 后端本项目未使用
)

# qrcode：手机网页访问的二维码渲染（qrcode.image.pil 等为动态引用，
# 静态分析追踪不到，必须整包收集）
QRCODE_SUBMODULES = collect_submodules("qrcode")

HIDDEN_IMPORTS = [
    *WEBSOCKETS_SUBMODULES,
    *QRCODE_SUBMODULES,
    # numpy：本机 numpy 代码版本为 1.26.4，但 site-packages 里残留了
    # numpy-1.24.4.dist-info，导致 importlib.metadata 读到 1.24.4。
    # PyInstaller 的 hook-numpy.py 正是用元数据判断版本，于是误判为 < 1.25，
    # 跳过了 `numpy.core._multiarray_tests` 的收集。而 numpy/core/_add_newdocs.py
    # 在导入阶段就会执行：
    #     add_newdoc('numpy.core._multiarray_tests', 'format_float_OSprintf_g', ...)
    # 该调用内部通过 __import__ 加载此模块，缺失时打包产物一启动即崩溃：
    #     ModuleNotFoundError: No module named 'numpy.core._multiarray_tests'
    # 因此这里必须显式补回。
    "numpy.core._multiarray_tests",
    # PyAutoGUI 的这几个依赖是在函数体内惰性 import 的
    "pyscreeze",
    "pymsgbox",
    "pytweening",
    "mouseinfo",
    "pygetwindow",
    # 屏幕采集与编码
    "mss",
    "cv2",
    "PIL.Image",
    "PIL.ImageGrab",
    # 剪贴板（内部在 try 中导入）
    "pyperclip",
]


# ==================================================================
# 资源文件
# ==================================================================
# 打包后 config.py 会通过 sys._MEIPASS 定位这些只读资源，
# 因此这里的落点目录名必须与源码中的目录名保持一致。
DATAS = [
    (ICON_PATH, "assets"),
    (os.path.join(PROJECT_ROOT, "assets", "web_remote.html"), "assets"),
    (os.path.join(PROJECT_ROOT, "ui", "style.qss"), "ui"),
    (os.path.join(PROJECT_ROOT, "web"), "web"),
]


# ==================================================================
# 分析
# ==================================================================
a = Analysis(  # noqa: F821
    [os.path.join(PROJECT_ROOT, "main.py")],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821


# ==================================================================
# 可执行文件（单文件）
# ==================================================================
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # 未安装 UPX，显式关闭避免打包期告警
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,             # 窗口程序，不弹控制台黑窗
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON_PATH,
)
