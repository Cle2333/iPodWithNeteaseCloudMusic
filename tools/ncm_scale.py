"""P0 第二步：算出"全库有多少首、下下来多大"。

P0 第一问（能不能拿到链接）已经答了：180/180，100%。
这一问是**规模**——决定用 lossless 还是 exhigh。

  1. 拉全部歌单的曲目（含分页），按歌曲 ID 去重
  2. 报告唯一歌曲数、跨歌单重复度
  3. 按实测单曲大小投影总占用
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ncm_p0 import api_get, load_state, save_state  # noqa: E402

# 实测单曲大小中位数（MB），来自上一轮逐档测试
SIZE_PER_SONG_MB = {
    "exhigh": 7.0,      # 320k MP3
    "lossless": 25.0,   # FLAC 无损
}


def fetch_playlist_songs(playlist_id: int, cookie: str, total: int) -> list[dict]:
    """取一个歌单的全部曲目（自动翻页 + 限速重试）。

    接口单次上限 1000。批量拉 40 个歌单时会被限速，表现为返回 code=200
    但 songs 为空——**不能当成"这个歌单是空的"**，必须重试。
    """
    songs: list[dict] = []
    offset = 0
    while offset < max(total, 1):
        batch: list[dict] = []
        for attempt in range(4):
            resp = api_get("/playlist/track/all", {
                "id": playlist_id, "cookie": cookie,
                "limit": 1000, "offset": offset,
                "timestamp": int(time.time() * 1000),
            })
            batch = resp.get("songs") or []
            if batch:
                break
            # 空结果 = 大概率被限速，退避后重试
            time.sleep(0.8 * (attempt + 1))
        if not batch:
            break
        songs.extend(batch)
        offset += len(batch)
        if len(batch) < 1000:
            break
        time.sleep(0.4)
    return songs


def main() -> int:
    state = load_state()
    cookie, uid = state.get("cookie"), state.get("uid")
    if not (cookie and uid):
        print("❌ 还没登录")
        return 1

    playlists = state.get("playlists") or []
    print(f"=== 拉取 {len(playlists)} 个歌单的全部曲目 ===")

    # 歌单 → 曲目 ID 集合；歌曲 ID → 元数据
    membership: dict[str, set[int]] = {}
    meta: dict[int, dict] = {}

    for index, p in enumerate(playlists, 1):
        songs = fetch_playlist_songs(p["id"], cookie, p.get("trackCount") or 0)
        ids = {s["id"] for s in songs if s.get("id")}
        membership[p["name"]] = ids
        for s in songs:
            if s.get("id"):
                meta.setdefault(s["id"], s)
        print(f"  [{index:>2}/{len(playlists)}] {p['name'][:34]:<36} "
              f"{len(songs):>5} 首")

    # 加上我喜欢的音乐
    liked = set(state.get("liked_ids") or [])
    if liked:
        membership["我喜欢的音乐"] = liked
        membership_ids = liked - set(meta)
        if membership_ids:
            from ncm_p0 import _song_meta_batch

            for s in _song_meta_batch(list(membership_ids), cookie):
                meta.setdefault(s["id"], s)

    unique = set(meta)
    total_entries = sum(len(v) for v in membership.values())

    print()
    print("=" * 66)
    print(f"歌单条目总数（含跨歌单重复）  {total_entries:>7}")
    print(f"唯一歌曲数                    {len(unique):>7}")
    print(f"重复率                        "
          f"{(1 - len(unique) / max(total_entries, 1)) * 100:>6.1f}%")
    print()
    print("按实测单曲大小中位数投影：")
    for level, mb in SIZE_PER_SONG_MB.items():
        total_gb = len(unique) * mb / 1024
        label = {"exhigh": "320k MP3", "lossless": "FLAC 无损"}[level]
        print(f"  {label:<12} {mb:>5.1f} MB/首  →  共约 {total_gb:>7.1f} GB")
    print()
    print("（你的 iPod 剩余约 78.5 GB）")

    # 存下来
    state["unique_song_ids"] = sorted(unique)
    state["membership_sizes"] = {k: len(v) for k, v in membership.items()}
    state["unique_count"] = len(unique)
    save_state(state)
    print()
    print(f"唯一歌曲 ID 已存入 {'.ncm/p0-state.json'}（供深度探测复用）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
