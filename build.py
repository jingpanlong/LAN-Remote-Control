"""
LAN Remote 一键打包脚本

用法：
    python build.py

产物：
    dist/LANRemote.exe（单文件，双击即可运行）

打包前会自动：
    - 校验关键依赖是否齐全
    - 清理上一轮的 build/ 与 dist/ 目录
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SPEC_FILE = os.path.join(ROOT, "LANRemote.spec")
BUILD_DIR = os.path.join(ROOT, "build")
DIST_DIR = os.path.join(ROOT, "dist")
OUTPUT_EXE = os.path.join(DIST_DIR, "LANRemote.exe")

REQUIRED_MODULES = [
    ("PyQt6", "PyQt6"),
    ("websockets", "websockets"),
    ("mss", "mss"),
    ("cv2", "opencv-python"),
    ("numpy", "numpy"),
    ("PIL", "Pillow"),
    ("pyautogui", "PyAutoGUI"),
    ("pyperclip", "pyperclip"),
    ("cryptography", "cryptography"),
    ("PyInstaller", "pyinstaller"),
]


def check_dependencies() -> list[str]:
    """返回缺失依赖对应的安装包名。"""
    import importlib.util

    missing: list[str] = []
    for module_name, package_name in REQUIRED_MODULES:
        if importlib.util.find_spec(module_name) is None:
            missing.append(package_name)
    return missing


def clean() -> None:
    for path in (BUILD_DIR, DIST_DIR):
        if os.path.isdir(path):
            print(f"[清理] 删除 {path}")
            shutil.rmtree(path, ignore_errors=True)


def detect_foreign_qt_bindings() -> list[str]:
    """检测除 PyQt6 之外多余安装的 Qt 绑定（不影响打包，仅作提醒）。"""
    import importlib.util

    foreign = []
    for name in ("PyQt5", "PySide2", "PySide6"):
        if importlib.util.find_spec(name) is not None:
            foreign.append(name)
    return foreign


def main() -> int:
    print("=" * 62)
    print("  LAN Remote 打包")
    print("=" * 62)

    missing = check_dependencies()
    if missing:
        print("\n[错误] 缺少以下依赖，请先安装：")
        for package in missing:
            print(f"    pip install {package}")
        return 1

    foreign = detect_foreign_qt_bindings()
    if foreign:
        print(
            "\n[提示] 检测到环境中还装有其它 Qt 绑定："
            + "、".join(foreign)
            + "\n       打包配置已在 excludes 中将其全部排除，仅保留 PyQt6。\n"
        )

    if not os.path.exists(SPEC_FILE):
        print(f"[错误] 找不到打包配置：{SPEC_FILE}")
        return 1

    clean()

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        SPEC_FILE,
        "--noconfirm",
        "--clean",
    ]
    print("[执行] " + " ".join(command) + "\n")

    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        print(f"\n[失败] 打包未成功，退出码 {result.returncode}")
        return result.returncode

    if not os.path.exists(OUTPUT_EXE):
        print(f"\n[失败] 未生成产物：{OUTPUT_EXE}")
        return 1

    size_mb = os.path.getsize(OUTPUT_EXE) / 1024 / 1024
    print("\n" + "=" * 62)
    print(f"  打包成功：{OUTPUT_EXE}")
    print(f"  文件大小：{size_mb:.1f} MB")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
