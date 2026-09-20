"""验证关键环节：给裸 FLAC 补标签 + 内嵌封面，看项目管线能否正确读取。

网易云返回的 lossless FLAC **没有任何标签**（Vorbis comment 为空），也没有内嵌封面。
而 320k MP3 是带完整标签的。所以走无损路线就必须自己写元数据。

这个脚本验证"自己写"这条路走得通：写完之后项目现有的 read_pc_track
能不能读出标题/艺人/专辑，以及能不能识别到内嵌封面。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from mutagen.flac import FLAC, Picture

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

DL = Path(str(Path(os.environ.get("TEMP", ".")) / "ncm-dl"))
SRC = DL / "lossless-2.flac"      # STAY 的裸 FLAC
COVER = DL / "cover-test.jpg"


def main() -> int:
    if not SRC.is_file() or not COVER.is_file():
        print(f"缺少测试文件：{SRC} / {COVER}")
        return 1

    print("=== 写入前 ===")
    audio = FLAC(SRC)
    print(f"  标签: {dict(audio) or '（空）'}")
    print(f"  图片数: {len(audio.pictures)}")

    # 1) 文本标签（字段来自 song_detail）
    audio["TITLE"] = "STAY"
    audio["ARTIST"] = "The Kid LAROI/Justin Bieber"
    audio["ALBUM"] = "STAY"
    audio["TRACKNUMBER"] = "1"
    audio["DATE"] = "2021"

    # 2) 内嵌封面（FLAC 用 PICTURE 元数据块）
    pic = Picture()
    pic.type = 3                    # Cover (front)
    pic.mime = "image/jpeg"
    pic.data = COVER.read_bytes()
    pic.width = pic.height = 600
    pic.depth = 24
    audio.clear_pictures()
    audio.add_picture(pic)
    audio.save()

    print()
    print("=== 写入后 ===")
    check = FLAC(SRC)
    print(f"  标签: {dict(check)}")
    first = check.pictures[0]
    print(f"  图片数: {len(check.pictures)}  "
          f"({first.width}x{first.height}, {len(first.data):,} 字节)")

    print()
    print("=== 项目管线读取结果 ===")
    from ipod_cli.mediafile import read_pc_track

    track = read_pc_track(SRC)
    print(f"  标题    : {track.title!r}")
    print(f"  艺人    : {track.artist!r}")
    print(f"  专辑    : {track.album!r}")
    print(f"  时长    : {track.duration_ms}ms  码率: {track.bitrate}kbps  "
          f"采样率: {track.sample_rate}")
    print(f"  内嵌封面: {track.has_embedded_artwork}")
    print(f"  需要转码: {track.needs_transcode}")

    ok = (
        track.title == "STAY"
        and track.artist == "The Kid LAROI/Justin Bieber"
        and track.has_embedded_artwork
    )
    print()
    print("=" * 60)
    print("结论：链路打通 ✅" if ok else "结论：链路有问题 ❌")
    print("=" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
