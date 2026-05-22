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
QRC_PREFIX = ".qrc%%"
QR_BOX_SIZE = 8
QR_BORDER = 2


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


def build_qr_payload(index: int, chunk: bytes) -> bytes:
    return f"{index}{QRC_PREFIX}".encode("ascii") + chunk


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
    def __init__(self, file_path: str) -> None:
        self.file_path = os.path.abspath(file_path)
        self.file_size = os.path.getsize(self.file_path)
        self.chunks = load_file_chunks(self.file_path)
        self.total_count = len(self.chunks)

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

    def _refresh_header(self) -> None:
        name = os.path.basename(self.file_path)
        self.header_var.set(
            f"文件: {name}  |  大小: {self.file_size:,} 字节  |  二维码总数: {self.total_count}"
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
        self._log_file.write(
            f"# 源文件: {self.file_path}\n"
            f"# 格式: 序号:块MD5:二维码内容MD5\n"
        )

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
        payload = build_qr_payload(index, chunk)
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
        count = self.total_count
        seconds = count * tick
        tick_s = f"{tick:g}" if tick == int(tick) else f"{tick:.2f}"
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

    def _play_loop(self) -> None:
        while self._current_index < self.total_count and not self._stop.is_set():
            if not self._wait_until_playing():
                continue

            index = self._current_index + 1
            chunk = self.chunks[self._current_index]
            payload = build_qr_payload(index, chunk)

            self._show_qr(index, chunk)
            self._append_log(index, chunk, payload)
            self._played_count = index
            self.win.after(0, self._update_status_bar)

            self._current_index += 1
            if self._current_index < self.total_count:
                if not self._wait_tick():
                    return
            elif not self._stop.is_set():
                self._wait_tick()

        if not self._stop.is_set() and self._played_count >= self.total_count:
            def done() -> None:
                self._playing.clear()
                self.play_btn.config(text="播放")
                self.status_var.set(
                    f"播放完毕  |  共 {self.total_count} 张  |  "
                    f"日志: {self._log_path or '(未生成)'}"
                )

            self.win.after(0, done)

    def run(self) -> None:
        self.win.mainloop()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="将文件分块编码为二维码并顺序播放",
    )
    parser.add_argument("file", help="要播放的文件路径（可为二进制/压缩包）")
    args = parser.parse_args()

    path = os.path.abspath(args.file)
    if not os.path.isfile(path):
        print(f"错误: 文件不存在: {path}", file=sys.stderr)
        return 1

    app = File2QrcPlayerApp(path)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
