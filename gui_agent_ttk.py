#!/usr/bin/env python3
"""Independent ttk front end for scrcpy preview and the package CLI.

This module is intentionally a sidecar. Running the package CLI does not import
this file, start scrcpy, create a Tk window, or incur any GUI-related overhead.
"""

from __future__ import annotations

import ctypes
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, scrolledtext
import tkinter as tk
from tkinter import ttk

from config import load_env_file
from device.adb import AdbController
from execution.atomic_tools.iqiyi.mode import (
    ACTION_MODES,
    MODE_ENVIRONMENT_VARIABLE,
    normalize_action_mode,
)


PROJECT_ROOT = Path(__file__).resolve().parent
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

SCRCPY_MAX_SIZE = 1024
SCRCPY_MAX_FPS = 20
SCRCPY_VIDEO_BIT_RATE = "2M"
SCRCPY_START_RETRY_DELAY = 0.6
SCRCPY_START_RETRY_WINDOW = 3.0


def load_project_environment() -> None:
    """Load the same local environment files as the existing CLI."""
    load_env_file(PROJECT_ROOT / ".env")


def resolve_adb_path() -> Path:
    value = os.environ.get("ADB_PATH")
    if not value or not value.strip():
        raise ValueError("ADB_PATH is required in .env.")
    return Path(value.strip()).expanduser()


def resolve_device_id() -> str:
    """Return the preferred already-connected ADB device, if configured."""
    return os.environ.get("DEVICE_ID", "").strip()


