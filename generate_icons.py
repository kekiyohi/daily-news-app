#!/usr/bin/env python3
"""
スマホ用アプリアイコン (apple-touch-icon.png, icon-192.png, icon-512.png) と
PWA用 manifest.json を標準ライブラリのみで生成するスクリプト
"""
import json
import math
import os
import struct
import zlib


def create_app_icon_png(filepath: str, size: int = 192) -> None:
    """モダンなグラデーション＋AIスパークル/ニュースカード風のPNGアイコンを生成"""
    raw_rows = []
    center = size / 2.0
    radius = size * 0.45
    corner_radius = size * 0.22

    for y in range(size):
        row = bytearray([0])  # PNG filter type 0 (None)
        ny = y / float(size)
        for x in range(size):
            nx = x / float(size)

            # 角丸スクエア判定
            dx = max(abs(x - center) - (size / 2.0 - corner_radius), 0.0)
            dy = max(abs(y - center) - (size / 2.0 - corner_radius), 0.0)
            dist_corner = math.hypot(dx, dy)

            if dist_corner > corner_radius:
                # 透明外側
                row.extend((0, 0, 0, 0))
                continue

            # アンチエイリアス
            alpha = 255
            if dist_corner > corner_radius - 1.2:
                alpha = int(max(0.0, min(1.0, corner_radius - dist_corner)) * 255)

            # 背景：ディープインディゴ〜シアンブルーのグラデーション
            r = int(15 + 35 * nx + 20 * (1 - ny))
            g = int(23 + 75 * ny + 40 * nx)
            b = int(65 + 155 * (0.5 * nx + 0.5 * ny))

            # 中央のAIニュースカード＆スパークルモチーフ
            # 1) ニュースカード枠 (白い半透明の角丸カード)
            in_card = (0.20 <= nx <= 0.80) and (0.24 <= ny <= 0.78)
            if in_card:
                cdx = max(abs(nx - 0.50) - 0.24, 0.0)
                cdy = max(abs(ny - 0.51) - 0.21, 0.0)
                if math.hypot(cdx, cdy) <= 0.06:
                    # カード背景 (ダークガラス風)
                    r = int(r * 0.35 + 240 * 0.65)
                    g = int(g * 0.35 + 246 * 0.65)
                    b = int(b * 0.35 + 255 * 0.65)

                    # カード内の見出しバー (ブルーグラデ)
                    if 0.28 <= nx <= 0.62 and 0.33 <= ny <= 0.39:
                        r, g, b = 37, 99, 235
                    # 要約テキストの3本線
                    elif 0.28 <= nx <= 0.72 and 0.45 <= ny <= 0.49:
                        r, g, b = 71, 85, 105
                    elif 0.28 <= nx <= 0.72 and 0.54 <= ny <= 0.58:
                        r, g, b = 100, 116, 139
                    elif 0.28 <= nx <= 0.56 and 0.63 <= ny <= 0.67:
                        r, g, b = 148, 163, 184

            # 2) 右上のAIスパークル (4光星・ゴールド〜シアン)
            sx, sy = nx - 0.70, ny - 0.28
            sparkle_dist = (abs(sx) ** 0.65 + abs(sy) ** 0.65)
            if sparkle_dist < 0.16:
                glow = max(0.0, 1.0 - sparkle_dist / 0.16)
                r = int(r * (1 - glow) + 251 * glow)
                g = int(g * (1 - glow) + 191 * glow)
                b = int(b * (1 - glow) + 36 * glow)

            row.extend((max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)), alpha))
        raw_rows.append(bytes(row))

    raw_data = b"".join(raw_rows)

    def make_chunk(chunk_type: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    idat = zlib.compress(raw_data, level=9)
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        + make_chunk(b"IHDR", ihdr)
        + make_chunk(b"IDAT", idat)
        + make_chunk(b"IEND", b"")
    )
    with open(filepath, "wb") as f:
        f.write(png_bytes)


def create_manifest(base_dir: str) -> None:
    manifest = {
        "name": "最新AI系ニュースまとめ",
        "short_name": "AIニュース",
        "description": "毎朝自動更新される自分専用のAI要約ニュースまとめアプリ",
        "start_url": "./index.html",
        "display": "standalone",
        "background_color": "#f8fafc",
        "theme_color": "#ffffff",
        "orientation": "portrait",
        "icons": [
            {
                "src": "icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable"
            },
            {
                "src": "icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable"
            }
        ]
    }
    manifest_path = os.path.join(base_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    base_dir = os.path.dirname(os.path.abspath(__file__))
    create_app_icon_png(os.path.join(base_dir, "apple-touch-icon.png"), 180)
    create_app_icon_png(os.path.join(base_dir, "icon-192.png"), 192)
    create_app_icon_png(os.path.join(base_dir, "icon-512.png"), 512)
    create_manifest(base_dir)
    print("Generated apple-touch-icon.png, icon-192.png, icon-512.png, and manifest.json")
