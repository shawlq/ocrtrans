#!/usr/bin/env python3
"""视频按时间间隔截图工具。"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox

try:
    import cv2
    from PIL import Image, ImageTk
except ImportError as e:
    print(
        "缺少依赖，请先安装: pip install opencv-python Pillow",
        file=sys.stderr,
    )
    raise SystemExit(1) from e

DEFAULT_START_FRAME = 1
DEFAULT_INTERVAL_SECONDS = 1.0


def output_dir_for_video(video_path: str) -> str:
    video_path = os.path.abspath(video_path)
    parent = os.path.dirname(video_path)
    stem = os.path.splitext(os.path.basename(video_path))[0]
    return os.path.join(parent, f"{stem}_output")


def export_screenshots(
    video_path: str,
    output_dir: str,
    start_frame: int,
    interval_sec: float,
    *,
    stop_event: threading.Event | None = None,
    on_saved: Callable[[int, str, int], None] | None = None,
) -> int:
    """从视频中按间隔导出截图，返回保存张数。"""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise OSError(f"无法打开视频: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    if fps <= 0:
        fps = 25.0

    os.makedirs(output_dir, exist_ok=True)
    start_time = (start_frame - 1) / fps
    next_capture_time = start_time
    saved = 0
    frame_idx = 0

    while True:
        if stop_event is not None and stop_event.is_set():
            break
        ret, frame = cap.read()
        if not ret:
            break
        frame_idx += 1
        current_time = (frame_idx - 1) / fps

        if frame_idx < start_frame:
            continue
        if current_time + 0.5 / fps < next_capture_time:
            continue

        out_path = os.path.join(output_dir, f"{frame_idx}.png")
        if not cv2.imwrite(out_path, frame):
            cap.release()
            raise OSError(f"无法写入图片: {out_path}")

        saved += 1
        if on_saved is not None:
            on_saved(frame_idx, out_path, saved)
        next_capture_time += interval_sec

    cap.release()
    return saved


class Video2PicApp:
    def __init__(self, video_path: str) -> None:
        self.video_path = os.path.abspath(video_path)
        self.output_dir = output_dir_for_video(self.video_path)

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
        self._export_running = False

        self.win = tk.Tk()
        self.win.title("视频截图工具")
        self.win.geometry("1000x640")
        self.win.minsize(800, 500)
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)

        header = tk.Frame(self.win)
        header.pack(fill=tk.X, padx=8, pady=6)
        self.header_var = tk.StringVar()
        tk.Label(header, textvariable=self.header_var, anchor="w").pack(fill=tk.X)
        self._refresh_header()

        body = tk.Frame(self.win)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        left = tk.Frame(body, bg="#111")
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.video_container = tk.Frame(left, bg="#111")
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

        right = tk.Frame(body, width=180)
        right.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))
        right.pack_propagate(False)

        tk.Label(right, text="起始帧索引", anchor="w").pack(fill=tk.X, pady=(8, 4))
        self.start_frame_var = tk.StringVar(value=str(DEFAULT_START_FRAME))
        tk.Entry(right, textvariable=self.start_frame_var, width=12).pack(
            fill=tk.X, pady=(0, 12)
        )

        tk.Label(right, text="时间间隔", anchor="w").pack(fill=tk.X, pady=(0, 4))
        interval_row = tk.Frame(right)
        interval_row.pack(fill=tk.X, pady=(0, 12))
        self.interval_var = tk.StringVar(value=str(int(DEFAULT_INTERVAL_SECONDS)))
        tk.Entry(interval_row, textvariable=self.interval_var, width=8).pack(
            side=tk.LEFT
        )
        tk.Label(interval_row, text="秒").pack(side=tk.LEFT, padx=(4, 0))

        tk.Label(right, text="输出目录", anchor="w").pack(fill=tk.X, pady=(8, 4))
        self.output_var = tk.StringVar(value=self.output_dir)
        tk.Label(
            right,
            textvariable=self.output_var,
            anchor="w",
            justify=tk.LEFT,
            wraplength=160,
            fg="#444",
        ).pack(fill=tk.X)

        btn_frame = tk.Frame(self.win)
        btn_frame.pack(fill=tk.X, padx=8, pady=(0, 6))
        self.play_btn = tk.Button(
            btn_frame, text="播放", width=8, command=self._toggle_play
        )
        self.play_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.export_btn = tk.Button(
            btn_frame, text="导出截图", width=10, command=self._start_export
        )
        self.export_btn.pack(side=tk.LEFT, padx=(0, 6))

        bottom = tk.Frame(self.win, relief=tk.SUNKEN, bd=1)
        bottom.pack(fill=tk.X, side=tk.BOTTOM, padx=8, pady=(0, 8))
        self.status_var = tk.StringVar()
        tk.Label(
            bottom,
            textvariable=self.status_var,
            anchor="w",
            padx=8,
            pady=4,
        ).pack(fill=tk.X)
        self._update_status()

        self.video_container.bind("<Configure>", self._on_container_resize)
        self._show_frame(0)
        self._worker = threading.Thread(target=self._play_loop, daemon=True)
        self._worker.start()

    def _refresh_header(self) -> None:
        name = os.path.basename(self.video_path)
        total = self.total_frames if self.total_frames > 0 else "?"
        fps_s = f"{self.fps:g}" if self.fps == int(self.fps) else f"{self.fps:.2f}"
        self.header_var.set(
            f"视频: {name}  |  帧率: {fps_s} fps  |  总帧数: {total}"
        )

    def _update_status(self, extra: str = "") -> None:
        frame_no = self._current_frame + 1
        base = f"当前帧: {frame_no}"
        if self.total_frames > 0:
            base += f" / {self.total_frames}"
        if extra:
            base += f"  |  {extra}"
        self.status_var.set(base)

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

    def _on_close(self) -> None:
        self._stop.set()
        self._playing.set()
        self.cap.release()
        self.win.destroy()

    def _toggle_play(self) -> None:
        if self._playing.is_set():
            self._playing.clear()
            self.play_btn.config(text="播放")
        else:
            self._playing.set()
            self.play_btn.config(text="暂停")

    def _on_container_resize(self, _event: tk.Event | None = None) -> None:
        self._show_frame(self._current_frame)

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

    def _show_frame(self, index: int) -> None:
        ret, frame = self._read_frame_at(index)
        if not ret or frame is None:
            return

        def update() -> None:
            self._photo = self._bgr_to_photo(frame)
            self.video_label.configure(image=self._photo)
            self.frame_idx_label.config(text=str(self._current_frame + 1))
            self._update_status()

        if threading.current_thread() is threading.main_thread():
            update()
        else:
            self.win.after(0, update)

    def _wait_until_playing(self) -> bool:
        while not self._playing.is_set():
            if self._stop.is_set():
                return False
            time.sleep(0.03)
        return True

    def _play_loop(self) -> None:
        while not self._stop.is_set():
            if not self._wait_until_playing():
                break

            next_index = self._current_frame + 1
            if self.total_frames > 0 and next_index >= self.total_frames:
                self._playing.clear()
                self.win.after(0, lambda: self.play_btn.config(text="播放"))
                self.win.after(0, lambda: self._update_status("播放完毕"))
                time.sleep(0.1)
                continue

            ret, frame = self._read_frame_at(next_index)
            if not ret or frame is None:
                self._playing.clear()
                self.win.after(0, lambda: self.play_btn.config(text="播放"))
                continue

            def update() -> None:
                self._photo = self._bgr_to_photo(frame)
                self.video_label.configure(image=self._photo)
                self.frame_idx_label.config(text=str(self._current_frame + 1))
                self._update_status()

            self.win.after(0, update)

            delay = max(0.001, 1.0 / self.fps)
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                if self._stop.is_set():
                    return
                if not self._playing.is_set():
                    break
                time.sleep(0.005)

    def _start_export(self) -> None:
        if self._export_running:
            return
        start_frame = self._get_start_frame()
        interval = self._get_interval_seconds()
        self._export_running = True
        self.export_btn.config(state=tk.DISABLED)
        self._playing.clear()
        self.play_btn.config(text="播放")
        threading.Thread(
            target=self._export_worker,
            args=(start_frame, interval),
            daemon=True,
        ).start()

    def _export_worker(self, start_frame: int, interval: float) -> None:
        saved = 0
        try:
            def on_saved(frame_idx: int, _path: str, count: int) -> None:
                self.win.after(
                    0,
                    lambda: self._update_status(
                        f"已导出 {count} 张，最近: {frame_idx}.png"
                    ),
                )

            saved = export_screenshots(
                self.video_path,
                self.output_dir,
                start_frame,
                interval,
                stop_event=self._stop,
                on_saved=on_saved,
            )
            msg = f"导出完成，共 {saved} 张\n目录:\n{self.output_dir}"
        except OSError as e:
            msg = f"导出失败: {e}"
        self.win.after(0, lambda: self._export_done(msg, saved))

    def _export_done(self, msg: str, saved: int) -> None:
        self._export_running = False
        self.export_btn.config(state=tk.NORMAL)
        self._update_status(f"导出完成，共 {saved} 张")
        if saved > 0:
            messagebox.showinfo("导出截图", msg, parent=self.win)
        else:
            messagebox.showwarning("导出截图", msg + "\n\n未生成任何图片。", parent=self.win)

    def run(self) -> None:
        self.win.mainloop()


def main() -> int:
    parser = argparse.ArgumentParser(description="视频按时间间隔导出截图")
    parser.add_argument("video", help="视频文件路径")
    args = parser.parse_args()

    video_path = os.path.abspath(args.video)
    if not os.path.isfile(video_path):
        print(f"错误: 视频文件不存在: {video_path}", file=sys.stderr)
        return 1

    try:
        app = Video2PicApp(video_path)
    except ValueError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
