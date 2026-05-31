#!/usr/bin/env python3
"""视频二维码解析播放器：播放视频并显示每帧二维码原始文本。"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import scrolledtext

try:
    import cv2
    from PIL import Image, ImageTk
except ImportError as e:
    print(
        "缺少依赖，请先安装: pip install opencv-python Pillow",
        file=sys.stderr,
    )
    raise SystemExit(1) from e

from pic2txt import decode_qr_from_bgr

DEFAULT_START_FRAME = 1
DEFAULT_INTERVAL_SECONDS = 1.0
NO_QR_PLACEHOLDER = "(未识别到二维码)"


def decode_qr_text_from_frame(frame_bgr) -> str:
    """从 BGR 帧解码二维码，返回原始字符串（latin-1 可逆字节表示）。"""
    payload = decode_qr_from_bgr(frame_bgr)
    if payload is None:
        return ""
    return payload.decode("latin-1")


class QrcVideoParserApp:
    def __init__(self, video_path: str) -> None:
        self.video_path = os.path.abspath(video_path)

        self.cap = cv2.VideoCapture(self.video_path)
        if not self.cap.isOpened():
            raise ValueError(f"无法打开视频: {self.video_path}")

        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 25.0
        if self.fps <= 0:
            self.fps = 25.0
        self.total_frames = max(0, int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT)))

        self._stop = threading.Event()
        self._playing = threading.Event()
        self._seek_lock = threading.Lock()
        self._current_frame = 0
        self._photo: ImageTk.PhotoImage | None = None
        self._updating_scale = False
        self._selected_play_armed = False

        self.win = tk.Tk()
        self.win.title("视频二维码解析")
        self.win.geometry("1400x700")
        self.win.minsize(900, 500)
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)

        header = tk.Frame(self.win)
        header.pack(fill=tk.X, padx=8, pady=6)
        self.header_var = tk.StringVar()
        tk.Label(header, textvariable=self.header_var, anchor="w").pack(fill=tk.X)
        self._refresh_header()

        body = tk.Frame(self.win)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        # 设置（最右）
        settings_lf = tk.LabelFrame(body, text="设置", padx=8, pady=8)
        settings_lf.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))

        tk.Label(settings_lf, text="开始帧索引", anchor="w").pack(fill=tk.X, pady=(4, 4))
        self.start_frame_var = tk.StringVar(value=str(DEFAULT_START_FRAME))
        tk.Entry(settings_lf, textvariable=self.start_frame_var, width=12).pack(
            fill=tk.X, pady=(0, 12)
        )

        tk.Label(settings_lf, text="间隔时间", anchor="w").pack(fill=tk.X, pady=(0, 4))
        interval_row = tk.Frame(settings_lf)
        interval_row.pack(fill=tk.X)
        self.interval_var = tk.StringVar(value=str(int(DEFAULT_INTERVAL_SECONDS)))
        tk.Entry(interval_row, textvariable=self.interval_var, width=8).pack(side=tk.LEFT)
        tk.Label(interval_row, text="秒").pack(side=tk.LEFT, padx=(4, 0))

        # 文本显示（中）
        text_lf = tk.LabelFrame(body, text="文本显示", padx=4, pady=4)
        text_lf.pack(side=tk.LEFT, fill=tk.BOTH, padx=(8, 0))
        text_lf.configure(width=360)
        text_lf.pack_propagate(False)

        self.text_widget = scrolledtext.ScrolledText(
            text_lf,
            wrap=tk.WORD,
            state=tk.DISABLED,
            font=("Consolas", 10),
        )
        self.text_widget.pack(fill=tk.BOTH, expand=True)

        # 视频（左，可扩展）
        video_lf = tk.LabelFrame(body, text="视频", padx=4, pady=4)
        video_lf.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.video_container = tk.Frame(video_lf, bg="#111")
        self.video_container.pack(fill=tk.BOTH, expand=True)
        self.video_label = tk.Label(self.video_container, bg="#111")
        self.video_label.pack(expand=True)
        self.frame_idx_label = tk.Label(
            self.video_container,
            text="1",
            bg="#000000",
            fg="#ffffff",
            font=("Consolas", 14, "bold"),
            padx=8,
            pady=4,
        )
        self.frame_idx_label.place(x=8, y=8, anchor="nw")

        ctrl = tk.Frame(video_lf)
        ctrl.pack(fill=tk.X, pady=(8, 0))
        self.play_btn = tk.Button(
            ctrl, text="播放", width=8, command=self._toggle_play
        )
        self.play_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.selected_frames_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            ctrl,
            text="仅播放选帧",
            variable=self.selected_frames_var,
        ).pack(side=tk.LEFT)

        progress_row = tk.Frame(video_lf)
        progress_row.pack(fill=tk.X, pady=(8, 0))
        scale_max = max(1, self.total_frames) if self.total_frames > 0 else 1
        self.progress_var = tk.IntVar(value=1)
        self.progress_scale = tk.Scale(
            progress_row,
            from_=1,
            to=scale_max,
            orient=tk.HORIZONTAL,
            variable=self.progress_var,
            showvalue=False,
            command=self._on_progress_drag,
        )
        self.progress_scale.pack(fill=tk.X, side=tk.LEFT, expand=True)
        self.frame_count_var = tk.StringVar()
        tk.Label(progress_row, textvariable=self.frame_count_var, width=28, anchor="e").pack(
            side=tk.RIGHT, padx=(8, 0)
        )

        self.video_container.bind("<Configure>", self._on_container_resize)
        self._show_frame(0, decode=True)
        self._worker = threading.Thread(target=self._play_loop, daemon=True)
        self._worker.start()

    def _refresh_header(self) -> None:
        name = os.path.basename(self.video_path)
        total = self.total_frames if self.total_frames > 0 else "?"
        fps_s = f"{self.fps:g}" if self.fps == int(self.fps) else f"{self.fps:.2f}"
        self.header_var.set(
            f"视频: {name}  |  帧率: {fps_s} fps  |  总帧数: {total}"
        )

    def _get_start_frame(self) -> int:
        try:
            val = int(self.start_frame_var.get().strip())
            if val < 1:
                raise ValueError
            if self.total_frames > 0:
                return min(val, self.total_frames)
            return val
        except (ValueError, tk.TclError):
            return DEFAULT_START_FRAME

    def _get_interval_seconds(self) -> float:
        try:
            val = float(self.interval_var.get().strip())
            if val <= 0:
                raise ValueError
            return min(val, 3600.0)
        except (ValueError, tk.TclError):
            return DEFAULT_INTERVAL_SECONDS

    def _frame_step(self) -> int:
        interval = self._get_interval_seconds()
        return max(1, int(round(interval * self.fps)))

    def _update_frame_count_label(self) -> None:
        current = self._current_frame + 1
        if self.total_frames > 0:
            remaining = max(0, self.total_frames - current)
            self.frame_count_var.set(
                f"当前帧: {current}  |  剩余: {remaining}"
            )
        else:
            self.frame_count_var.set(f"当前帧: {current}")

    def _sync_progress_scale(self) -> None:
        self._updating_scale = True
        try:
            self.progress_var.set(self._current_frame + 1)
        finally:
            self._updating_scale = False
        self._update_frame_count_label()

    def _on_progress_drag(self, _value: str) -> None:
        if self._updating_scale:
            return
        self._playing.clear()
        self.win.after(0, lambda: self.play_btn.config(text="播放"))
        try:
            target = int(float(_value)) - 1
        except ValueError:
            return
        self._display_at(target)

    def _set_text_panel(self, text: str) -> None:
        display = text if text else NO_QR_PLACEHOLDER
        self.text_widget.config(state=tk.NORMAL)
        self.text_widget.delete("1.0", tk.END)
        self.text_widget.insert(tk.END, display)
        self.text_widget.config(state=tk.DISABLED)

    def _on_close(self) -> None:
        self._stop.set()
        self._playing.set()
        self.cap.release()
        self.win.destroy()

    def _toggle_play(self) -> None:
        if self._playing.is_set():
            self._playing.clear()
            self.play_btn.config(text="播放")
            return

        if self.selected_frames_var.get():
            start = self._get_start_frame()
            start_index = start - 1
            if self._selected_play_armed or self._current_frame < start_index:
                self._display_at(start_index)
            self._selected_play_armed = False

        self._playing.set()
        self.play_btn.config(text="暂停")

    def _on_container_resize(self, _event: tk.Event | None = None) -> None:
        ret, frame = self._read_frame_at(self._current_frame)
        if ret and frame is not None:
            self._photo = self._bgr_to_photo(frame)
            self.video_label.configure(image=self._photo)

    def _read_frame_at(self, index: int) -> tuple[bool, object | None]:
        with self._seek_lock:
            if index < 0:
                index = 0
            if self.total_frames > 0:
                index = min(index, self.total_frames - 1)
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, index)
            ret, frame = self.cap.read()
            if ret:
                self._current_frame = index
            return ret, frame

    def _bgr_to_photo(self, frame_bgr) -> ImageTk.PhotoImage:
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(frame_rgb)

        cw = max(1, self.video_container.winfo_width())
        ch = max(1, self.video_container.winfo_height())
        iw, ih = img.size
        scale = min(cw / iw, ch / ih)
        if scale < 1.0 or (iw > cw or ih > ch):
            nw = max(1, int(iw * scale))
            nh = max(1, int(ih * scale))
            img = img.resize((nw, nh), Image.Resampling.LANCZOS)
        return ImageTk.PhotoImage(img)

    def _display_at(self, index: int, *, decode: bool = True) -> str:
        ret, frame = self._read_frame_at(index)
        if not ret or frame is None:
            return ""

        qr_text = decode_qr_text_from_frame(frame) if decode else ""

        def update() -> None:
            self._photo = self._bgr_to_photo(frame)
            self.video_label.configure(image=self._photo)
            self.frame_idx_label.config(text=str(self._current_frame + 1))
            self._sync_progress_scale()
            if decode:
                self._set_text_panel(qr_text)

        if threading.current_thread() is threading.main_thread():
            update()
        else:
            self.win.after(0, update)
        return qr_text

    def _show_frame(self, index: int, *, decode: bool = True) -> None:
        self._display_at(index, decode=decode)

    def _wait_until_playing(self) -> bool:
        while not self._playing.is_set():
            if self._stop.is_set():
                return False
            time.sleep(0.03)
        return True

    def _sleep_while_playing(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._stop.is_set():
                return False
            if not self._playing.is_set():
                return False
            time.sleep(0.02)
        return True

    def _stop_playback_ui(self, extra: str = "") -> None:
        self._playing.clear()

        def done() -> None:
            self.play_btn.config(text="播放")
            self._selected_play_armed = True
            if extra:
                self.frame_count_var.set(
                    f"{self.frame_count_var.get()}  |  {extra}"
                )

        self.win.after(0, done)

    def _play_loop(self) -> None:
        while not self._stop.is_set():
            if not self._wait_until_playing():
                break

            if self.selected_frames_var.get():
                self._play_selected_frames()
            else:
                self._play_continuous()

    def _play_continuous(self) -> None:
        next_index = self._current_frame + 1
        if self.total_frames > 0 and next_index >= self.total_frames:
            self._stop_playback_ui("播放完毕")
            time.sleep(0.1)
            return

        ret, frame = self._read_frame_at(next_index)
        if not ret or frame is None:
            self._stop_playback_ui()
            return

        qr_text = decode_qr_text_from_frame(frame)

        def update() -> None:
            self._photo = self._bgr_to_photo(frame)
            self.video_label.configure(image=self._photo)
            self.frame_idx_label.config(text=str(self._current_frame + 1))
            self._sync_progress_scale()
            self._set_text_panel(qr_text)

        self.win.after(0, update)

        delay = max(0.001, 1.0 / self.fps)
        if not self._sleep_while_playing(delay):
            return

    def _play_selected_frames(self) -> None:
        interval = self._get_interval_seconds()
        if not self._sleep_while_playing(interval):
            return

        step = self._frame_step()
        next_index = self._current_frame + step

        if self.total_frames > 0 and next_index >= self.total_frames:
            self._stop_playback_ui("播放完毕")
            time.sleep(0.1)
            return

        ret, frame = self._read_frame_at(next_index)
        if not ret or frame is None:
            self._stop_playback_ui()
            return

        qr_text = decode_qr_text_from_frame(frame)

        def update() -> None:
            self._photo = self._bgr_to_photo(frame)
            self.video_label.configure(image=self._photo)
            self.frame_idx_label.config(text=str(self._current_frame + 1))
            self._sync_progress_scale()
            self._set_text_panel(qr_text)

        self.win.after(0, update)

    def run(self) -> None:
        self.win.mainloop()


def main() -> int:
    parser = argparse.ArgumentParser(description="视频二维码解析播放器")
    parser.add_argument("video", help="视频文件路径")
    args = parser.parse_args()

    video_path = os.path.abspath(args.video)
    if not os.path.isfile(video_path):
        print(f"错误: 视频文件不存在: {video_path}", file=sys.stderr)
        return 1

    try:
        app = QrcVideoParserApp(video_path)
    except ValueError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
