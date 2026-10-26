import json
import os
import shutil
import queue
import subprocess
import sys
import threading
import time
import ctypes
import re
import urllib.request
import urllib.error
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "rainbowcutter.config.json"
ICON_PATH = APP_DIR / "rainbowcutter.ico"
APP_USER_MODEL_ID = "LynnHMa.RainbowCutter"
DEFAULT_CONFIG = {
    "whisper_dir": str(APP_DIR / "whiper_cpp"),
    "model_dir": str(APP_DIR / "whiper_cpp"),
    "output_dir": str(APP_DIR / "output"),
    "language": "auto",
    "threads": 8,
    "api_base": "https://api.openai.com/v1",
    "api_key": "",
    "model": "gpt-4o-mini",
    "temperature": 0.3,
    "system_prompt": "你是一名专业的播客内容总结顾问。请阅读带时间戳的 SRT 字幕，准确总结主题、结构、关键观点和高光片段。必须保留时间段，不能臆造字幕中没有的信息。",
    "edit_system_prompt": "你是一名专业的播客剪辑助理。你会收到完整的带时间戳 SRT 字幕，必须根据字幕内容和本提示词生成可执行的剪辑指南。所有时间段必须直接取自 SRT 原有时间戳，不能估算或编造。必须严格输出 ## 1. 内容概要、## 2. 高光片段、## 3. 剪辑清单、## 4. 剪辑顺序建议；剪辑清单必须使用表格列：时间段｜操作(删/留/压缩/⚠️高风险)｜原因。只输出 Markdown，不输出 JSON 或代码围栏。"
}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma", ".mp4", ".mov", ".mkv"}
SRT_EXT = {".srt"}
OUTPUT_MODE_SRT_ONLY = "仅进行 SRT 识别"
OUTPUT_MODE_SUMMARY = "只进行 MD 总结"
OUTPUT_MODE_EDIT = "总结并根据 MD 剪辑"
TIME_RANGE_RE = re.compile(r"(?P<start>\d{2}:\d{2}:\d{2}(?:[,.]\d{3})?)\s*[–—-]\s*(?P<end>\d{2}:\d{2}:\d{2}(?:[,.]\d{3})?)")


def parse_timestamp(value):
    value = value.replace(",", ".")
    hours, minutes, seconds = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def parse_edit_guide(markdown):
    """Read only the fixed 剪辑清单 table emitted by the Agent."""
    section = re.search(r"(?ms)^##\s*3[.、．]\s*剪辑清单\s*$.*?(?=^##\s|\Z)", markdown)
    if not section:
        raise ValueError("Markdown 缺少“## 3. 剪辑清单”章节，拒绝自动剪辑")
    rows = []
    for line in section.group(0).splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3 or cells[0] in {"时间段", "---", "---:"} or set(cells[0]) <= {"-", ":"}:
            continue
        match = TIME_RANGE_RE.search(cells[0])
        if not match:
            continue
        operation = cells[1]
        rows.append({
            "start": parse_timestamp(match.group("start")),
            "end": parse_timestamp(match.group("end")),
            "operation": operation,
            "reason": cells[2],
        })
    if not rows:
        raise ValueError("Markdown 的剪辑清单没有可执行的时间段，拒绝自动剪辑")
    rows.sort(key=lambda item: (item["start"], item["end"]))
    for row in rows:
        if row["end"] <= row["start"]:
            raise ValueError(f"剪辑清单时间段无效：{row['start']}-{row['end']}")
    return rows


def audio_codec_for_suffix(suffix):
    return {
        ".mp3": ("libmp3lame", ["-b:a", "192k"]),
        ".wav": ("pcm_s16le", []),
        ".flac": ("flac", []),
        ".ogg": ("libvorbis", ["-q:a", "5"]),
        ".aac": ("aac", ["-b:a", "192k"]),
        ".m4a": ("aac", ["-b:a", "192k"]),
        ".mp4": ("aac", ["-b:a", "192k"]),
        ".mov": ("aac", ["-b:a", "192k"]),
        ".mkv": ("aac", ["-b:a", "192k"]),
        ".wma": ("wmav2", ["-b:a", "192k"]),
    }.get(suffix.lower(), ("aac", ["-b:a", "192k"]))


def inspect_audio_tool(source, log):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("Agent 音频工具需要 ffprobe，请先安装 ffmpeg 并加入 PATH")
    command = [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(source)]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError("音频探测失败：" + result.stderr[-500:])
    duration = float(result.stdout.strip())
    log(f"AudioTool.inspect_audio：时长 {fmt_time(duration)}")
    return duration