def resolve_scrcpy_path(adb_path: Path) -> Path | None:
    """Resolve scrcpy without installing or starting anything."""
    configured = os.environ.get("SCRCPY_PATH")
    candidates: list[Path] = []
    if configured:
        candidates.append(
            Path(os.path.expandvars(configured)).expanduser()
        )

    discovered = shutil.which("scrcpy") or shutil.which("scrcpy.exe")
    if discovered:
        candidates.append(Path(discovered))

    # WinGet updates PATH for newly opened shells only. Resolve its stable link
    # and package directory as fallbacks so this GUI can see a fresh install
    # immediately, including when it was started from an older terminal.
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        winget_root = Path(local_app_data) / "Microsoft" / "WinGet"
        candidates.append(winget_root / "Links" / "scrcpy.exe")
        packages_root = winget_root / "Packages"
        if packages_root.is_dir():
            candidates.extend(
                packages_root.glob(
                    "Genymobile.scrcpy_*/*/scrcpy.exe"
                )
            )

    candidates.extend(
        [
            PROJECT_ROOT / "scrcpy" / "scrcpy.exe",
            PROJECT_ROOT / "tools" / "scrcpy" / "scrcpy.exe",
            adb_path.parent / "scrcpy.exe",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def list_adb_devices(adb_path: Path) -> tuple[list[str], list[str]]:
    """Return authorized serials and human-readable non-ready device states."""
    if not adb_path.is_file():
        raise FileNotFoundError(f"adb.exe 不存在：{adb_path}")
    result = subprocess.run(
        [str(adb_path), "devices"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "未知错误"
        raise RuntimeError(f"adb devices 执行失败：{detail}")

    ready: list[str] = []
    unavailable: list[str] = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        if state == "device":
            ready.append(serial)
        else:
            unavailable.append(f"{serial} ({state})")
    return ready, unavailable


def make_case_id() -> str:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    return f"GUI-{timestamp}"


def build_agent_command(
    case_id: str,
    instruction: str,
    serial: str,
    iqiyi_mode: str,
) -> list[str]:
    return [
        sys.executable,
        "-u",
        "-m",
        "orchestrator",
        case_id,
        instruction,
        "--serial",
        serial,
        "--iqiyi-mode",
        normalize_action_mode(iqiyi_mode),
    ]


def build_scrcpy_command(
    scrcpy_path: Path,
    serial: str,
    window_title: str,
) -> list[str]:
    return [
        str(scrcpy_path),
        "--serial",
        serial,
        "--window-title",
        window_title,
        "--window-borderless",
        "--no-window-aspect-ratio-lock",
        "--render-fit=letterbox",
        "--no-audio",
        # Keep scrcpy's native SDL mouse control enabled. Clicks and drags in
        # the embedded child window are forwarded to Android as touch events.
        "--mouse=sdk",
        f"--max-size={SCRCPY_MAX_SIZE}",
        f"--max-fps={SCRCPY_MAX_FPS}",
        f"--video-bit-rate={SCRCPY_VIDEO_BIT_RATE}",
        "--no-terminal-title",
    ]


def enable_windows_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


class WindowsChildWindow:
    """Small ctypes adapter for hosting the scrcpy SDL window inside Tk."""

    GWL_STYLE = -16
    WS_CHILD = 0x40000000
    WS_VISIBLE = 0x10000000
    WS_POPUP = 0x80000000
    WS_CAPTION = 0x00C00000
    WS_THICKFRAME = 0x00040000
    WS_MINIMIZEBOX = 0x00020000
    WS_MAXIMIZEBOX = 0x00010000
    WS_SYSMENU = 0x00080000
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOZORDER = 0x0004
    SWP_FRAMECHANGED = 0x0020
    SW_SHOW = 5

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("scrcpy 窗口嵌入目前只支持 Windows。")
        self.user32 = ctypes.windll.user32
        self.user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        self.user32.FindWindowW.restype = wintypes.HWND
        self.user32.IsWindow.argtypes = [wintypes.HWND]
        self.user32.IsWindow.restype = wintypes.BOOL
        self.user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
        self.user32.SetParent.restype = wintypes.HWND
        self.user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.GetWindowLongW.restype = ctypes.c_long
        self.user32.SetWindowLongW.argtypes = [
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_long,
        ]
        self.user32.SetWindowLongW.restype = ctypes.c_long
        self.user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        self.user32.SetWindowPos.restype = wintypes.BOOL
        self.user32.MoveWindow.argtypes = [
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.BOOL,
        ]
        self.user32.MoveWindow.restype = wintypes.BOOL
        self.user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.ShowWindow.restype = wintypes.BOOL

    def find(self, title: str) -> int | None:
        handle = self.user32.FindWindowW(None, title)
        return int(handle) if handle else None

    def is_window(self, handle: int | None) -> bool:
        return bool(handle and self.user32.IsWindow(wintypes.HWND(handle)))

    def embed(self, child: int, parent: int) -> None:
        child_hwnd = wintypes.HWND(child)
        parent_hwnd = wintypes.HWND(parent)
        style = int(self.user32.GetWindowLongW(child_hwnd, self.GWL_STYLE))
        style &= ~(
            self.WS_POPUP
            | self.WS_CAPTION
            | self.WS_THICKFRAME
            | self.WS_MINIMIZEBOX
            | self.WS_MAXIMIZEBOX
            | self.WS_SYSMENU
        )
        style |= self.WS_CHILD | self.WS_VISIBLE
        self.user32.SetParent(child_hwnd, parent_hwnd)
        self.user32.SetWindowLongW(child_hwnd, self.GWL_STYLE, style)
        self.user32.SetWindowPos(
            child_hwnd,
            None,
            0,
            0,
            0,
            0,
            self.SWP_NOMOVE
            | self.SWP_NOSIZE
            | self.SWP_NOZORDER
            | self.SWP_FRAMECHANGED,
        )
        self.user32.ShowWindow(child_hwnd, self.SW_SHOW)

    def resize(self, handle: int, width: int, height: int) -> None:
        self.user32.MoveWindow(
            wintypes.HWND(handle),
            0,
            0,
            max(1, width),
            max(1, height),
            True,
        )


class ScrcpyPreview:
    """Own one scrcpy process and its embedded Windows child window."""

    def __init__(
        self,
        host: ttk.Frame,
        adb_path: Path,
        scrcpy_path: Path,
        emit,
    ) -> None:
        self.host = host
        self.adb_path = adb_path
        self.scrcpy_path = scrcpy_path
        self.emit = emit
        self.process: subprocess.Popen[str] | None = None
        self.window_handle: int | None = None
        self.window_title = ""
        self.generation = 0
        self.serial = ""
        self.started_at = 0.0
        self.early_restart_count = 0
        self.win32 = WindowsChildWindow()
        self.host.bind("<Configure>", self._on_host_resize, add="+")

    def start(self, serial: str, *, automatic_retry: bool = False) -> None:
        previous_restart_count = self.early_restart_count
        self.stop()
        self.generation += 1
        generation = self.generation
        self.serial = serial
        self.started_at = time.monotonic()
        self.early_restart_count = (
            previous_restart_count if automatic_retry else 0
        )
        self.window_title = (
            f"GUIAgent-scrcpy-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        )
        command = build_scrcpy_command(
            self.scrcpy_path,
            serial,
            self.window_title,
        )
        environment = {
            **os.environ,
            "ADB": str(self.adb_path),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
        try:
            self.process = subprocess.Popen(
                command,
                cwd=self.scrcpy_path.parent,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as error:
            self.emit("scrcpy_error", message=f"scrcpy 启动失败：{error}")
            return

        self.emit(
            "scrcpy_starting",
            message=(
                f"正在启动 scrcpy：{serial}，{SCRCPY_MAX_SIZE}px / "
                f"{SCRCPY_MAX_FPS} FPS / {SCRCPY_VIDEO_BIT_RATE}"
            ),
        )
        threading.Thread(
            target=self._read_output,
            args=(self.process, generation),
            daemon=True,
        ).start()
        self.host.after(100, self._try_embed, generation, time.monotonic() + 10)

    def _read_output(
        self,
        process: subprocess.Popen[str],
        generation: int,
    ) -> None:
        if process.stdout is not None:
            for line in process.stdout:
                cleaned = ANSI_ESCAPE.sub("", line).strip()
                if cleaned:
                    self.emit("scrcpy_log", message=cleaned)
        return_code = process.wait()
        if generation == self.generation:
            early_exit = (
                time.monotonic() - self.started_at <= SCRCPY_START_RETRY_WINDOW
                and self.early_restart_count == 0
            )
            if early_exit:
                self.early_restart_count += 1
            self.emit(
                "scrcpy_exit",
                message=f"scrcpy 已退出，返回码 {return_code}。",
                return_code=return_code,
                auto_retry=early_exit,
                serial=self.serial,
                generation=generation,
            )

    def retry_early_exit(self, generation: int, serial: str) -> None:
        """Retry one early scrcpy exit caused by startup timing."""
        if generation != self.generation:
            return
        if self.process is not None and self.process.poll() is None:
            return
        self.emit(
            "scrcpy_retrying",
            message="scrcpy 首次连接过早退出，正在自动重试一次。",
        )
        self.start(serial, automatic_retry=True)

    def _try_embed(self, generation: int, deadline: float) -> None:
        if generation != self.generation or self.process is None:
            return
        if self.process.poll() is not None:
            return
        handle = self.win32.find(self.window_title)
        if handle:
            try:
                self.host.update_idletasks()
                self.win32.embed(handle, self.host.winfo_id())
                self.window_handle = handle
                self.resize()
            except (OSError, RuntimeError) as error:
                self.emit(
                    "scrcpy_embed_error",
                    message=(
                        f"scrcpy 已启动，但无法嵌入左侧区域：{error}。"
                        "将保留为独立窗口。"
                    ),
                )
            else:
                self.emit("scrcpy_embedded", message="scrcpy 实时画面已连接。")
            return
        if time.monotonic() < deadline:
            self.host.after(100, self._try_embed, generation, deadline)
        else:
            self.emit(
                "scrcpy_embed_error",
                message="未找到 scrcpy 窗口，预览可能已在独立窗口中打开。",
            )

    def _on_host_resize(self, _event=None) -> None:
        self.resize()

    def resize(self) -> None:
        if self.win32.is_window(self.window_handle):
            self.win32.resize(
                self.window_handle,
                self.host.winfo_width(),
                self.host.winfo_height(),
            )

    def stop(self, *, wait: bool = False) -> None:
        self.generation += 1
        self.window_handle = None
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
        except OSError:
            return
        if wait:
            try:
                process.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                process.kill()


class GuiAgentTtkApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Single-Step GUI Agent")
        self.root.geometry("1440x860")
        self.root.minsize(1000, 650)

        self.adb_path = resolve_adb_path()
        self.scrcpy_path = resolve_scrcpy_path(self.adb_path)
        self.events: queue.Queue[dict] = queue.Queue()
        self.preview: ScrcpyPreview | None = None
        self.agent_process: subprocess.Popen[str] | None = None
        self.agent_process_lock = threading.Lock()
        self.running = False

        self.device_var = tk.StringVar(value=resolve_device_id())
        self.instruction_var = tk.StringVar()
        self.iqiyi_mode_var = tk.StringVar(
            value=normalize_action_mode(
                os.environ.get(MODE_ENVIRONMENT_VARIABLE, "medium")
            )
        )
        self.status_var = tk.StringVar(value="正在初始化……")
        self._build_ui()
        self.root.after(100, self._set_initial_pane_ratio)

        if self.scrcpy_path is not None and sys.platform == "win32":
            self.preview = ScrcpyPreview(
                self.preview_host,
                self.adb_path,
                self.scrcpy_path,
                self.emit,
            )

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(50, self._drain_events)
        self.refresh_devices()

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        self.main_paned = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        self.main_paned.grid(row=0, column=0, sticky="nsew")

        left = ttk.Frame(self.main_paned, padding=8)
        right = ttk.Frame(self.main_paned, padding=(0, 8, 8, 8))
        self.main_paned.add(left, weight=2)
        self.main_paned.add(right, weight=1)

        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(left)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        toolbar.columnconfigure(1, weight=1)
        ttk.Label(toolbar, text="ADB 设备：").grid(row=0, column=0, padx=(0, 6))
        self.device_combo = ttk.Combobox(
            toolbar,
            textvariable=self.device_var,
            state="readonly",
        )
        self.device_combo.grid(row=0, column=1, sticky="ew", padx=(0, 6))
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_selected)
        self.refresh_button = ttk.Button(
            toolbar,
            text="刷新设备",
            command=self.refresh_devices,
        )
        self.refresh_button.grid(row=0, column=2, padx=(0, 6))
        self.reconnect_button = ttk.Button(
            toolbar,
            text="重连预览",
            command=self.connect_preview,
        )
        self.reconnect_button.grid(row=0, column=3)

        self.preview_host = ttk.Frame(left, relief="sunken", borderwidth=1)
        self.preview_host.grid(row=1, column=0, sticky="nsew")
        self.preview_host.columnconfigure(0, weight=1)
        self.preview_host.rowconfigure(0, weight=1)
        self.preview_placeholder = tk.Label(
            self.preview_host,
            text="正在等待设备和 scrcpy……",
            background="#151515",
            foreground="#d0d0d0",
            font=("Microsoft YaHei UI", 12),
        )
        self.preview_placeholder.grid(row=0, column=0, sticky="nsew")

        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        log_frame = ttk.LabelFrame(right, text="运行日志", padding=6)
        log_frame.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            wrap=tk.WORD,
            state=tk.DISABLED,
            font=("Consolas", 10),
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")

        command_frame = ttk.LabelFrame(right, text="单步指令", padding=8)
        command_frame.grid(row=1, column=0, sticky="ew")
        command_frame.columnconfigure(0, weight=1)
        self.instruction_entry = ttk.Entry(
            command_frame,
            textvariable=self.instruction_var,
            font=("Microsoft YaHei UI", 11),
        )
        self.instruction_entry.grid(
            row=0,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=(0, 8),
        )
        self.instruction_entry.bind("<Return>", self._on_enter)
        self.run_button = ttk.Button(
            command_frame,
            text="执行",
            command=self.execute_instruction,
        )
        self.run_button.grid(row=1, column=1, padx=(6, 0))
        ttk.Button(
            command_frame,
            text="清空日志",
            command=self.clear_log,
        ).grid(row=1, column=2, padx=(6, 0))
        self.progress = ttk.Progressbar(command_frame, mode="indeterminate")
        self.progress.grid(row=1, column=0, sticky="ew")
        mode_frame = ttk.Frame(command_frame)
        mode_frame.grid(
            row=2,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=(8, 0),
        )
        ttk.Label(mode_frame, text="爱奇艺组合动作模式：").pack(side=tk.LEFT)
        self.iqiyi_mode_combo = ttk.Combobox(
            mode_frame,
            textvariable=self.iqiyi_mode_var,
            values=ACTION_MODES,
            state="readonly",
            width=10,
        )
        self.iqiyi_mode_combo.pack(side=tk.LEFT)

        status = ttk.Label(
            self.root,
            textvariable=self.status_var,
            anchor="w",
            relief="sunken",
            padding=(6, 3),
        )
        status.grid(row=1, column=0, sticky="ew")

    def _set_initial_pane_ratio(self) -> None:
        """Place the main divider at two thirds of the available width."""
        self.root.update_idletasks()
        width = self.main_paned.winfo_width()
        if width <= 1:
            self.root.after(100, self._set_initial_pane_ratio)
            return
        self.main_paned.sashpos(0, round(width * 2 / 3))

    def emit(self, event_type: str, **payload) -> None:
        self.events.put({"type": event_type, **payload})

    def log(self, message: str, *, source: str = "GUI") -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        text = f"[{timestamp}] [{source}] {message}\n"
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def clear_log(self) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def refresh_devices(self) -> None:
        if self.running:
            return
        self.refresh_button.configure(state=tk.DISABLED)
        self.status_var.set("正在查询 ADB 设备……")
        self.log(f"使用 ADB：{self.adb_path}")

        def worker() -> None:
            try:
                devices, unavailable = list_adb_devices(self.adb_path)
            except (OSError, RuntimeError) as error:
                self.emit("device_error", message=str(error))
            else:
                self.emit(
                    "devices",
                    devices=devices,
                    unavailable=unavailable,
                )

        threading.Thread(target=worker, daemon=True).start()

    def _on_device_selected(self, _event=None) -> None:
        if not self.running:
            self.connect_preview()

    def connect_preview(self) -> None:
        serial = self.device_var.get().strip()
        if not serial:
            return
        if self.scrcpy_path is None:
            message = (
                "未找到 scrcpy.exe。请安装官方 Windows 版，或设置 "
                "SCRCPY_PATH。\n\n"
                "winget install --exact Genymobile.scrcpy"
            )
            self.preview_placeholder.configure(text=message)
            self.log(message.replace("\n", " "), source="scrcpy")
            return
        if self.preview is None:
            self.preview_placeholder.configure(
                text="当前平台不支持将 scrcpy 嵌入 ttk。"
            )
            return
        self.preview_placeholder.configure(text="正在连接 scrcpy……")
        self.preview_placeholder.grid()
        self.preview.start(serial)

    def execute_instruction(self) -> None:
        if self.running:
            return
        instruction = self.instruction_var.get().strip()
        serial = self.device_var.get().strip()
        iqiyi_mode = self.iqiyi_mode_var.get()
        if not serial:
            messagebox.showwarning("没有设备", "请先选择一个已授权的 ADB 设备。")
            return
        if not instruction:
            messagebox.showwarning("没有指令", "请输入要执行的单步指令。")
            return

        case_id = make_case_id()
        self.running = True
        self._set_running_controls(True)
        self.status_var.set(f"正在执行 {case_id}……")
        self.log(f"任务编号：{case_id}")
        self.log(f"用户指令：{instruction}")

        threading.Thread(
            target=self._run_agent,
            args=(case_id, instruction, serial, iqiyi_mode),
            daemon=True,
        ).start()

    def _run_agent(
        self,
        case_id: str,
        instruction: str,
        serial: str,
        iqiyi_mode: str,
    ) -> None:
        command = build_agent_command(case_id, instruction, serial, iqiyi_mode)
        environment = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
        }
        try:
            process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            with self.agent_process_lock:
                self.agent_process = process
            if process.stdout is not None:
                for line in process.stdout:
                    cleaned = ANSI_ESCAPE.sub("", line).rstrip()
                    if cleaned:
                        self.emit("agent_log", message=cleaned)
            return_code = process.wait()
        except OSError as error:
            self.emit(
                "agent_finished",
                case_id=case_id,
                return_code=1,
                message=f"无法启动 orchestrator：{error}",
            )
        else:
            self.emit(
                "agent_finished",
                case_id=case_id,
                return_code=return_code,
            )
        finally:
            with self.agent_process_lock:
                self.agent_process = None

    def _set_running_controls(self, running: bool) -> None:
        state = tk.DISABLED if running else tk.NORMAL
        combo_state = tk.DISABLED if running else "readonly"
        self.run_button.configure(state=state)
        self.refresh_button.configure(state=state)
        self.reconnect_button.configure(state=state)
        self.instruction_entry.configure(state=state)
        self.device_combo.configure(state=combo_state)
        self.iqiyi_mode_combo.configure(state=combo_state)
        if running:
            self.progress.start(12)
        else:
            self.progress.stop()

    def _on_enter(self, _event=None):
        self.execute_instruction()
        return "break"

    def _drain_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        self.root.after(50, self._drain_events)

    def _handle_event(self, event: dict) -> None:
        event_type = event.get("type")
        message = str(event.get("message", ""))
        if event_type == "devices":
            devices = event.get("devices", [])
            unavailable = event.get("unavailable", [])
            previous = self.device_var.get()
            self.device_combo.configure(values=devices)
            if previous in devices:
                self.device_var.set(previous)
            elif devices:
                self.device_var.set(devices[0])
            else:
                self.device_var.set("")
            self.refresh_button.configure(state=tk.NORMAL)
            if unavailable:
                self.log("不可用设备：" + ", ".join(unavailable), source="ADB")
            if devices:
                self.status_var.set(f"已发现 {len(devices)} 台设备。")
                self.log("已授权设备：" + ", ".join(devices), source="ADB")
                # Let Tk finish the first layout before creating the SDL window.
                # This avoids starting scrcpy while the embedding host has a
                # zero-sized/unrealized native window.
                self.root.after(350, self.connect_preview)
                self.instruction_entry.focus_set()
            else:
                self.status_var.set("没有已授权的 ADB 设备。")
                self.preview_placeholder.configure(
                    text="没有已授权的 ADB 设备。\n请连接设备后点击“刷新设备”。"
                )
        elif event_type == "device_error":
            self.refresh_button.configure(state=tk.NORMAL)
            self.status_var.set("ADB 设备查询失败。")
            self.log(message, source="错误")
            self.preview_placeholder.configure(text=message)
        elif event_type == "scrcpy_embedded":
            self.preview_placeholder.grid_remove()
            self.status_var.set("scrcpy 实时画面已连接。")
            self.log(message, source="scrcpy")
        elif event_type in {"scrcpy_starting", "scrcpy_log"}:
            self.log(message, source="scrcpy")
        elif event_type in {"scrcpy_error", "scrcpy_embed_error"}:
            self.preview_placeholder.configure(text=message)
            self.preview_placeholder.grid()
            self.log(message, source="scrcpy")
        elif event_type == "scrcpy_retrying":
            self.preview_placeholder.configure(text=message)
            self.preview_placeholder.grid()
            self.log(message, source="scrcpy")
        elif event_type == "scrcpy_exit":
            if event.get("auto_retry") and self.preview is not None:
                serial = str(event.get("serial", "")).strip()
                generation = int(event.get("generation", 0))
                self.log(
                    "scrcpy 首次启动退出，已安排自动重试。",
                    source="scrcpy",
                )
                self.root.after(
                    round(SCRCPY_START_RETRY_DELAY * 1000),
                    self.preview.retry_early_exit,
                    generation,
                    serial,
                )
                return
            self.preview_placeholder.configure(
                text="scrcpy 已退出。点击“重连预览”重新连接。"
            )
            self.preview_placeholder.grid()
            self.log(message, source="scrcpy")
        elif event_type == "agent_log":
            self.log(message, source="Agent")
        elif event_type == "agent_finished":
            self.running = False
            self._set_running_controls(False)
            return_code = int(event.get("return_code", 1))
            case_id = str(event.get("case_id", ""))
            if message:
                self.log(message, source="错误")
            if return_code == 0:
                self.status_var.set(f"{case_id} 执行完成。")
                self.log(f"{case_id} 执行完成。", source="GUI")
                self.instruction_var.set("")
            else:
                self.status_var.set(f"{case_id} 执行失败。")
                self.log(
                    f"{case_id} 执行失败，返回码 {return_code}。",
                    source="错误",
                )
            self.instruction_entry.focus_set()

    def on_close(self) -> None:
        if self.running:
            should_close = messagebox.askyesno(
                "任务正在执行",
                "当前单步任务尚未结束。确定要终止任务并关闭窗口吗？",
            )
            if not should_close:
                return
            with self.agent_process_lock:
                process = self.agent_process
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
        if self.preview is not None:
            self.preview.stop(wait=True)
        self.root.destroy()


def main() -> int:
    load_project_environment()
    enable_windows_dpi_awareness()
    root = tk.Tk()
    GuiAgentTtkApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
