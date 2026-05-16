#!/usr/bin/env python3
"""递归遍历文件夹，在窗口中按每秒 30 行播放文本文件内容。"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox
from tkinter import scrolledtext

DEFAULT_LINES_PER_TICK = 30
DEFAULT_TICK_SECONDS = 1.0
DEMO_CONTENT_LINES = 30
INDENT_SPACES = "    "


def _demo_line_text(line_no: int) -> str:
    unit = f"第{line_no}行"
    return unit * 14

BINARY_EXTENSIONS = {
    ".bin",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".obj",
    ".o",
    ".a",
    ".lib",
    ".pyc",
    ".pyo",
    ".pyd",
    ".whl",
    ".egg",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".ico",
    ".webp",
    ".tiff",
    ".tif",
    ".mp3",
    ".mp4",
    ".avi",
    ".mkv",
    ".mov",
    ".wav",
    ".flac",
    ".ogg",
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".otf",
    ".class",
    ".jar",
    ".war",
    ".pkl",
    ".pickle",
    ".npy",
    ".npz",
    ".pt",
    ".pth",
    ".onnx",
    ".h5",
    ".keras",
    ".wasm",
    ".ico",
    ".cur",
    ".msi",
    ".dmg",
    ".iso",
    ".img",
}


def is_binary_file(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    if ext in BINARY_EXTENSIONS:
        return True
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
        if not chunk:
            return False
        if b"\x00" in chunk:
            return True
        non_text = sum(1 for b in chunk if b < 9 or (13 < b < 32 and b not in (9, 10, 13)))
        if non_text / len(chunk) > 0.30:
            return True
    except OSError:
        return True
    return False


def read_text_file(path: str) -> list[str]:
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb2312", "latin-1"):
        try:
            with open(path, encoding=enc) as f:
                return f.read().splitlines()
        except (UnicodeDecodeError, OSError):
            continue
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()


def collect_text_files(root: str) -> list[str]:
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise NotADirectoryError(f"不是有效目录: {root}")

    files: list[str] = []
    for dirpath, _, filenames in os.walk(root):
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            if not is_binary_file(path):
                files.append(path)
    return sorted(files)


def count_file_lines(path: str) -> int:
    """快速统计行数（与 read_text_file 的 splitlines 行为一致）。"""
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb2312", "latin-1"):
        try:
            with open(path, encoding=enc) as f:
                return sum(1 for _ in f)
        except (UnicodeDecodeError, OSError):
            continue
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def ticks_for_file(line_count: int, lines_per_tick: int) -> int:
    """单文件播放所需 tick 数（与 _play_file 逻辑一致）。"""
    if line_count <= 0:
        return 1
    return (line_count + lines_per_tick - 1) // lines_per_tick


def format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h{m}m{s}s"
    if m > 0:
        return f"{m}m{s}s"
    return f"{s}s"


def estimate_playback_seconds(
    file_line_counts: list[int], lines_per_tick: int, tick_seconds: float
) -> float:
    total_ticks = sum(ticks_for_file(n, lines_per_tick) for n in file_line_counts)
    return total_ticks * tick_seconds


class FilePlayerApp:
    def __init__(self, root_dir: str) -> None:
        self.root_dir = os.path.abspath(root_dir)
        self.files = collect_text_files(self.root_dir)
        self.file_index = 0
        self._stop = threading.Event()
        self._playing = threading.Event()
        self._font_size = 11
        self._played_files = 0
        self._nav_lock = threading.Lock()
        self._jump_to_index: int | None = None
        self._nav_event = threading.Event()
        self._demo_mode = True
        self._stats_running = False

        self.win = tk.Tk()
        self.win.title("文件内容播放器")
        self._enter_fullscreen()
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)

        status_frame = tk.Frame(self.win)
        status_frame.pack(fill=tk.X, padx=8, pady=6)

        self.status_var = tk.StringVar()
        tk.Label(
            status_frame,
            textvariable=self.status_var,
            anchor="w",
            justify=tk.LEFT,
        ).pack(fill=tk.X)

        family = "Consolas"
        probe = tkfont.Font(family=family, size=self._font_size)
        if probe.actual("family") == "TkFixedFont":
            family = "Courier New"
        self._mono_font = tkfont.Font(family=family, size=self._font_size)

        text_frame = tk.Frame(self.win)
        text_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        self.line_nums = tk.Text(
            text_frame,
            width=6,
            wrap=tk.NONE,
            font=self._mono_font,
            state=tk.DISABLED,
            bg="#f0f0f0",
            fg="#666",
            relief=tk.FLAT,
            padx=4,
            pady=2,
            takefocus=0,
            cursor="arrow",
        )
        self.line_nums.pack(side=tk.LEFT, fill=tk.Y)

        self.text = scrolledtext.ScrolledText(
            text_frame,
            wrap=tk.NONE,
            font=self._mono_font,
            state=tk.DISABLED,
        )
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        def _sync_yscroll(first: str, last: str) -> None:
            self.line_nums.yview_moveto(first)
            self.text.vbar.set(first, last)

        self.text.configure(yscrollcommand=_sync_yscroll)

        def _forward_scroll(event: tk.Event) -> str:
            self.text.event_generate(
                "<MouseWheel>", x=event.x, y=event.y, delta=event.delta
            )
            return "break"

        self.line_nums.bind("<MouseWheel>", _forward_scroll)
        self.line_nums.bind("<Button-4>", _forward_scroll)
        self.line_nums.bind("<Button-5>", _forward_scroll)

        btn_frame = tk.Frame(self.win)
        btn_frame.pack(fill=tk.X, padx=8, pady=(0, 8))
        self.play_btn = tk.Button(
            btn_frame, text="播放", width=8, command=self._toggle_play
        )
        self.play_btn.pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(
            btn_frame,
            text="上一个文件",
            width=10,
            command=self._go_prev_file,
        ).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(
            btn_frame,
            text="下一个文件",
            width=10,
            command=self._go_next_file,
        ).pack(side=tk.LEFT, padx=(0, 6))
        self.stats_btn = tk.Button(
            btn_frame, text="统计", width=8, command=self._start_stats
        )
        self.stats_btn.pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(btn_frame, text="字体放大", width=10, command=self._zoom_in).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        tk.Button(btn_frame, text="字体缩小", width=10, command=self._zoom_out).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        tk.Label(btn_frame, text="停留时长").pack(side=tk.LEFT, padx=(12, 4))
        self.tick_var = tk.StringVar(value="1")
        tk.Entry(btn_frame, textvariable=self.tick_var, width=6).pack(
            side=tk.LEFT, padx=(0, 2)
        )
        tk.Label(btn_frame, text="秒").pack(side=tk.LEFT, padx=(0, 6))
        tk.Label(btn_frame, text="每次行数").pack(side=tk.LEFT, padx=(12, 4))
        self.lines_var = tk.StringVar(value="30")
        tk.Entry(btn_frame, textvariable=self.lines_var, width=6).pack(
            side=tk.LEFT, padx=(0, 2)
        )
        tk.Label(btn_frame, text="行").pack(side=tk.LEFT, padx=(0, 6))

        bottom_frame = tk.Frame(self.win, relief=tk.SUNKEN, bd=1)
        bottom_frame.pack(fill=tk.X, side=tk.BOTTOM, padx=8, pady=(0, 8))

        self.file_count_var = tk.StringVar(
            value=f"已播放: 0 / {len(self.files)}"
        )
        tk.Label(
            bottom_frame,
            textvariable=self.file_count_var,
            anchor="w",
            padx=8,
            pady=4,
        ).pack(fill=tk.X)

        self.progress_var = tk.StringVar(value="")
        tk.Label(
            bottom_frame,
            textvariable=self.progress_var,
            anchor="w",
            fg="#555",
            padx=8,
            pady=4,
        ).pack(fill=tk.X)

        self.status_var.set("预览模式：可调整字体大小与行号，点击「播放」开始浏览文件")
        self._show_demo()

        self._worker = threading.Thread(target=self._play_loop, daemon=True)
        self._worker.start()

    def _enter_fullscreen(self) -> None:
        try:
            self.win.geometry("1280x720")
            # self.win.attributes("-fullscreen", True)
        except tk.TclError:
            try:
                self.win.state("zoomed")
            except tk.TclError:
                w = self.win.winfo_screenwidth()
                h = self.win.winfo_screenheight()
                self.win.geometry(f"{w}x{h}+0+0")

    def _on_close(self) -> None:
        self._stop.set()
        self._playing.set()
        self.win.destroy()

    def _toggle_play(self) -> None:
        if self._playing.is_set():
            self._playing.clear()
            self.play_btn.config(text="播放")
        else:
            self._playing.set()
            self.play_btn.config(text="暂停")

    def _current_file_index(self) -> int:
        if not self.files:
            return 0
        if self.file_index >= len(self.files):
            return len(self.files) - 1
        return self.file_index

    def _go_prev_file(self) -> None:
        self._navigate_file(-1)

    def _go_next_file(self) -> None:
        self._navigate_file(1)

    def _navigate_file(self, delta: int) -> None:
        if not self.files:
            return
        base = self._current_file_index()
        new_idx = max(0, min(base + delta, len(self.files) - 1))
        if new_idx == base and self.file_index < len(self.files):
            return
        with self._nav_lock:
            self._jump_to_index = new_idx
        self._nav_event.set()
        if not self._worker.is_alive():
            self.file_index = new_idx
            with self._nav_lock:
                self._jump_to_index = None
            self._nav_event.clear()
            self._played_files = max(self._played_files, self.file_index + 1)
            self._worker = threading.Thread(target=self._play_loop, daemon=True)
            self._worker.start()

    def _take_jump_target(self) -> int | None:
        with self._nav_lock:
            if self._jump_to_index is None:
                return None
            idx = self._jump_to_index
            self._jump_to_index = None
            return idx

    def _should_abort_play(self) -> bool:
        return self._stop.is_set() or self._nav_event.is_set()

    def _zoom_in(self) -> None:
        self._set_font_size(self._font_size + 1)

    def _zoom_out(self) -> None:
        self._set_font_size(self._font_size - 1)

    def _set_font_size(self, size: int) -> None:
        self._font_size = max(6, min(72, size))
        self._mono_font.configure(size=self._font_size)
        self.text.configure(font=self._mono_font)
        self.line_nums.configure(font=self._mono_font)
        if self._demo_mode:
            self._show_demo()

    def _build_demo_display(self) -> tuple[list[int], list[str]]:
        line_nums = [1, 2, 3, 4]
        content_lines = ["", f"{INDENT_SPACES}完整文件名", "", ""]
        for n in range(5, 5 + DEMO_CONTENT_LINES):
            line_nums.append(n)
            content_lines.append(f"{INDENT_SPACES}{_demo_line_text(n)}")
        return line_nums, content_lines

    def _show_demo(self) -> None:
        line_nums, content_lines = self._build_demo_display()
        self._set_display(line_nums, content_lines)
        self.progress_var.set(
            f"预览 {DEMO_CONTENT_LINES} 行  |  点击「播放」开始浏览文件"
        )

    def _set_line_num_width(self, max_num: int) -> None:
        width = max(4, len(str(max(max_num, 1))) + 1)
        self.line_nums.configure(width=width)

    def _wait_until_playing(self) -> bool:
        while not self._playing.is_set():
            if self._stop.is_set():
                return False
            if self._nav_event.is_set():
                return True
            time.sleep(0.05)
        return True

    def _get_lines_per_tick(self) -> int:
        try:
            val = int(self.lines_var.get().strip())
            if val <= 0:
                raise ValueError
            return min(val, 10000)
        except (ValueError, tk.TclError):
            return DEFAULT_LINES_PER_TICK

    def _get_tick_seconds(self) -> float:
        try:
            val = float(self.tick_var.get().strip())
            if val <= 0:
                raise ValueError
            return min(val, 3600.0)
        except (ValueError, tk.TclError):
            return DEFAULT_TICK_SECONDS

    def _start_stats(self) -> None:
        if self._stats_running:
            return
        self._stats_running = True
        self.stats_btn.config(state=tk.DISABLED)
        tick = self._get_tick_seconds()
        lines_per = self._get_lines_per_tick()
        root_dir = self.root_dir
        threading.Thread(
            target=self._stats_worker,
            args=(root_dir, tick, lines_per),
            daemon=True,
        ).start()

    def _stats_worker(self, root_dir: str, tick: float, lines_per: int) -> None:
        try:
            files = collect_text_files(root_dir)
            line_counts = [count_file_lines(p) for p in files]
            total_lines = sum(line_counts)
            seconds = estimate_playback_seconds(line_counts, lines_per, tick)
            duration = format_duration(seconds)
            tick_s = f"{tick:g}" if tick == int(tick) else f"{tick:.2f}"
            msg = (
                f"目录: {root_dir}\n\n"
                f"文本文件: {len(files)} 个\n"
                f"总行数: {total_lines:,}\n"
                f"播放参数: 每 {tick_s} 秒 {lines_per} 行\n\n"
                f"预计播放时长: {duration}"
            )
        except OSError as e:
            msg = f"统计失败: {e}"
        self.win.after(0, lambda: self._stats_done(msg))

    def _stats_done(self, msg: str) -> None:
        self._stats_running = False
        self.stats_btn.config(state=tk.NORMAL)
        messagebox.showinfo("播放时长统计", msg, parent=self.win)

    def _update_file_count(self) -> None:
        total_files = len(self.files)
        self.file_count_var.set(f"已播放: {self._played_files} / {total_files}")

    def _set_status(self, path: str, line_start: int, line_end: int, total: int) -> None:
        rel = os.path.relpath(path, self.root_dir)
        idx = self.file_index + 1
        total_files = len(self.files)
        self.status_var.set(f"[{idx}/{total_files}] {rel}")
        tick = self._get_tick_seconds()
        lines_per = self._get_lines_per_tick()
        tick_s = f"{tick:g}" if tick == int(tick) else f"{tick:.2f}"
        self.progress_var.set(
            f"行 {line_start + 1}–{line_end} / {total}  |  每 {tick_s} 秒 {lines_per} 行"
        )
        self._update_file_count()

    def _build_display(
        self,
        path: str,
        chunk: list[str],
        start: int,
        *,
        empty: bool = False,
    ) -> tuple[list[int], list[str]]:
        """返回 (行号列表, 内容行列表)，顶部空一行，文件名与正文之间空两行。"""
        line_nums = [1, 2, 3, 4]
        content_lines = [
            "",
            f"{INDENT_SPACES}{os.path.abspath(path)}",
            "",
            "",
        ]
        if empty:
            content_lines.append(f"{INDENT_SPACES}(空文件)")
            line_nums.append(1)
        else:
            for i, line in enumerate(chunk):
                content_lines.append(f"{INDENT_SPACES}{line}")
                line_nums.append(start + i + 1)
        return line_nums, content_lines

    def _format_line_nums(self, line_nums: list[int]) -> str:
        if not line_nums:
            return ""
        width = len(str(max(line_nums)))
        return "\n".join(f"{n:>{width}}" for n in line_nums)

    def _set_display(self, line_nums: list[int], content_lines: list[str]) -> None:
        nums_text = self._format_line_nums(line_nums)
        body = "\n".join(content_lines)
        max_num = max(line_nums) if line_nums else 1
        self._set_line_num_width(max_num)
        self.line_nums.config(state=tk.NORMAL)
        self.line_nums.delete("1.0", tk.END)
        self.line_nums.insert("1.0", nums_text)
        self.line_nums.config(state=tk.DISABLED)
        self.text.config(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", body)
        self.text.config(state=tk.DISABLED)
        self.line_nums.yview_moveto(self.text.yview()[0])

    def _show_lines(self, path: str, lines: list[str], start: int, end: int) -> None:
        chunk = lines[start:end]
        if not chunk and start >= len(lines):
            line_nums, content_lines = self._build_display(path, [], start, empty=True)
        else:
            line_nums, content_lines = self._build_display(path, chunk, start)

        def update() -> None:
            self._demo_mode = False
            self._set_display(line_nums, content_lines)
            self._set_status(path, start, min(end, len(lines)), len(lines))

        self.win.after(0, update)

    def _wait_tick(self) -> bool:
        """等待一个播放周期；返回 False 表示应停止。"""
        if self._stop.is_set():
            return False
        deadline = time.monotonic() + self._get_tick_seconds()
        while time.monotonic() < deadline:
            if self._stop.is_set():
                return False
            if self._nav_event.is_set():
                return False
            if not self._playing.is_set():
                time.sleep(0.05)
                continue
            time.sleep(0.05)
        return True

    def _play_file(self, path: str) -> None:
        try:
            lines = read_text_file(path)
        except OSError as e:
            self.win.after(
                0,
                lambda: self._set_status(path, 0, 0, 0),
            )
            self.win.after(
                0,
                lambda: self._show_error(
                    path, f"无法读取: {path}\n{e}"
                ),
            )
            if self._wait_tick():
                pass
            return

        pos = 0
        total = len(lines)
        while pos < total:
            if self._should_abort_play():
                return
            end = min(pos + self._get_lines_per_tick(), total)
            self._show_lines(path, lines, pos, end)
            pos = end
            if pos < total:
                if not self._wait_tick():
                    return
            elif not self._should_abort_play():
                self._wait_tick()

    def _show_error(self, path: str, msg: str) -> None:
        self._demo_mode = False
        line_nums, content_lines = self._build_display(
            path, msg.splitlines(), 0
        )
        self._set_display(line_nums, content_lines)

    def _play_loop(self) -> None:
        if not self.files:
            self.win.after(
                0,
                lambda: self.status_var.set(f"目录下没有可播放的文本文件: {self.root_dir}"),
            )
            self.win.after(0, self._update_file_count)
            return

        while self.file_index < len(self.files) and not self._stop.is_set():
            if not self._wait_until_playing():
                return
            path = self.files[self.file_index]
            self._play_file(path)
            jump = self._take_jump_target()
            if jump is not None:
                self.file_index = jump
                self._nav_event.clear()
                self._played_files = max(self._played_files, self.file_index + 1)
                self.win.after(0, self._update_file_count)
                continue
            self.file_index += 1
            self._played_files = self.file_index
            self.win.after(0, self._update_file_count)

        if not self._stop.is_set():
            def _done() -> None:
                self.status_var.set("全部文件播放完毕")
                self.progress_var.set("")
                self._update_file_count()

            self.win.after(0, _done)

    def run(self) -> None:
        self.win.mainloop()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="递归播放文件夹内文本文件内容（每秒 30 行，跳过二进制文件）",
    )
    parser.add_argument(
        "folder",
        help="要遍历的根目录",
    )
    args = parser.parse_args()

    folder = os.path.abspath(args.folder)
    if not os.path.isdir(folder):
        print(f"错误: 目录不存在: {folder}", file=sys.stderr)
        return 1

    app = FilePlayerApp(folder)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
