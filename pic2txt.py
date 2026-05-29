#!/usr/bin/env python3
"""从目录中的 PNG 二维码图片还原文件。"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

try:
    import cv2
except ImportError as e:
    print(
        "缺少依赖，请先安装: pip install opencv-python",
        file=sys.stderr,
    )
    raise SystemExit(1) from e

from file2qrc_player import LAST_CHUNK_INDEX

PAYLOAD_SEP = b"%%"
RESULTS_DIR_NAME = "results"


def collect_png_files(pic_path: str) -> list[str]:
    pic_path = os.path.abspath(pic_path)
    if not os.path.isdir(pic_path):
        raise NotADirectoryError(f"不是有效目录: {pic_path}")

    results_abs = os.path.join(pic_path, RESULTS_DIR_NAME)
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(pic_path):
        if os.path.abspath(dirpath) == results_abs:
            dirnames.clear()
            continue
        for name in sorted(filenames):
            if name.lower().endswith(".png"):
                files.append(os.path.join(dirpath, name))
    return files


def qr_decoded_to_bytes(data: str) -> bytes:
    """将 OpenCV 解码结果还原为原始字节载荷。"""
    try:
        return data.encode("latin-1")
    except UnicodeEncodeError:
        # 含 UTF-8 文本时 OpenCV 返回 Unicode，需按 utf-8 还原
        return data.encode("utf-8")


def decode_qr_payload(image_path: str) -> bytes | None:
    img = cv2.imread(image_path)
    if img is None:
        return None

    detector = cv2.QRCodeDetector()
    data, _, _ = detector.detectAndDecode(img)
    if not data:
        ok, decoded_list, _, _ = detector.detectAndDecodeMulti(img)
        if ok and decoded_list:
            for item in decoded_list:
                if item:
                    data = item
                    break

    if not data:
        return None
    return qr_decoded_to_bytes(data)


def parse_payload(data: bytes) -> tuple[str, int, bytes] | None:
    parts = data.split(PAYLOAD_SEP, 2)
    if len(parts) != 3:
        return None
    rel_path_b, index_b, chunk = parts
    try:
        rel_path = rel_path_b.decode("ascii")
        index = int(index_b.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None
    return rel_path, index, chunk


def safe_output_path(results_dir: str, rel_path: str) -> str:
    rel_path = rel_path.replace("\\", "/").lstrip("/")
    parts = [p for p in rel_path.split("/") if p and p != "."]
    if ".." in parts:
        raise ValueError(f"非法相对路径: {rel_path}")
    return os.path.join(results_dir, *parts) if parts else results_dir


def assemble_chunks(chunks: dict[int, bytes]) -> bytes | None:
    if LAST_CHUNK_INDEX not in chunks:
        return None
    ordered = sorted(chunks)
    return b"".join(chunks[i] for i in ordered)


def process_directory(pic_path: str) -> tuple[int, int, int]:
    """扫描 PNG、解析二维码并还原文件。返回 (png数, 有效二维码数, 写出文件数)。"""
    pic_path = os.path.abspath(pic_path)
    png_files = collect_png_files(pic_path)
    results_dir = os.path.join(pic_path, RESULTS_DIR_NAME)
    os.makedirs(results_dir, exist_ok=True)

    file_chunks: dict[str, dict[int, bytes]] = defaultdict(dict)
    qr_count = 0

    for png_path in png_files:
        payload = decode_qr_payload(png_path)
        if payload is None:
            continue
        parsed = parse_payload(payload)
        if parsed is None:
            continue
        rel_path, index, chunk = parsed
        file_chunks[rel_path][index] = chunk
        qr_count += 1

    written = 0
    for rel_path, chunks in sorted(file_chunks.items()):
        data = assemble_chunks(chunks)
        if data is None:
            continue
        out_path = safe_output_path(results_dir, rel_path)
        os.makedirs(os.path.dirname(out_path) or results_dir, exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(data)
        written += 1

    return len(png_files), qr_count, written


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从 PNG 二维码图片还原文件到 results/ 目录",
    )
    parser.add_argument(
        "pic_path",
        help="包含 PNG 二维码图片的目录路径",
    )
    args = parser.parse_args()

    pic_path = os.path.abspath(args.pic_path)
    if not os.path.isdir(pic_path):
        print(f"错误: 目录不存在: {pic_path}", file=sys.stderr)
        return 1

    try:
        png_count, qr_count, written = process_directory(pic_path)
    except OSError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1

    results_dir = os.path.join(pic_path, RESULTS_DIR_NAME)
    print(
        f"扫描 PNG: {png_count} 张  |  识别二维码: {qr_count} 个  |  "
        f"还原文件: {written} 个  |  输出: {results_dir}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
