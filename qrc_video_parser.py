#!/usr/bin/env python3
"""视频二维码解析播放器：播放视频、逐帧解析二维码并还原文件。"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import tkinter as tk
from collections import defaultdict
from tkinter import messagebox, scrolledtext
from typing import Callable

try:
    import cv2
    from PIL import Image, ImageTk
except ImportError as e:
    print(
        "缺少依赖，请先安装: pip install opencv-python Pillow",
        file=sys.stderr,
    )
    raise SystemExit(1) from e

from file2qrc_player import LAST_CHUNK_INDEX
from pic2txt import (
    RESULTS_DIR_NAME,
    assemble_chunks,
    decode_qr_from_bgr,
    parse_payload,
    safe_output_path,
)

DEFAULT_START_FRAME = 1
DEFAULT_INTERVAL_SECONDS = 1.0
SKIP_AFTER_NEW_INDEX = 10
NO_QR_PLACEHOLDER = "(未识别到二维码)"


def format_qr_summary(payload: bytes) -> str:
    """将 payload 格式化为可读摘要。"""
    parsed = parse_payload(payload)
    if parsed is None:
        try:
            return payload.decode("latin-1")
        except UnicodeDecodeError:
            return repr(payload)
    rel_path, index, chunk = parsed
    return f"{rel_path}%%{index}%%  chunk={len(chunk)} bytes"


class VideoChunkAssembler:
    """按 path/index 累积 chunk，支持去重与 index 跳跃检测。"""

    def __init__(
        self,
        on_index_gap: Callable[[str, int, int, int], None] | None = None,
    ) -> None:
        self.file_chunks: dict[str, dict[int, bytes]] = defaultdict(dict)
        self.last_index: dict[str, int] = {}
        self.written_paths: set[str] = set()
        self.qr_count = 0
        self.duplicate_count = 0
        self.accepted_count = 0
        self.gap_alert_count = 0
        self._on_index_gap = on_index_gap

    def reset(self) -> None:
        self.file_chunks = defaultdict(dict)
        self.last_index.clear()
        self.written_paths.clear()
        self.qr_count = 0
        self.duplicate_count = 0
        self.accepted_count = 0
        self.gap_alert_count = 0

    def try_accept(self, payload: bytes, frame_no: int) -> tuple[str, int, bytes] | None:
        """解析 payload；失败或重复 index 返回 None，新 chunk 返回 (path, index, chunk)。"""
        parsed = parse_payload(payload)
        if parsed is None:
            return None

        rel_path, index, chunk = parsed
        self.qr_count += 1

        if index in self.file_chunks[rel_path]:
            self.duplicate_count += 1
            return None

        if rel_path in self.last_index:
            prev = self.last_index[rel_path]
            if index != LAST_CHUNK_INDEX and index - prev > 1:
                if self._on_index_gap is not None:
                    self._on_index_gap(rel_path, prev, index, frame_no)
                self.gap_alert_count += 1

        self.file_chunks[rel_path][index] = chunk
        self.last_index[rel_path] = index
        self.accepted_count += 1
        return rel_path, index, chunk

    def pending_paths(self) -> list[str]:
        return [p for p in self.file_chunks if p not in self.written_paths]


class QrcVideoParserApp:
    def __init__(self, video_path: str) -> None:
        self.video_path = os.path.abspath(video_path)
        self.results_dir = os.path.join(
            os.path.dirname(self.video_path), RESULTS_DIR_NAME
        )

        self.cap = cv2.VideoCapture(self.video_path)
        if not self.cap.isOpened():
            raise ValueError(f"无法打开视频: {self.video_path}")

        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 25.0
        if self.fps <= 0:
            self.fps = 25.0
        self.total_frames = max(0, int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT)))

        self._stop = threading.Event()
        self._playing = threading.Event()
        self._parsing = threading.Event()
        self._seek_lock = threading.Lock()
        self._current_frame = 0
        self._photo: ImageTk.PhotoImage | None = None
        self._updating_scale = False
        self._selected_play_armed = False
        self._assembler = VideoChunkAssembler(on_index_gap=self._show_index_gap_warning)
        self._parse_ui_pending = False
        self._resize_after_id: str | None = None

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

        tk.Label(settings_lf, text="开始帧索引（仅播放）", anchor="w").pack(
            fill=tk.X, pady=(4, 4)
        )
        self.start_frame_var = tk.StringVar(value=str(DEFAULT_START_FRAME))
        tk.Entry(settings_lf, textvariable=self.start_frame_var, width=12).pack(
            fill=tk.X, pady=(0, 12)
        )

        tk.Label(settings_lf, text="间隔时间（仅播放）", anchor="w").pack(
            fill=tk.X, pady=(0, 4)
        )
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
        self.parse_btn = tk.Button(
            ctrl, text="解析视频", width=10, command=self._start_video_parse
        )
        self.parse_btn.pack(side=tk.LEFT, padx=(0, 8))
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

    def _show_index_gap_warning(
        self, rel_path: str, prev: int, index: int, frame_no: int
    ) -> None:
        done = threading.Event()

        def show() -> None:
            messagebox.showwarning(
                "索引跳跃",
                f"文件: {rel_path}\n帧: {frame_no}\n"
                f"上一 index: {prev}，当前 index: {index}，差值: {index - prev}\n\n"
                f"可能存在丢帧，点击确定继续解析。",
                parent=self.win,
            )
            done.set()

        self.win.after(0, show)
        done.wait()

    def _write_assembled_file(self, rel_path: str, chunks: dict[int, bytes]) -> bool:
        data = assemble_chunks(chunks)
        if data is None:
            return False
        out_path = safe_output_path(self.results_dir, rel_path)
        os.makedirs(os.path.dirname(out_path) or self.results_dir, exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(data)
        self._assembler.written_paths.add(rel_path)
        return True

    def _try_write_path(self, rel_path: str) -> bool:
        if rel_path in self._assembler.written_paths:
            return False
        chunks = self._assembler.file_chunks.get(rel_path)
        if not chunks:
            return False
        return self._write_assembled_file(rel_path, chunks)

    def _write_all_pending(self) -> int:
        written = 0
        for rel_path in list(self._assembler.pending_paths()):
            if self._try_write_path(rel_path):
                written += 1
        return written

    def _start_video_parse(self) -> None:
        if self._parsing.is_set():
            return
        self._playing.clear()
        self.play_btn.config(text="播放")
        self._parsing.set()
        self.parse_btn.config(state=tk.DISABLED)
        self._assembler.reset()
        self._parse_ui_pending = False
        os.makedirs(self.results_dir, exist_ok=True)
        threading.Thread(target=self._parse_video_loop, daemon=True).start()

    def _skip_parse_frames(
        self,
        count: int,
        frame_index: int,
        total: int | None,
        scanned: int,
    ) -> tuple[int, int, bool]:
        """跳过若干帧（不解码），返回 (新帧索引, 新扫描计数, 是否继续)。"""
        for _ in range(count):
            if self._stop.is_set():
                return frame_index, scanned, False
            if total is not None and frame_index + 1 >= total:
                return frame_index + 1, scanned, True
            with self._seek_lock:
                ret, _ = self.cap.read()
            if not ret:
                return frame_index, scanned, False
            frame_index += 1
            scanned += 1
        return frame_index, scanned, True

    def _parse_video_loop(self) -> None:
        """逐帧解析：新 index 接受后跳 10 帧再逐帧搜索，UI 仅节流刷新进度文字。"""
        total = self.total_frames if self.total_frames > 0 else None
        scanned = 0
        last_ui_time = 0.0
        last_summary = ""
        ui_interval = 0.15

        try:
            with self._seek_lock:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            frame_index = 0
            while not self._stop.is_set():
                if total is not None and frame_index >= total:
                    break

                with self._seek_lock:
                    ret, frame = self.cap.read()
                scanned += 1
                if not ret or frame is None:
                    break

                self._current_frame = frame_index
                frame_no = frame_index + 1

                payload = decode_qr_from_bgr(frame)
                accepted: tuple[str, int, bytes] | None = None
                if payload is not None:
                    accepted = self._assembler.try_accept(payload, frame_no)
                    last_summary = format_qr_summary(payload)
                    if accepted is not None:
                        rel_path, index, _chunk = accepted
                        if index == LAST_CHUNK_INDEX:
                            self._try_write_path(rel_path)
                        frame_index, scanned, cont = self._skip_parse_frames(
                            SKIP_AFTER_NEW_INDEX, frame_index, total, scanned
                        )
                        if not cont:
                            break

                now = time.monotonic()
                if frame_index == 0 or now - last_ui_time >= ui_interval:
                    last_ui_time = now
                    self._update_parse_progress(frame_no, total, last_summary)

                frame_index += 1

            self._write_all_pending()
            asm = self._assembler
            msg = (
                f"扫描帧数: {scanned}\n"
                f"有效二维码: {asm.qr_count}\n"
                f"新 chunk: {asm.accepted_count}\n"
                f"跳过重复: {asm.duplicate_count}\n"
                f"索引跳跃告警: {asm.gap_alert_count}\n"
                f"还原文件: {len(asm.written_paths)}\n"
                f"输出目录:\n{self.results_dir}"
            )
            self.win.after(
                0,
                lambda: messagebox.showinfo("解析完成", msg, parent=self.win),
            )
        finally:
            def done() -> None:
                self._parsing.clear()
                self.parse_btn.config(state=tk.NORMAL)
                self._update_frame_count_label()

            self.win.after(0, done)

    def _update_parse_progress(
        self,
        frame_no: int,
        total: int | None,
        summary: str = "",
    ) -> None:
        if self._parse_ui_pending:
            return
        self._parse_ui_pending = True

        asm = self._assembler
        progress_text = (
            f"解析中 帧 {frame_no}"
            + (f"/{total}" if total else "")
            + f"  |  有效QR: {asm.qr_count}"
            + f"  |  新chunk: {asm.accepted_count}"
            + f"  |  已写出: {len(asm.written_paths)}"
        )

        def update() -> None:
            self._parse_ui_pending = False
            self.frame_idx_label.config(text=str(frame_no))
            self._updating_scale = True
            try:
                self.progress_var.set(frame_no)
            finally:
                self._updating_scale = False
            self.frame_count_var.set(progress_text)
            if summary:
                self._set_text_panel(summary)

        self.win.after(0, update)

    def _on_close(self) -> None:
        self._stop.set()
        self._playing.set()
        self.cap.release()
        self.win.destroy()

    def _toggle_play(self) -> None:
        if self._parsing.is_set():
            return
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
        if self._parsing.is_set():
            return
        if self._resize_after_id is not None:
            self.win.after_cancel(self._resize_after_id)
        self._resize_after_id = self.win.after(100, self._refresh_video_display)

    def _refresh_video_display(self) -> None:
        self._resize_after_id = None
        if self._parsing.is_set():
            return
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

    def _decode_qr_text_from_frame(self, frame_bgr) -> str:
        payload = decode_qr_from_bgr(frame_bgr)
        if payload is None:
            return ""
        return format_qr_summary(payload)

    def _display_at(self, index: int, *, decode: bool = True) -> str:
        ret, frame = self._read_frame_at(index)
        if not ret or frame is None:
            return ""

        qr_text = self._decode_qr_text_from_frame(frame) if decode else ""

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
            if self._parsing.is_set():
                time.sleep(0.05)
                continue

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

        qr_text = self._decode_qr_text_from_frame(frame)

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

        qr_text = self._decode_qr_text_from_frame(frame)

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