def render_audio_tool(source, guide, destination, log):
    """Render a new audio file from the guide; never writes to source."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("Agent 音频工具需要 ffmpeg，请先安装 ffmpeg 并加入 PATH")
    duration = inspect_audio_tool(source, log)
    delete_ranges = []
    for row in guide:
        if row["end"] > duration + 0.2:
            raise ValueError(f"剪辑时间段超出音频时长：{row['start']:.3f}-{row['end']:.3f}")
        operation = row["operation"]
        if "删" in operation or "高风险" in operation:
            delete_ranges.append((row["start"], row["end"]))
    delete_ranges.sort()
    merged = []
    for start, end in delete_ranges:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    keep_ranges = []
    cursor = 0.0
    for start, end in merged:
        if start > cursor + 0.01:
            keep_ranges.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration - 0.01:
        keep_ranges.append((cursor, duration))
    if not keep_ranges:
        raise ValueError("剪辑清单会删除整段音频，已停止以避免生成空文件")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_destination = destination.with_name(destination.stem + ".rendering" + destination.suffix)
    if len(keep_ranges) == 1 and keep_ranges[0][0] <= 0.01 and keep_ranges[0][1] >= duration - 0.01:
        shutil.copy2(source, temp_destination)
        log("AudioTool.copy_source：剪辑清单未删除片段，复制为新副本")
    else:
        labels = "".join(f"[a{i}]" for i in range(len(keep_ranges)))
        filters = [f"[0:a]asplit={len(keep_ranges)}{labels}"]
        trimmed = []
        for i, (start, end) in enumerate(keep_ranges):
            trimmed.append(f"[a{i}]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[s{i}]")
        concat_inputs = "".join(f"[s{i}]" for i in range(len(keep_ranges)))
        filters.append(";".join(trimmed) + f";{concat_inputs}concat=n={len(keep_ranges)}:v=0:a=1[outa]")
        codec, codec_args = audio_codec_for_suffix(destination.suffix)
        command = [ffmpeg, "-y", "-i", str(source), "-filter_complex", ";".join(filters), "-map", "[outa]", "-vn", "-c:a", codec, *codec_args, str(temp_destination)]
        log(f"AudioTool.cut_audio_segments：保留 {len(keep_ranges)} 段")
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise RuntimeError("音频剪辑失败：" + result.stderr[-800:])
    os.replace(temp_destination, destination)
    return destination


def load_config():
    data = dict(DEFAULT_CONFIG)
    try:
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        data.update(saved)
        if "edit_system_prompt" not in saved:
            data["edit_system_prompt"] = saved.get("system_prompt", DEFAULT_CONFIG["edit_system_prompt"])
    except Exception:
        pass
    return data


def save_config(data):
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def fmt_time(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def call_chat_api(cfg, srt_text, log, system_prompt=None, purpose="字幕总结"):
    base = cfg["api_base"].rstrip("/")
    if not base.endswith("/v1") and "/v1/" not in base:
        base += "/v1"
    url = base + "/chat/completions"
    user_prompt = f'''你正在执行“{purpose}”。请完整阅读下面的 SRT 字幕，将字幕文件作为唯一的时间定位依据。
请处理下面的 SRT 字幕。输出一份适合播客剪辑 Agent 执行的中文 Markdown 剪辑指南，必须包含：
1. 一句话结论；2. 结构化摘要；3. 关键观点与事实；4. 高光内容列表（每条必须给出具体起止时间段和原字幕依据）；5. 播客剪辑建议（哪些时间段应删除，尤其是隐私、敏感信息、口误、重复、偏离主题内容；哪些时间段重点保留；推荐开场、标题和切片方向）；6. 风险与待人工确认项。
如果字幕信息不足，明确标注“不确定”，不要补写。请严格使用以下章节标题：## 1. 内容概要、## 2. 高光片段、## 3. 剪辑清单、## 4. 剪辑顺序建议；其中“## 3. 剪辑清单”必须包含表格列：时间段｜操作(删/留/压缩/⚠️高风险)｜原因。请只输出 Markdown 正文，不要输出 JSON 或代码围栏。

SRT 字幕：
{srt_text}'''
    payload = {"model": cfg["model"], "temperature": float(cfg["temperature"]), "messages": [
        {"role": "system", "content": system_prompt or cfg["system_prompt"]},
        {"role": "user", "content": user_prompt}
    ]}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + cfg["api_key"]}
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    log(f"Agent: POST {url} | model={cfg['model']} | purpose={purpose} | 已读取 SRT 字幕字符数={len(srt_text)}")
    with urllib.request.urlopen(req, timeout=300) as response:
        raw = response.read().decode("utf-8")
    parsed = json.loads(raw)
    return parsed["choices"][0]["message"]["content"]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("RainbowCutter")
        self.geometry("1180x900")
        self.minsize(980, 700)
        self._drag_origin = None
        self._drag_latest = None
        self._drag_pending = False
        self._maximized = False
        self._restore_geometry = None
        self._drop_callback = None
        self._drop_subclass_id = 0
        self.cfg = load_config()
        Path(self.cfg["output_dir"]).mkdir(parents=True, exist_ok=True)
        self.files = []
        self.log_queue = queue.Queue()
        self.native_drop_queue = queue.Queue()
        self.running = False
        self._build_style()
        self._build_ui()
        self._install_drag_bindings()
        self.after(50, self._install_native_file_drop)
        self._set_window_icon()
        self.after(0, self._remove_native_titlebar)
        self.after(100, self._drain_logs)
        self._refresh_models()

    def _minimize_window(self):
        self.iconify()

    def _toggle_maximize(self):
        if self._maximized:
            self.state("normal")
            if self._restore_geometry:
                self.geometry(self._restore_geometry)
            self._maximized = False
        else:
            self._restore_geometry = self.geometry()
            self.state("zoomed")
            self._maximized = True

    def _build_style(self):
        self.palette = {"bg": "#f7f8fa", "surface": "#ffffff", "soft": "#f1f3f5", "line": "#e6e8eb", "blue": "#4d6bfe", "blue_soft": "#eef1ff", "text": "#1f2329", "muted": "#86909c", "success": "#18a058", "danger": "#d03050"}
        self.configure(bg=self.palette["bg"])
        style = ttk.Style(self)
        try: style.theme_use("clam")
        except tk.TclError: pass
        p = self.palette
        style.configure("TFrame", background=p["bg"])
        style.configure("Sidebar.TFrame", background=p["surface"])
        style.configure("Topbar.TFrame", background=p["surface"])
        style.configure("Card.TFrame", background=p["surface"])
        style.configure("TLabel", background=p["bg"], foreground=p["text"], font=("Segoe UI", 10))
        style.configure("Topbar.TLabel", background=p["surface"], foreground=p["text"], font=("Segoe UI", 11, "bold"))
        style.configure("Brand.TLabel", background=p["surface"], foreground=p["text"], font=("Segoe UI", 14, "bold"))
        style.configure("Title.TLabel", background=p["bg"], foreground=p["text"], font=("Segoe UI", 23, "bold"))
        style.configure("Section.TLabel", background=p["surface"], foreground=p["text"], font=("Segoe UI", 11, "bold"))
        style.configure("Sub.TLabel", background=p["bg"], foreground=p["muted"], font=("Segoe UI", 9))
        style.configure("CardSub.TLabel", background=p["surface"], foreground=p["muted"], font=("Segoe UI", 9))
        style.configure("Status.TLabel", background=p["surface"], foreground=p["success"], font=("Segoe UI", 9, "bold"))
        style.configure("Nav.TButton", background=p["surface"], foreground=p["muted"], borderwidth=0, padding=(14, 10), anchor="w", font=("Segoe UI", 10))
        style.map("Nav.TButton", background=[("active", p["soft"])], foreground=[("active", p["text"])])
        style.configure("Primary.TButton", background=p["blue"], foreground="#ffffff", borderwidth=0, padding=(18, 10), font=("Segoe UI", 10, "bold"))
        style.map("Primary.TButton", background=[("active", "#405be0"), ("disabled", "#b8c1f5")])
        style.configure("TButton", background=p["surface"], foreground=p["text"], bordercolor=p["line"], padding=(12, 8), font=("Segoe UI", 9))
        style.map("TButton", background=[("active", p["soft"])])
        style.configure("TEntry", fieldbackground=p["surface"], foreground=p["text"], insertcolor=p["blue"], bordercolor=p["line"], padding=7)
        style.configure("TCombobox", fieldbackground="#ffffff", background="#ffffff", foreground=p["text"], arrowcolor=p["blue"], bordercolor=p["line"], padding=6)
        style.configure("OutputMode.TCombobox", fieldbackground="#ffffff", background="#ffffff", foreground=p["text"], arrowcolor=p["blue"], bordercolor=p["line"], padding=(8, 7, 8, 7), font=("Microsoft YaHei UI", 9))
        style.map("TCombobox", fieldbackground=[("readonly", "#ffffff"), ("focus", "#ffffff")], background=[("readonly", "#ffffff"), ("focus", "#ffffff")], foreground=[("readonly", p["text"])], selectbackground=[("readonly", "#ffffff")], selectforeground=[("readonly", p["text"])])
        style.configure("TRadiobutton", background=p["surface"], foreground=p["text"], font=("Segoe UI", 9))
        style.map("TRadiobutton", foreground=[("active", p["blue"])])
        style.configure("TScrollbar", background=p["soft"], troughcolor=p["bg"], arrowcolor=p["muted"], bordercolor=p["bg"])

    def _build_ui(self):
        titlebar = tk.Frame(self, bg=self.palette["surface"], height=40)
        titlebar.pack(fill="x", side="top")
        titlebar.pack_propagate(False)
        tk.Label(titlebar, text="RainbowCutter", bg=self.palette["surface"], fg=self.palette["text"], font=("Segoe UI", 10, "bold")).pack(side="left", padx=16)
        controls = tk.Frame(titlebar, bg=self.palette["surface"]); controls.pack(side="right", fill="y")
        for text, command, color in (("—", self._minimize_window, self.palette["muted"]), ("□", self._toggle_maximize, self.palette["muted"]), ("×", self.destroy, self.palette["danger"])):
            tk.Button(controls, text=text, command=command, width=5, bd=0, relief="flat", bg=self.palette["surface"], fg=color, activebackground=self.palette["soft"], activeforeground=self.palette["text"], font=("Segoe UI Symbol", 11), cursor="hand2").pack(side="left", fill="y")
        shell = ttk.Frame(self); shell.pack(fill="both", expand=True)
        sidebar = ttk.Frame(shell, style="Sidebar.TFrame", width=218, padding=(14, 18)); sidebar.pack(side="left", fill="y"); sidebar.pack_propagate(False)
        ttk.Label(sidebar, text="工作区", style="CardSub.TLabel").pack(anchor="w", padx=12, pady=(8, 10))
        self.nav_buttons = {}
        self.nav_buttons["workspace"] = ttk.Button(sidebar, text="  ⌂   工作台", style="Nav.TButton", command=lambda: self._show_view("workspace")); self.nav_buttons["workspace"].pack(fill="x", pady=2)
        self.nav_buttons["settings"] = ttk.Button(sidebar, text="  ⚙   设置", style="Nav.TButton", command=lambda: self._show_view("settings")); self.nav_buttons["settings"].pack(fill="x", pady=2)
        ttk.Label(sidebar, text="RainbowCutter 1.0", style="CardSub.TLabel").pack(side="bottom", anchor="w", padx=12, pady=8)
        self.content = ttk.Frame(shell, padding=(30, 24, 34, 28)); self.content.pack(side="left", fill="both", expand=True)
        self.views = {}
        self._build_work_tab(); self._build_settings_tab(); self._show_view("workspace")

    def _set_window_icon(self):
        if os.name != "nt" or not ICON_PATH.exists():
            return
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
            self.iconbitmap(default=str(ICON_PATH))
            self.wm_iconbitmap(str(ICON_PATH))
            user32 = ctypes.windll.user32
            user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            user32.GetAncestor.restype = ctypes.c_void_p
            hwnd = user32.GetAncestor(self.winfo_id(), 2)
            user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
            user32.LoadImageW.restype = ctypes.c_void_p
            user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
            user32.SendMessageW.restype = ctypes.c_void_p
            icon = user32.LoadImageW(0, str(ICON_PATH), 1, 32, 32, 0x00000010 | 0x00000040)
            small_icon = user32.LoadImageW(0, str(ICON_PATH), 1, 16, 16, 0x00000010 | 0x00000040)
            if hwnd and icon:
                user32.SendMessageW(hwnd, 0x0080, 1, icon)
                user32.SendMessageW(hwnd, 0x0080, 0, icon)
            if hwnd and small_icon:
                user32.SendMessageW(hwnd, 0x0080, 0, small_icon)
        except (OSError, tk.TclError):
            pass

    def _native_hwnd(self):
        if os.name != "nt":
            return None
        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
        return user32.GetAncestor(self.winfo_id(), 2)

    def _remove_native_titlebar(self):
        if os.name != "nt":
            return
        user32 = ctypes.windll.user32
        hwnd = self._native_hwnd()
        if not hwnd:
            return
        user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
        user32.SetWindowLongW.restype = ctypes.c_long
        user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        user32.SetWindowPos.restype = ctypes.c_bool
        style = user32.GetWindowLongW(hwnd, -16)
        style &= ~0x00C00000
        style |= 0x00040000
        style |= 0x00080000
        user32.SetWindowLongW(hwnd, -16, style)
        extended = user32.GetWindowLongW(hwnd, -20)
        extended &= ~0x00000080
        extended |= 0x00040000
        user32.SetWindowLongW(hwnd, -20, extended)
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0020)
        self.bind("<Alt-F4>", lambda event: self.destroy())
        self.bind("<Escape>", lambda event: self.iconify())

    def _install_drag_bindings(self):
        draggable_classes = {"Frame", "TFrame", "Label", "TLabel", "Canvas"}
        interactive_classes = {"Button", "TButton", "Entry", "TEntry", "Combobox", "TCombobox", "Listbox", "Text", "TRadiobutton", "Checkbutton", "TCheckbutton", "Scrollbar", "TScrollbar"}

        def bind_widget(widget):
            widget_class = widget.winfo_class()
            if widget_class in draggable_classes and widget_class not in interactive_classes:
                widget.bind("<ButtonPress-1>", self._begin_window_drag, add="+")
                widget.bind("<B1-Motion>", self._move_window_drag, add="+")
                widget.bind("<ButtonRelease-1>", self._end_window_drag, add="+")
            for child in widget.winfo_children():
                bind_widget(child)

        bind_widget(self)

    def _begin_window_drag(self, event):
        if os.name != "nt" or getattr(self, "_maximized", False):
            return
        hwnd = self._native_hwnd()
        if not hwnd:
            return
        user32 = ctypes.windll.user32
        class Rect(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        rect = Rect()
        user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(Rect)]
        user32.GetWindowRect.restype = ctypes.c_bool
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return
        self._drag_origin = (event.x_root, event.y_root, rect.left, rect.top)
        self._drag_latest = (event.x_root, event.y_root)
        self._drag_pending = False

    def _move_window_drag(self, event):
        if not self._drag_origin or getattr(self, "_maximized", False):
            return
        self._drag_latest = (event.x_root, event.y_root)
        if not self._drag_pending:
            self._drag_pending = True
            self.after_idle(self._apply_window_drag)

    def _apply_window_drag(self):
        self._drag_pending = False
        if not self._drag_origin or not getattr(self, "_drag_latest", None):
            return
        start_x, start_y, window_x, window_y = self._drag_origin
        latest_x, latest_y = self._drag_latest
        hwnd = self._native_hwnd()
        if not hwnd:
            return
        user32 = ctypes.windll.user32
        user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        user32.SetWindowPos.restype = ctypes.c_bool
        flags = 0x0001 | 0x0004 | 0x0010 | 0x0040
        user32.SetWindowPos(hwnd, 0, window_x + latest_x - start_x, window_y + latest_y - start_y, 0, 0, flags)

    def _end_window_drag(self, _event):
        self._drag_origin = None
        self._drag_latest = None
        self._drag_pending = False

    def _show_view(self, name):
        for view in self.views.values(): view.pack_forget()
        self.views[name].pack(fill="both", expand=True)
        for key, button in self.nav_buttons.items():
            button.configure(style="Primary.TButton" if key == name else "Nav.TButton")

    def _card(self, parent, padding=18):
        return ttk.Frame(parent, style="Card.TFrame", padding=padding)

    def _build_work_tab(self):
        tab = ttk.Frame(self.content); self.views["workspace"] = tab
        heading = ttk.Frame(tab); heading.pack(fill="x")
        self.status = ttk.Label(heading, text="● 就绪", style="Status.TLabel"); self.status.pack(side="right", anchor="n")
        ttk.Label(heading, text="工作台", style="Title.TLabel").pack(anchor="w")
        ttk.Label(heading, text="播客剪辑：从音频或字幕生成剪辑指南，并可安全输出剪辑副本。", style="Sub.TLabel").pack(anchor="w", pady=(5, 0))
        mode = self._card(tab, 12); mode.pack(fill="x", pady=(22, 14))
        self.mode_var = tk.StringVar(value="podcast")
        ttk.Label(mode, text="处理模式", style="Section.TLabel").pack(side="left", padx=(4, 16))
        ttk.Radiobutton(mode, text="播客剪辑", variable=self.mode_var, value="podcast", command=self._on_mode_change).pack(side="left", padx=10)
        ttk.Radiobutton(mode, text="字幕总结", variable=self.mode_var, value="subtitle", command=self._on_mode_change).pack(side="left", padx=10)
        self.mode_desc = ttk.Label(mode, text="音频 → Whisper → 字幕总结 → Agent 剪辑流程", style="CardSub.TLabel"); self.mode_desc.pack(side="right", padx=5)
        body = ttk.Frame(tab, height=320); body.pack(fill="both", expand=False); body.pack_propagate(False)
        left = self._card(body, 18); left.pack(side="left", fill="both", expand=True, padx=(0, 10))
        right = self._card(body, 18); right.pack(side="right", fill="y", padx=(10, 0)); right.configure(width=340); right.pack_propagate(False)
        input_head = ttk.Frame(left, style="Card.TFrame"); input_head.pack(fill="x")
        ttk.Label(input_head, text="输入素材", style="Section.TLabel").pack(side="left")
        self.file_hint = ttk.Label(input_head, text="尚未添加文件", style="CardSub.TLabel"); self.file_hint.pack(side="right")
        drop = tk.Frame(left, bg="#f7f8fa", highlightbackground="#d9dde3", highlightthickness=1, height=112); drop.pack(fill="x", pady=(16, 12)); drop.pack_propagate(False)
        self.add_btn = ttk.Button(drop, text="添加音频", style="Primary.TButton", command=self._choose_files); self.add_btn.pack(pady=(25, 6))
        self.input_hint = ttk.Label(drop, text="可从资源管理器拖入，或选择目录批量导入", style="CardSub.TLabel"); self.input_hint.pack()
        row = ttk.Frame(left, style="Card.TFrame"); row.pack(fill="x")
        ttk.Button(row, text="选择目录", command=self._choose_dir).pack(side="left")
        ttk.Button(row, text="清空列表", command=lambda: self._set_files([])).pack(side="left", padx=8)
        self.file_list = tk.Listbox(left, height=8, selectmode=tk.EXTENDED, activestyle="none", relief="flat", bd=0, bg="#ffffff", fg="#1f2329", selectbackground="#eef1ff", selectforeground="#4d6bfe", font=("Segoe UI", 9))
        self.file_list.pack(fill="both", expand=True, pady=(14, 0)); self.file_list.bind("<Delete>", lambda e: self._remove_selected()); self.file_list.bind("<<ListboxSelect>>", lambda e: self._update_file_count()); self._enable_drop_target(self.file_list)
        ttk.Label(right, text="处理配置", style="Section.TLabel").pack(anchor="w")
        ttk.Label(right, text="字幕模型", style="CardSub.TLabel").pack(anchor="w", pady=(20, 5)); self.model_combo = ttk.Combobox(right, state="readonly"); self.model_combo.pack(fill="x"); ttk.Button(right, text="刷新模型列表", command=self._refresh_models).pack(anchor="w", pady=(7, 0))
        ttk.Label(right, text="识别语言", style="CardSub.TLabel").pack(anchor="w", pady=(18, 5)); self.lang_var = tk.StringVar(value=self.cfg["language"]); ttk.Combobox(right, textvariable=self.lang_var, values=["auto", "zh", "en", "ja", "ko"], state="readonly").pack(fill="x")
        ttk.Label(right, text="输出模式", style="CardSub.TLabel").pack(anchor="w", pady=(18, 5)); self.output_mode_var = tk.StringVar(value=OUTPUT_MODE_EDIT)
        self.output_mode_combo = ttk.Combobox(right, textvariable=self.output_mode_var, values=[OUTPUT_MODE_SRT_ONLY, OUTPUT_MODE_SUMMARY, OUTPUT_MODE_EDIT], state="readonly", width=36, justify="left", style="OutputMode.TCombobox"); self.output_mode_combo.pack(fill="x"); self.output_mode_combo.set(OUTPUT_MODE_EDIT)
        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=20)
        ttk.Label(right, text="Agent 模型", style="CardSub.TLabel").pack(anchor="w"); self.agent_model = ttk.Label(right, text=self.cfg["model"], style="Section.TLabel"); self.agent_model.pack(anchor="w", pady=(5, 0))
        ttk.Label(right, text="OpenAI 兼容接口", style="CardSub.TLabel").pack(anchor="w", pady=(4, 0))
        ttk.Button(right, text="配置 API", command=lambda: self._show_view("settings")).pack(fill="x", pady=(18, 0))
        out = self._card(tab, 12); out.pack(fill="x", pady=(14, 0)); ttk.Label(out, text="输出到", style="CardSub.TLabel").pack(side="left"); self.out_var = tk.StringVar(value=self.cfg["output_dir"]); ttk.Entry(out, textvariable=self.out_var).pack(side="left", fill="x", expand=True, padx=12); ttk.Button(out, text="选择", command=self._choose_output).pack(side="left"); self.start_btn = ttk.Button(out, text="开始处理", style="Primary.TButton", command=self._start); self.start_btn.pack(side="right", padx=(14, 0))
        log_card = self._card(tab, 14); log_card.pack(fill="both", expand=True, pady=(14, 0)); ttk.Label(log_card, text="执行记录", style="Section.TLabel").pack(anchor="w"); self.log_text = tk.Text(log_card, height=16, wrap="word", state="disabled", bg="#f7f8fa", fg="#4e5969", insertbackground=self.palette["blue"], relief="flat", font=("Consolas", 9)); self.log_text.pack(side="left", fill="both", expand=True, pady=(10, 0)); scroll = ttk.Scrollbar(log_card, command=self.log_text.yview); scroll.pack(side="right", fill="y"); self.log_text.configure(yscrollcommand=scroll.set)

    def _build_settings_tab(self):
        tab = ttk.Frame(self.content); self.views["settings"] = tab
        ttk.Label(tab, text="设置", style="Title.TLabel").pack(anchor="w")
        
        canvas = tk.Canvas(tab, highlightthickness=0, bg="#ffffff"); bar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview); inner = tk.Frame(canvas, bg="#ffffff"); inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))); canvas.create_window((0, 0), window=inner, anchor="nw"); canvas.configure(yscrollcommand=bar.set); canvas.pack(side="left", fill="both", expand=True); bar.pack(side="right", fill="y")
        self.setting_vars = {}
        self._settings_section(inner, "路径与识别", [("whisper_dir", "whisper.cpp 目录"), ("model_dir", "字幕模型目录"), ("output_dir", "默认输出目录")])
        self._settings_section(inner, "Agent API", [("api_base", "API Base URL"), ("api_key", "API Key"), ("model", "模型名称"), ("temperature", "Temperature")])
        preset = tk.Frame(inner, bg="#ffffff"); preset.pack(fill="x", pady=8); tk.Label(preset, text="Agent 模型预设", bg="#ffffff", fg="#1f2329", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 8)); self.preset_var = tk.StringVar(); self.preset_combo = ttk.Combobox(preset, textvariable=self.preset_var, state="readonly", width=28); self.preset_combo.pack(side="left"); self.preset_combo.bind("<<ComboboxSelected>>", lambda _event: self._load_preset()); ttk.Button(preset, text="加载预设", command=self._load_preset).pack(side="left", padx=8); ttk.Button(preset, text="当前配置另存为…", command=self._save_preset).pack(side="left"); self._refresh_presets()
        sec = tk.Frame(inner, bg="#ffffff"); sec.pack(fill="x", pady=8); tk.Label(sec, text="识别参数", bg="#ffffff", fg="#1f2329", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 8)); self._add_setting(sec, "language", "默认语言", ["auto", "zh", "en", "ja", "ko"]); self._add_setting(sec, "threads", "CPU 线程数")
        summary_prompt_card = tk.Frame(inner, bg="#ffffff", highlightbackground="#d9dde3", highlightthickness=1, bd=0)
        summary_prompt_card.pack(fill="x", pady=(14, 8), ipadx=14, ipady=14)
        tk.Label(summary_prompt_card, text="字幕总结 System Prompt", bg="#ffffff", fg="#1f2329", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        tk.Label(summary_prompt_card, text="字幕总结模式使用这条系统指令；Agent 会读取完整 SRT 字幕。", bg="#ffffff", fg="#86909c", font=("Segoe UI", 9)).pack(anchor="w", pady=(4, 10))
        summary_prompt_row = tk.Frame(summary_prompt_card, bg="#ffffff")
        summary_prompt_row.pack(fill="x")
        self.summary_prompt = tk.Text(summary_prompt_row, height=10, wrap="word", undo=True, bg="#fbfcfe", fg="#1f2329", insertbackground="#4d6bfe", selectbackground="#dbe4ff", selectforeground="#1f2329", relief="solid", bd=1, highlightthickness=1, highlightbackground="#c9d2e3", highlightcolor="#4d6bfe", padx=10, pady=8, font=("Segoe UI", 10))
        self.summary_prompt.pack(side="left", fill="both", expand=True)
        summary_scroll = ttk.Scrollbar(summary_prompt_row, orient="vertical", command=self.summary_prompt.yview)
        summary_scroll.pack(side="right", fill="y")
        self.summary_prompt.configure(yscrollcommand=summary_scroll.set)
        self.summary_prompt.insert("1.0", self.cfg["system_prompt"])

        edit_prompt_card = tk.Frame(inner, bg="#ffffff", highlightbackground="#d9dde3", highlightthickness=1, bd=0)
        edit_prompt_card.pack(fill="x", pady=(8, 8), ipadx=14, ipady=14)
        tk.Label(edit_prompt_card, text="播客剪辑 System Prompt", bg="#ffffff", fg="#1f2329", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        tk.Label(edit_prompt_card, text="播客剪辑模式使用这条系统指令；Agent 会依据完整 SRT 字幕生成剪辑清单，再交给音频工具执行。", bg="#ffffff", fg="#86909c", font=("Segoe UI", 9)).pack(anchor="w", pady=(4, 10))
        edit_prompt_row = tk.Frame(edit_prompt_card, bg="#ffffff")
        edit_prompt_row.pack(fill="x")
        self.edit_prompt = tk.Text(edit_prompt_row, height=14, wrap="word", undo=True, bg="#fbfcfe", fg="#1f2329", insertbackground="#4d6bfe", selectbackground="#dbe4ff", selectforeground="#1f2329", relief="solid", bd=1, highlightthickness=1, highlightbackground="#c9d2e3", highlightcolor="#4d6bfe", padx=10, pady=8, font=("Segoe UI", 10))
        self.edit_prompt.pack(side="left", fill="both", expand=True)
        edit_scroll = ttk.Scrollbar(edit_prompt_row, orient="vertical", command=self.edit_prompt.yview)
        edit_scroll.pack(side="right", fill="y")
        self.edit_prompt.configure(yscrollcommand=edit_scroll.set)
        self.edit_prompt.insert("1.0", self.cfg["edit_system_prompt"])

        ttk.Button(inner, text="保存全部设置", style="Primary.TButton", command=self._save_settings).pack(anchor="e", pady=12)
    def _refresh_presets(self):
        names = sorted(self.cfg.get("presets", {}).keys()); self.preset_combo["values"] = names
        if names and not self.preset_var.get(): self.preset_combo.set(names[0])

    def _load_preset(self):
        name = self.preset_var.get(); preset = self.cfg.get("presets", {}).get(name)
        if not preset: return
        for key in ("api_base", "api_key", "model", "temperature"):
            if key in preset and key in self.setting_vars: self.setting_vars[key].set(str(preset[key]))
        if "system_prompt" in preset:
            self.summary_prompt.delete("1.0", "end")
            self.summary_prompt.insert("1.0", str(preset["system_prompt"]))
        if "edit_system_prompt" in preset or "system_prompt" in preset:
            self.edit_prompt.delete("1.0", "end")
            self.edit_prompt.insert("1.0", str(preset.get("edit_system_prompt", preset.get("system_prompt", self.cfg["edit_system_prompt"]))))
        for key in ("api_base", "api_key", "model", "temperature", "system_prompt", "edit_system_prompt"):
            if key in preset:
                self.cfg[key] = preset[key]
        self.agent_model.configure(text=self.cfg["model"])
        save_config(self.cfg)
        self._log(f"已加载 Agent 预设：{name}")

    def _save_preset(self):
        from tkinter.simpledialog import askstring
        name = askstring("保存预设", "预设名称：", parent=self)
        if not name: return
        self.cfg.setdefault("presets", {})[name] = {k: self.setting_vars[k].get() for k in ("api_base", "api_key", "model", "temperature")}
        self.cfg["presets"][name]["system_prompt"] = self.summary_prompt.get("1.0", "end-1c")
        self.cfg["presets"][name]["edit_system_prompt"] = self.edit_prompt.get("1.0", "end-1c")
        save_config(self.cfg); self._refresh_presets(); self.preset_combo.set(name); self._log(f"已保存 Agent 预设：{name}")

    def _settings_section(self, parent, title, fields):
        sec = tk.Frame(parent, bg="#ffffff"); sec.pack(fill="x", pady=8); tk.Label(sec, text=title, bg="#ffffff", fg="#1f2329", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 8))
        for key, label in fields: self._add_setting(sec, key, label)

    def _add_setting(self, parent, key, label, values=None):
        row = tk.Frame(parent, bg="#ffffff"); row.pack(fill="x", pady=4); tk.Label(row, text=label, width=18, anchor="w", bg="#ffffff", fg="#1f2329", font=("Segoe UI", 9)).pack(side="left")
        var = tk.StringVar(value=str(self.cfg.get(key, ""))); self.setting_vars[key] = var
        if values: ttk.Combobox(row, textvariable=var, values=values, state="readonly").pack(side="left", fill="x", expand=True)
        else: ttk.Entry(row, textvariable=var, show="*" if key == "api_key" else "").pack(side="left", fill="x", expand=True)
        if key in ("whisper_dir", "model_dir", "output_dir"): ttk.Button(row, text="选择", command=lambda k=key: self._pick_setting(k)).pack(side="left", padx=8)

    def _pick_setting(self, key):
        p = filedialog.askdirectory() if key != "output_dir" else filedialog.askdirectory()
        if p: self.setting_vars[key].set(p)

    def _save_settings(self):
        for k, v in self.setting_vars.items():
            self.cfg[k] = int(v.get()) if k == "threads" else v.get()
        self.cfg["system_prompt"] = self.summary_prompt.get("1.0", "end-1c"); self.cfg["edit_system_prompt"] = self.edit_prompt.get("1.0", "end-1c"); self.cfg["output_dir"] = self.setting_vars["output_dir"].get(); self.out_var.set(self.cfg["output_dir"]); self.agent_model.configure(text=self.cfg["model"])
        save_config(self.cfg); self._refresh_models(); self._log("设置已保存")

    def _on_mode_change(self):
        subtitle_mode = self.mode_var.get() == "subtitle"
        if subtitle_mode:
            self.output_mode_combo.set(OUTPUT_MODE_SUMMARY)
            self.output_mode_combo.configure(state="disabled")
        else:
            self.output_mode_combo.configure(state="readonly")
        self.add_btn.configure(text="添加 SRT" if subtitle_mode else "添加音频")
        self.mode_desc.configure(text="SRT → Agent → Markdown 字幕总结" if subtitle_mode else "音频 → Whisper → Markdown 剪辑指南 → Agent 剪辑流程")
        self.input_hint.configure(text="SRT 将直接交给 Agent，不执行语音识别" if subtitle_mode else "音频将先由 whisper.cpp 生成 SRT，再按输出模式处理")
        self.file_hint.configure(text="已添加 SRT 文件 · Delete 删除选中项" if subtitle_mode else "也可将文件路径粘贴到列表中（Ctrl+V）")
        self._set_files([])

    def _choose_files(self):
        if self.mode_var.get() == "subtitle":
            paths = filedialog.askopenfilenames(filetypes=[("SRT 字幕", "*.srt"), ("所有文件", "*.*")])
        else:
            paths = filedialog.askopenfilenames(filetypes=[("音频/视频", " ".join("*" + x for x in AUDIO_EXT)), ("所有文件", "*.*")])
        self._set_files(list(paths))
    def _choose_dir(self):
        d = filedialog.askdirectory()
        if d:
            allowed = SRT_EXT if self.mode_var.get() == "subtitle" else AUDIO_EXT
            self._set_files([str(p) for p in Path(d).iterdir() if p.suffix.lower() in allowed])
    def _choose_output(self):
        d = filedialog.askdirectory(initialdir=self.out_var.get() or str(APP_DIR));
        if d: self.out_var.set(d); self.cfg["output_dir"] = d; save_config(self.cfg)
    def _set_files(self, paths):
        allowed = SRT_EXT if self.mode_var.get() == "subtitle" else AUDIO_EXT
        self.files = list(dict.fromkeys(str(Path(p)) for p in paths if Path(p).is_file() and Path(p).suffix.lower() in allowed))
        self.file_list.delete(0, tk.END)
        for path in self.files:
            self.file_list.insert(tk.END, path)
        self._update_file_count()
    def _remove_selected(self):
        selected = set(self.file_list.curselection()); self._set_files([p for i, p in enumerate(self.files) if i not in selected])
    def _update_file_count(self): self.file_hint.configure(text=f"已添加 {len(self.files)} 个文件 · 可从资源管理器直接拖入 · Delete 删除选中项")
    def _enable_drop_target(self, widget):
        try:
            from tkinterdnd2 import DND_FILES
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", lambda e: self._set_files(self.files + list(self.tk.splitlist(e.data))))
        except Exception:
            return

    def _install_native_file_drop(self):
        if os.name != "nt" or getattr(self, "_drop_callback", None):
            return
        try:
            hwnd = self._native_hwnd()
            if not hwnd:
                return
            shell32 = ctypes.windll.shell32
            comctl32 = ctypes.windll.comctl32
            shell32.DragAcceptFiles.argtypes = [ctypes.c_void_p, ctypes.c_bool]
            shell32.DragAcceptFiles.restype = None
            shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint]
            shell32.DragQueryFileW.restype = ctypes.c_uint
            shell32.DragFinish.argtypes = [ctypes.c_void_p]
            shell32.DragFinish.restype = None
            callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t, ctypes.c_size_t, ctypes.c_size_t)
            comctl32.SetWindowSubclass.argtypes = [ctypes.c_void_p, callback_type, ctypes.c_size_t, ctypes.c_size_t]
            comctl32.SetWindowSubclass.restype = ctypes.c_bool
            comctl32.DefSubclassProc.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
            comctl32.DefSubclassProc.restype = ctypes.c_ssize_t
            shell32.DragAcceptFiles(hwnd, True)

            def subclass_proc(window, message, wparam, lparam, subclass_id, ref_data):
                if message != 0x0233:
                    return comctl32.DefSubclassProc(window, message, wparam, lparam)
                try:
                    paths = []
                    try:
                        count = shell32.DragQueryFileW(wparam, 0xFFFFFFFF, None, 0)
                        for index in range(count):
                            length = shell32.DragQueryFileW(wparam, index, None, 0)
                            buffer = ctypes.create_unicode_buffer(length + 1)
                            shell32.DragQueryFileW(wparam, index, buffer, length + 1)
                            paths.append(buffer.value)
                    finally:
                        shell32.DragFinish(wparam)
                    if paths:
                        self.native_drop_queue.put(tuple(paths))
                except Exception as error:
                    try:
                        self._log("拖放回调异常：" + str(error))
                    except Exception:
                        pass
                return 0

            self._drop_callback = callback_type(subclass_proc)
            self._drop_subclass_id = id(self) & 0xFFFFFFFF
            if not comctl32.SetWindowSubclass(hwnd, self._drop_callback, self._drop_subclass_id, 0):
                self._drop_callback = None
                self._log("原生拖放注册失败")
        except (AttributeError, OSError) as error:
            self._drop_callback = None
            self._log("原生拖放不可用：" + str(error))

    def _accept_native_drop(self, paths):
        try:
            self._set_files(self.files + list(paths))
            self._log(f"拖入 {len(paths)} 个项目，已加入可处理列表")
        except Exception as error:
            self._log("拖放导入失败：" + str(error))
    def _refresh_models(self):
        d = Path(self.cfg.get("model_dir", "")); models = sorted(str(p) for p in d.glob("*.bin")) if d.exists() else []
        self.model_combo["values"] = models
        if models:
            current = next((m for m in models if Path(m).name == getattr(self, "selected_model", "")), models[0]); self.model_combo.set(current); self.selected_model = Path(current).name
        else: self.model_combo.set("未发现 .bin 模型")
    def _log(self, text): self.log_queue.put(f"[{time.strftime('%H:%M:%S')}] {text}")
    def _drain_logs(self):
        try:
            while True:
                line = self.log_queue.get_nowait(); self.log_text.configure(state="normal"); self.log_text.insert("end", line + "\n"); self.log_text.see("end"); self.log_text.configure(state="disabled")
        except queue.Empty: pass
        try:
            while True:
                self._accept_native_drop(self.native_drop_queue.get_nowait())
        except queue.Empty: pass
        self.after(100, self._drain_logs)

    def _start(self):
        if self.running: return
        if not self.files: messagebox.showwarning("缺少输入", "请先添加音频文件或选择目录。"); return
        if self.mode_var.get() == "podcast" and not self.model_combo.get().endswith(".bin"): messagebox.showwarning("缺少模型", "请在设置中选择包含 .bin 模型的目录。\n字幕总结模式不需要字幕模型。"); return
        srt_only = self.mode_var.get() == "podcast" and self.output_mode_combo.get() == OUTPUT_MODE_SRT_ONLY
        if not srt_only and not self.cfg.get("api_key"): messagebox.showwarning("缺少 API Key", "请先在设置中填写 Agent API Key。"); self._show_view("settings"); return
        self.running = True; self.start_btn.configure(state="disabled"); self.status.configure(text="执行中…", foreground="#b45309"); threading.Thread(target=self._pipeline, daemon=True).start()

    def _pipeline(self):
        try:
            model = self.model_combo.get(); output_dir = Path(self.out_var.get()); output_dir.mkdir(parents=True, exist_ok=True)
            whisper_dir = Path(self.cfg["whisper_dir"]); exe = whisper_dir / "whisper-cli.exe"
            subtitle_mode = self.mode_var.get() == "subtitle"
            srt_only = self.mode_var.get() == "podcast" and self.output_mode_combo.get() == OUTPUT_MODE_SRT_ONLY
            podcast_edit = self.mode_var.get() == "podcast" and self.output_mode_combo.get() == OUTPUT_MODE_EDIT
            for idx, audio in enumerate(self.files, 1):
                src = Path(audio); self._log(f"[{idx}/{len(self.files)}] 开始处理：{src.name}")
                stem = src.stem; srt_path = src if subtitle_mode else output_dir / f"{stem}.srt"; md_path = output_dir / f"{stem}.md"
                if subtitle_mode:
                    self._log("字幕总结：跳过 whisper.cpp，直接提交 Agent")
                elif not srt_path.exists():
                    self._log("字幕识别：调用 whisper.cpp")
                    input_audio = src
                    temp_audio = None
                    if src.suffix.lower() not in {".flac", ".mp3", ".ogg", ".wav"}:
                        ffmpeg = shutil.which("ffmpeg")
                        if not ffmpeg: raise RuntimeError("当前文件格式需要 ffmpeg 转换，但系统 PATH 中未找到 ffmpeg")
                        temp_audio = APP_DIR / "temp" / (stem + ".wav"); temp_audio.parent.mkdir(exist_ok=True)
                        self._log(f"音频转换：{src.suffix.lower()} → 16kHz 单声道 WAV")
                        convert = subprocess.run([ffmpeg, "-y", "-i", str(src), "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(temp_audio)], capture_output=True, text=True, encoding="utf-8", errors="replace")
                        if convert.returncode != 0: raise RuntimeError("ffmpeg 音频转换失败：" + convert.stderr[-500:])
                        input_audio = temp_audio
                    cmd = [str(exe), "-m", model, "-f", str(input_audio), "-t", str(self.cfg.get("threads", 8)), "-osrt", "-of", str(output_dir / stem)]
                    language = self.cfg.get("language", "auto")
                    if language: cmd.extend(["-l", language])
                    proc = subprocess.Popen(cmd, cwd=str(whisper_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
                    for line in proc.stdout: self._log("Whisper: " + line.rstrip())
                    code = proc.wait()
                    if code != 0 or not srt_path.exists(): raise RuntimeError(f"whisper.cpp 失败，退出码 {code}")
                    if temp_audio and temp_audio.exists(): temp_audio.unlink()
                    t2s_script = whisper_dir / "t2s.py"
                    if t2s_script.exists():
                        self._log(f"字幕繁转简：{srt_path.name}")
                        convert_srt = subprocess.run([sys.executable, str(t2s_script), str(srt_path)], cwd=str(whisper_dir), capture_output=True, text=True, encoding="utf-8", errors="replace")
                        if convert_srt.returncode != 0:
                            raise RuntimeError("繁体转简体失败：" + (convert_srt.stderr.strip() or convert_srt.stdout.strip()))
                    self._log(f"字幕完成：{srt_path.name}")
                else: self._log(f"检测到已有字幕，跳过识别：{srt_path.name}")
                if srt_only:
                    self._log(f"SRT 识别完成：仅输出 {srt_path.name}，跳过 Agent 和音频剪辑")
                    continue
                srt = srt_path.read_text(encoding="utf-8-sig", errors="replace")
                if subtitle_mode:
                    self._log("字幕总结 Agent：读取 SRT 字幕并使用字幕总结 System Prompt")
                    report = call_chat_api(self.cfg, srt, self._log, system_prompt=self.cfg["system_prompt"], purpose="字幕总结")
                else:
                    self._log(f"播客剪辑 Agent：读取字幕文件 {srt_path.name} 并使用播客剪辑 System Prompt")
                    report = call_chat_api(self.cfg, srt, self._log, system_prompt=self.cfg["edit_system_prompt"], purpose="播客剪辑")
                report_body = f"# {stem}｜{'字幕总结' if subtitle_mode else '播客剪辑指南'}\n\n> 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n> 字幕文件：`{srt_path.name}`\n> Agent 模型：`{self.cfg['model']}`\n\n---\n\n" + report + "\n"
                md_path.write_text(report_body, encoding="utf-8")
                self._log(f"完成：{md_path}")
                if podcast_edit:
                    self._log("Agent 剪辑流程：解析 Markdown 剪辑清单")
                    guide = parse_edit_guide(report)
                    edited_path = output_dir / f"{stem}_edited{src.suffix.lower()}"
                    render_audio_tool(src, guide, edited_path, self._log)
                    self._log(f"剪辑副本完成：{edited_path}（源文件未修改）")
            self._log("全部任务完成"); self.after(0, lambda: self.status.configure(text="完成", foreground="#16803c"))
        except Exception as e:
            self._log("ERROR: " + str(e)); self.after(0, lambda: self.status.configure(text="失败，请查看日志", foreground="#b91c1c"))
        finally:
            self.running = False; self.after(0, lambda: self.start_btn.configure(state="normal"))

if __name__ == "__main__":
    App().mainloop()







