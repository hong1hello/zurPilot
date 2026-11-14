"""AzurPilot Windows 安装包启动器（PyInstaller 单文件 exe 的入口）。

只依赖标准库，**不 import 项目里的任何模块**：cv2 / numpy / onnxruntime 等重依赖
会在运行时由 ``runtime/`` 里的 Python 环境安装，不能（也不该）塞进单文件 exe。

约定目录结构（与 windows-exe.yml 打出的 zip 一致）::

    AzurPilot-Windows/
      AzurPilot.exe        <- 本文件编译产物
      runtime/python.exe   <- 便携 Python（含 uv.exe）
      app/                 <- zurPilot 源码
      README.txt

Usage (构建时):
    pyinstaller --onefile --noconsole -n AzurPilot dev_tools/windows_launcher.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

WEBUI_URL = "http://127.0.0.1:22365"


def root() -> Path:
    """返回安装包根目录（exe 所在目录）。"""
    return Path(sys.executable).resolve().parent


def describe() -> tuple[Path, Path, Path]:
    """返回 (便携 Python, 源码目录, gui.py)。"""
    base = root()
    python = base / "runtime" / "python.exe"
    app = base / "app"
    return python, app, app / "gui.py"


class Window:
    """极简状态窗口：不依赖任何第三方库。"""

    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import scrolledtext

        self.tk = tk
        self.root = tk.Tk()
        self.root.title("AzurPilot")
        self.root.geometry("560x320")
        self.root.resizable(False, False)
        self.text = scrolledtext.ScrolledText(self.root, state="disabled",
                                              wrap="word", font=("Consolas", 9))
        self.text.pack(fill="both", expand=True, padx=10, pady=10)
        self.running = True

    def log(self, message: str) -> None:
        """追加一行日志（线程安全：由主线程 after 调度）。"""
        def append() -> None:
            self.text.configure(state="normal")
            self.text.insert("end", message + "\n")
            self.text.see("end")
            self.text.configure(state="disabled")

        try:
            self.root.after(0, append)
        except RuntimeError:
            pass

    def mainloop(self) -> None:
        """进入 UI 循环。"""
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.mainloop()

    def close(self) -> None:
        """关闭窗口。"""
        self.running = False
        self.root.destroy()


def launch(window: Window) -> None:
    """在后台线程启动 WebUI。"""
    python, app, gui = describe()
    window.log(f"安装包目录: {root()}")
    if not python.is_file():
        window.log(f"[错误] 缺少便携 Python: {python}")
        return
    if not gui.is_file():
        window.log(f"[错误] 缺少启动脚本: {gui}")
        return

    environment = os.environ.copy()
    runtime = str(python.parent)
    environment["PATH"] = runtime + os.pathsep + environment.get("PATH", "")
    environment.setdefault("AZURPILOT_ANDROID", "0")

    window.log("正在启动 AzurPilot WebUI（首次启动会安装依赖，需要几分钟）…")
    try:
        process = subprocess.Popen(
            [str(python), str(gui)], cwd=str(app), env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as error:
        window.log(f"[错误] 启动失败: {error}")
        return

    for line in iter(process.stdout.readline, ""):
        if not window.running:
            break
        window.log(line.rstrip())
    code = process.wait()
    window.log(f"WebUI 进程已退出（code={code}）。窗口可以关闭。")


def main() -> int:
    """入口。"""
    window = Window()
    thread = threading.Thread(target=launch, args=(window,), daemon=True)
    thread.start()
    # 稍后打开浏览器；失败也不影响主流程
    threading.Timer(8.0, lambda: _open_browser(window)).start()
    window.mainloop()
    return 0


def _open_browser(window: Window) -> None:
    try:
        webbrowser.open(WEBUI_URL)
        window.log(f"已尝试打开 {WEBUI_URL}")
    except Exception as error:                      # noqa: BLE001
        window.log(f"浏览器打开失败（可手动访问 {WEBUI_URL}）: {type(error).__name__}")


if __name__ == "__main__":
    sys.exit(main())
