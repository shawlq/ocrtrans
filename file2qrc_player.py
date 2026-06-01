#!/usr/bin/env python3
"""将文件按块编码为二维码并顺序播放。"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox

try:
    import qrcode
    from PIL import Image, ImageTk
except ImportError as e:
    print(
        "缺少依赖，请先安装: pip install qrcode[pil] Pillow",
        file=sys.stderr,
    )
    raise SystemExit(1) from e

CHUNK_SIZE = 2000
DEFAULT_TICK_SECONDS = 1.0
QR_BOX_SIZE = 8
QR_BORDER = 2

# 目录遍历时跳过的二进制/非文本类扩展名
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
    ".cur",
    ".msi",
    ".dmg",
    ".iso",
    ".img",
}

# 目录遍历时跳过的子目录（版本库、依赖、缓存等）
SKIP_DIR_NAMES = {
    ".git",
    ".svn",
    ".hg",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
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
        non_text = sum(
            1 for b in chunk if b < 9 or (13 < b < 32 and b not in (9, 10, 13))
        )
        if non_text / len(chunk) > 0.30:
            return True
    except OSError:
        return True
    return False


def collect_files(root: str) -> list[str]:
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise NotADirectoryError(f"不是有效目录: {root}")

    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIR_NAMES)
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            if not is_binary_file(path):
                files.append(path)
    return sorted(files)


def format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}小时{m}分{s}秒"
    if m > 0:
        return f"{m}分{s}秒"
    return f"{s}秒"


def load_file_chunks(path: str) -> list[bytes]:
    with open(path, "rb") as f:
        data = f.read()
    if not data:
        return [b""]
    chunks: list[bytes] = []
    for i in range(0, len(data), CHUNK_SIZE):
        chunks.append(data[i : i + CHUNK_SIZE])
    return chunks


def chunk_count_for_size(size: int) -> int:
    if size <= 0:
        return 1
    return (size + CHUNK_SIZE - 1) // CHUNK_SIZE


LAST_CHUNK_INDEX = 99999999


def build_qr_payload(rel_path: str, index: int, chunk: bytes) -> bytes:
    path = rel_path.replace("\\", "/")
    return f"{path}%%{index}%%".encode("ascii") + chunk


def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def make_qr_image(payload: bytes) -> Image.Image:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=QR_BOX_SIZE,
        border=QR_BORDER,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")


class File2QrcPlayerApp:
    def __init__(self, files: list[str], root_dir: str | None = None) -> None:
        self.files = [os.path.abspath(p) for p in files]
        self.root_dir = os.path.abspath(root_dir) if root_dir else None
        self.file_index = 0
        self.file_path = ""
        self.file_size = 0
        self.chunks: list[bytes] = []
        self.total_count = 0
        self._load_current_file()

        self._stop = threading.Event()
        self._playing = threading.Event()
        self._play_started = False
        self._current_index = 0
        self._played_count = 0
        self._log_path: str | None = None
        self._log_file = None
        self._photo: ImageTk.PhotoImage | None = None
        self._stats_running = False

        self.win = tk.Tk()
        self.win.title("文件二维码播放器")
        self.win.geometry("900x700")
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)

        top = tk.Frame(self.win)
        top.pack(fill=tk.X, padx=8, pady=6)
        self.header_var = tk.StringVar()
        tk.Label(top, textvariable=self.header_var, anchor="w").pack(fill=tk.X)
        self._refresh_header()

        self.qr_label = tk.Label(self.win, bg="white")
        self.qr_label.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        btn_frame = tk.Frame(self.win)
        btn_frame.pack(fill=tk.X, padx=8, pady=(0, 6))
        self.play_btn = tk.Button(
            btn_frame, text="播放", width=8, command=self._toggle_play
        )
        self.play_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.stats_btn = tk.Button(
            btn_frame, text="统计", width=8, command=self._start_stats
        )
        self.stats_btn.pack(side=tk.LEFT, padx=(0, 6))
        tk.Label(btn_frame, text="停留时长").pack(side=tk.LEFT, padx=(12, 4))
        self.tick_var = tk.StringVar(value="1")
        tk.Entry(btn_frame, textvariable=self.tick_var, width=6).pack(
            side=tk.LEFT, padx=(0, 2)
        )
        tk.Label(btn_frame, text="秒").pack(side=tk.LEFT, padx=(0, 6))
        self.save_img_var = tk.BooleanVar(value=False)
        tk.Checkbutton(btn_frame, text="生成图片", variable=self.save_img_var).pack(
            side=tk.LEFT, padx=(12, 0)
        )

        bottom_frame = tk.Frame(self.win, relief=tk.SUNKEN, bd=1)
        bottom_frame.pack(fill=tk.X, side=tk.BOTTOM, padx=8, pady=(0, 8))
        self.status_var = tk.StringVar()
        tk.Label(
            bottom_frame,
            textvariable=self.status_var,
            anchor="w",
            padx=8,
            pady=4,
        ).pack(fill=tk.X)
        self._update_status_bar()

        self._worker = threading.Thread(target=self._play_loop, daemon=True)
        self._worker.start()

    def _load_current_file(self) -> None:
        self.file_path = self.files[self.file_index]
        self.file_size = os.path.getsize(self.file_path)
        self.chunks = load_file_chunks(self.file_path)
        self.total_count = len(self.chunks)
        self._current_index = 0
        self._played_count = 0

    def _display_name(self, path: str) -> str:
        if self.root_dir:
            try:
                return os.path.relpath(path, self.root_dir)
            except ValueError:
                pass
        return os.path.basename(path)

    def _total_chunks_all(self) -> int:
        total = 0
        for path in self.files:
            total += chunk_count_for_size(os.path.getsize(path))
        return total

    def _refresh_header(self) -> None:
        name = self._display_name(self.file_path)
        if len(self.files) > 1:
            prefix = f"[{self.file_index + 1}/{len(self.files)}] "
        else:
            prefix = ""
        self.header_var.set(
            f"文件: {prefix}{name}  |  大小: {self.file_size:,} 字节  |  "
            f"本文件二维码: {self.total_count}"
        )

    def _get_tick_seconds(self) -> float:
        try:
            val = float(self.tick_var.get().strip())
            if val <= 0:
                raise ValueError
            return min(val, 3600.0)
        except (ValueError, tk.TclError):
            return DEFAULT_TICK_SECONDS

    def _remaining_count(self) -> int:
        return max(0, self.total_count - self._played_count)

    def _remaining_seconds(self) -> float:
        return self._remaining_count() * self._get_tick_seconds()

    def _update_status_bar(self) -> None:
        remaining = self._remaining_count()
        self.status_var.set(
            f"已播放: {self._played_count} / 剩余: {remaining}  |  "
            f"剩余时间: {format_duration(self._remaining_seconds())}"
        )

    def _on_close(self) -> None:
        self._stop.set()
        self._playing.set()
        self._close_log()
        self.win.destroy()

    def _close_log(self) -> None:
        if self._log_file is not None:
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None

    def _toggle_play(self) -> None:
        if self._playing.is_set():
            self._playing.clear()
            self.play_btn.config(text="播放")
            return
        if not self._play_started:
            self._play_started = True
            self._open_log_file()
        self._playing.set()
        self.play_btn.config(text="暂停")

    def _open_log_file(self) -> None:
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        self._log_path = os.path.join(os.getcwd(), f"output_qrc_{stamp}.txt")
        self._log_file = open(self._log_path, "w", encoding="utf-8")
        if self.root_dir:
            self._log_file.write(f"# 源目录: {self.root_dir}\n# 文件数: {len(self.files)}\n")
        self._log_file.write("# 格式: 序号:块MD5:二维码内容MD5\n")
        self._write_log_file_header()

    def _write_log_file_header(self) -> None:
        if self._log_file is None:
            return
        self._log_file.write(f"# 源文件: {self.file_path}\n")

    def _append_log(self, index: int, chunk: bytes, payload: bytes) -> None:
        if self._log_file is None:
            return
        line = f"{index}:{md5_hex(chunk)}:{md5_hex(payload)}\n"
        self._log_file.write(line)
        self._log_file.flush()

    def _save_qr_image(self, index: int, img: Image.Image) -> None:
        if not self.save_img_var.get():
            return
        stamp = os.path.basename(self._log_path or "output_qrc")
        if stamp.endswith(".txt"):
            stamp = stamp[:-4]
        out_dir = os.path.join(os.getcwd(), f"{stamp}_images")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"qrc_{index:06d}.png")
        img.save(path, format="PNG")

    def _show_qr(self, index: int, chunk: bytes) -> None:
        rel_path = self._display_name(self.file_path)
        payload = build_qr_payload(rel_path, index, chunk)
        img = make_qr_image(payload)
        self._save_qr_image(index, img)

        max_side = min(self.win.winfo_width() - 40, self.win.winfo_height() - 160)
        max_side = max(200, max_side)
        display_img = img.resize((max_side, max_side), Image.Resampling.NEAREST)

        def update() -> None:
            self._photo = ImageTk.PhotoImage(display_img)
            self.qr_label.configure(image=self._photo, text="")

        self.win.after(0, update)

    def _start_stats(self) -> None:
        if self._stats_running:
            return
        self._stats_running = True
        self.stats_btn.config(state=tk.DISABLED)
        tick = self._get_tick_seconds()
        count = self._total_chunks_all() if len(self.files) > 1 else self.total_count
        seconds = count * tick
        tick_s = f"{tick:g}" if tick == int(tick) else f"{tick:.2f}"
        if len(self.files) > 1:
            msg = (
                f"目录: {self.root_dir}\n\n"
                f"文件数: {len(self.files)}\n"
                f"每块大小: {CHUNK_SIZE} 字节\n"
                f"二维码总张数: {count}\n"
                f"每张停留: {tick_s} 秒\n\n"
                f"预计播放时长: {format_duration(seconds)}"
            )
        else:
            msg = (
                f"文件: {self.file_path}\n\n"
                f"文件大小: {self.file_size:,} 字节\n"
                f"每块大小: {CHUNK_SIZE} 字节\n"
                f"二维码张数: {count}\n"
                f"每张停留: {tick_s} 秒\n\n"
                f"预计播放时长: {format_duration(seconds)}"
            )
        self.win.after(0, lambda: self._stats_done(msg))

    def _stats_done(self, msg: str) -> None:
        self._stats_running = False
        self.stats_btn.config(state=tk.NORMAL)
        messagebox.showinfo("播放统计", msg, parent=self.win)

    def _wait_until_playing(self) -> bool:
        while not self._playing.is_set():
            if self._stop.is_set():
                return False
            time.sleep(0.05)
        return True

    def _wait_tick(self) -> bool:
        if self._stop.is_set():
            return False
        deadline = time.monotonic() + self._get_tick_seconds()
        while time.monotonic() < deadline:
            if self._stop.is_set():
                return False
            if not self._playing.is_set():
                time.sleep(0.05)
                continue
            time.sleep(0.05)
        return True

    def _play_current_file(self) -> bool:
        while self._current_index < self.total_count and not self._stop.is_set():
            if not self._wait_until_playing():
                continue

            seg = self._current_index + 1
            index = LAST_CHUNK_INDEX if seg >= self.total_count else seg
            chunk = self.chunks[self._current_index]
            rel_path = self._display_name(self.file_path)
            payload = build_qr_payload(rel_path, index, chunk)

            self._show_qr(index, chunk)
            self._append_log(index, chunk, payload)
            self._played_count = index
            self.win.after(0, self._update_status_bar)

            self._current_index += 1
            if self._current_index < self.total_count:
                if not self._wait_tick():
                    return False
            elif not self._stop.is_set():
                self._wait_tick()
        return not self._stop.is_set()

    def _play_loop(self) -> None:
        while self.file_index < len(self.files) and not self._stop.is_set():
            if self.file_index > 0:
                self._load_current_file()
                self._write_log_file_header()
                self.win.after(0, self._refresh_header)

            if not self._play_current_file():
                return

            self.file_index += 1

        if not self._stop.is_set():
            def done() -> None:
                self._playing.clear()
                self.play_btn.config(text="播放")
                total_qr = self._total_chunks_all() if len(self.files) > 1 else self.total_count
                if len(self.files) > 1:
                    summary = f"全部播放完毕  |  {len(self.files)} 个文件  |  共 {total_qr} 张"
                else:
                    summary = f"播放完毕  |  共 {total_qr} 张"
                self.status_var.set(f"{summary}  |  日志: {self._log_path or '(未生成)'}")

            self.win.after(0, done)

    def run(self) -> None:
        self.win.mainloop()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="将文件分块编码为二维码并顺序播放",
    )
    parser.add_argument(
        "path",
        help="要播放的文件路径，或递归遍历的目录（目录模式跳过二进制文件）",
    )
    args = parser.parse_args()

    path = os.path.abspath(args.path)
    if os.path.isdir(path):
        files = collect_files(path)
        if not files:
            print(f"错误: 目录内没有可播放的文件: {path}", file=sys.stderr)
            return 1
        app = File2QrcPlayerApp(files, root_dir=path)
    elif os.path.isfile(path):
        app = File2QrcPlayerApp([path])
    else:
        print(f"错误: 路径不存在: {path}", file=sys.stderr)
        return 1
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
