"""P0 冒烟验证：网易云歌单能不能拿到可下载的链接。

回答一个问题：**你歌单里有百分之多少的歌，真的能下下来？**

流程：
    1. 扫码登录（二维码存成 PNG，人工扫）
    2. 拉取账号 + 歌单列表
    3. 逐首调 song_url_v1，统计可下载率与音质分布
    4. 结果存 JSON（可中断续跑）

用法：
    uv run python tools/ncm_p0.py login          # 第一步：出二维码
    uv run python tools/ncm_p0.py wait           # 第二步：等扫码
    uv run python tools/ncm_p0.py playlists      # 第三步：看歌单
    uv run python tools/ncm_p0.py probe --sample 60   # 第四步：抽查可下载率
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_BASE = "http://127.0.0.1:4000"
STATE_DIR = Path(__file__).resolve().parents[1] / ".ncm"
STATE_FILE = STATE_DIR / "p0-state.json"

# 探测档位，从高到低。**只列"能上 iPod"的档位**：
#   jymaster/hires 是 24bit 以上的高解析（iPod Classic 播不了），
#   jyeffect 是空间音频（50-116MB/首，iPod 完全不支持），
#   所以都不在候选里——请求它们只会拿到没用的巨大文件。
QUALITY_LADDER = ["lossless", "exhigh", "standard"]

# 网易云返回的 level 字符串 → 给人看的说明
LEVEL_LABEL = {
    "standard": "标准",
    "higher": "较高",
    "exhigh": "极高",
    "lossless": "无损",
    "hires": "Hi-Res",
    "jyeffect": "高清臻音",
    "sky": "沉浸环绕",
    "jymaster": "超清母带",
}

# song_url_v1 返回的 code=200 但 url 为 null 时，这些 freeTrialInfo 之类字段说明原因
PRIVILEGE_HINT = {
    0: "无版权/不可播",
    8: "需付费专辑",
    10: "需付费专辑",
    16: "会员专享",
    17: "会员专享",
    18: "会员专享",
}


def api_get(path: str, params: dict | None = None, timeout: int = 30) -> dict:
    """GET 本地 API 服务。"""
    url = f"{API_BASE}{path}"
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        url += "?" + urllib.parse.urlencode(clean)
    req = urllib.request.Request(url, headers={"User-Agent": "ipod-cli-p0/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        return {"_http_error": exc.code, "_body": body}
    except Exception as exc:
        return {"_error": f"{type(exc).__name__}: {exc}"}


def load_state() -> dict:
    if STATE_FILE.is_file():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def require_service() -> bool:
    probe = api_get("/login/qr/key", {"timestamp": int(time.time() * 1000)})
    if "_error" in probe or "_http_error" in probe:
        print(f"❌ 连不上 API 服务（{API_BASE}）")
        print(f"   {probe.get('_error') or probe.get('_body')}")
        print("   先启动：cd source/repos/api-enhanced && node app.js")
        return False
    return True


# ──────────────────────────────────────────────────────────────────────
# 1. 登录
# ──────────────────────────────────────────────────────────────────────

def cmd_login() -> int:
    if not require_service():
        return 1

    key_resp = api_get("/login/qr/key", {"timestamp": int(time.time() * 1000)})
    unikey = (key_resp.get("data") or {}).get("unikey")
    if not unikey:
        print("❌ 拿不到 unikey：", json.dumps(key_resp, ensure_ascii=False)[:300])
        return 1

    create_resp = api_get(
        "/login/qr/create",
        {"key": unikey, "qrimg": "true", "timestamp": int(time.time() * 1000)},
    )
    data = create_resp.get("data") or {}
    qrimg = data.get("qrimg") or ""
    qrurl = data.get("qrurl") or ""
    if not qrimg.startswith("data:image"):
        print("❌ 拿不到二维码图：", json.dumps(create_resp, ensure_ascii=False)[:300])
        return 1

    # data URL → PNG 文件，给用户扫
    _, _, b64 = qrimg.partition(",")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    png = STATE_DIR / "login-qrcode.png"
    png.write_bytes(base64.b64decode(b64))

    state = load_state()
    state["unikey"] = unikey
    state["qrurl"] = qrurl
    state["qr_png"] = str(png)
    state.pop("cookie", None)
    save_state(state)

    print(f"二维码已保存：{png}")
    print(f"扫码地址（备用）：{qrurl}")
    print()
    print("用网易云音乐 App 扫上面的二维码。扫完运行：")
    print("    uv run python tools/ncm_p0.py wait")
    return 0


def cmd_wait(*, max_seconds: int = 180) -> int:
    state = load_state()
    unikey = state.get("unikey")
    if not unikey:
        print("❌ 还没有二维码，先跑 login")
        return 1

    print("等待扫码…（在手机上打开网易云音乐 App 扫二维码）")
    deadline = time.time() + max_seconds
    last_code = None
    while time.time() < deadline:
        resp = api_get("/login/qr/check", {"key": unikey, "timestamp": int(time.time() * 1000)})
        code = resp.get("code")
        if code != last_code:
            label = {
                800: "二维码已过期 —— 重新跑 login",
                801: "等待扫码…",
                802: "已扫描，请在手机上确认登录",
                803: "授权成功 ✅",
            }.get(code, f"未知状态 {code}")
            print(f"  [{code}] {label}")
            last_code = code
        if code == 803:
            cookie = resp.get("cookie") or ""
            if not cookie:
                print("❌ 授权成功但没拿到 cookie：", json.dumps(resp, ensure_ascii=False)[:300])
                return 1
            state["cookie"] = cookie
            save_state(state)

            # 顺便把账号信息取出来
            status = api_get("/login/status", {"cookie": cookie, "timestamp": int(time.time() * 1000)})
            acc = ((status.get("data") or {}).get("account") or {})
            if acc:
                state["uid"] = acc.get("id")
                state["nickname"] = acc.get("userName")
                state["vip"] = acc.get("vipType")
                save_state(state)
                print(f"\n账号：{acc.get('userName')}  uid={acc.get('id')}  vipType={acc.get('vipType')}")
                vip = acc.get("vipType") or 0
                print(f"会员状态：{'VIP' if vip > 0 else '普通用户（无损/Hi-Res 可能拿不到）'}")
            print("\n下一步：uv run python tools/ncm_p0.py playlists")
            return 0
        if code == 800:
            return 1
        time.sleep(2)
    print("⏱  等待超时。重新跑 login 换一张二维码。")
    return 1


# ──────────────────────────────────────────────────────────────────────
# 2. 歌单
# ──────────────────────────────────────────────────────────────────────

def cmd_playlists() -> int:
    state = load_state()
    cookie, uid = state.get("cookie"), state.get("uid")
    if not (cookie and uid):
        print("❌ 还没登录，先跑 login / wait")
        return 1

    # 自己的歌单 + 收藏的歌单
    resp = api_get("/user/playlist", {
        "uid": uid, "cookie": cookie, "limit": 100,
        "timestamp": int(time.time() * 1000),
    })
    playlists = resp.get("playlist") or []
    if not playlists:
        print("❌ 没拿到歌单：", json.dumps(resp, ensure_ascii=False)[:400])
        return 1

    state["playlists"] = [
        {
            "id": p.get("id"),
            "name": p.get("name"),
            "trackCount": p.get("trackCount"),
            "userId": p.get("userId"),
            "creator": ((p.get("creator") or {}).get("nickname")),
            "subscribed": p.get("subscribed"),
        }
        for p in playlists
    ]
    save_state(state)

    mine = uid
    print(f"=== 共 {len(playlists)} 个歌单 ===\n")
    print(f"{'曲目数':>7}  {'类型':<6} 名字")
    print("-" * 66)
    for p in sorted(playlists, key=lambda x: -(x.get("trackCount") or 0)):
        kind = "收藏" if p.get("subscribed") or p.get("userId") != mine else "自建"
        print(f"{p.get('trackCount'):>7}  {kind:<6} {p.get('name')}")
    print()
    print("下一步：uv run python tools/ncm_p0.py probe --sample 60")
    return 0


# ──────────────────────────────────────────────────────────────────────
# 3. 可下载率探测
# ──────────────────────────────────────────────────────────────────────

def _song_url(song_id: int, cookie: str, level: str, unblock: bool) -> dict:
    """取一首歌的下载链接。song_url_v1 的 unblock 会走第三方源兜底。"""
    params = {
        "id": song_id,
        "level": level,
        "cookie": cookie,
        "unblock": "true" if unblock else "false",
        "timestamp": int(time.time() * 1000),
    }
    resp = api_get("/song/url/v1", params)
    items = resp.get("data") or []
    return items[0] if items else {}


def _liked_song_ids(cookie: str, uid: int) -> list[int]:
    resp = api_get("/likelist", {"uid": uid, "cookie": cookie,
                                 "timestamp": int(time.time() * 1000)})
    return list(resp.get("ids") or [])


def _song_meta_batch(ids: list[int], cookie: str, chunk: int = 100) -> list[dict]:
    """批量补歌曲元数据。song_detail 一次能问多首。"""
    out: list[dict] = []
    for start in range(0, len(ids), chunk):
        batch = ids[start:start + chunk]
        resp = api_get("/song/detail", {
            "ids": ",".join(str(i) for i in batch),
            "cookie": cookie,
            "timestamp": int(time.time() * 1000),
        })
        out.extend(resp.get("songs") or [])
    return out


def _probe_songs(
    label: str,
    songs: list[dict],
    cookie: str,
    state: dict,
    *,
    unblock: bool,
) -> tuple[int, int, dict[str, int], list[dict]]:
    """逐首探测可下载性。返回 (总数, 可下载数, 音质分布, 失败列表)。"""
    results = state.setdefault("probe", {})
    total = ok = 0
    levels: dict[str, int] = {}
    failures: list[dict] = []

    print(f"--- {label}：{len(songs)} 首 ---")
    for index, song in enumerate(songs, 1):
        sid = song.get("id")
        key = str(sid)
        if key in results:
            cached = results[key]
            total += 1
            if cached.get("url"):
                ok += 1
                levels[cached.get("level") or "?"] = levels.get(cached.get("level") or "?", 0) + 1
            else:
                failures.append(cached)
            continue

        best: dict = {}
        for level in QUALITY_LADDER:
            item = _song_url(sid, cookie, level, unblock)
            if item.get("url"):
                best = item
                break
            time.sleep(0.05)

        record = {
            "name": song.get("name"),
            # 艺人的 name 可能是显式 null，别直接 join
            "artist": "/".join((a.get("name") or "") for a in (song.get("ar") or [])),
            "url": bool(best.get("url")),
            "level": best.get("level") or "",
            "br": best.get("br") or 0,
            "size": best.get("size") or 0,
            "fee": song.get("fee"),
            "label": label,
        }
        results[key] = record
        total += 1
        if record["url"]:
            ok += 1
            levels[record["level"] or "?"] = levels.get(record["level"] or "?", 0) + 1
        else:
            failures.append(record)

        if index % 10 == 0 or index == len(songs):
            save_state(state)
            print(f"    {index}/{len(songs)}  可下载 {ok}/{total} "
                  f"({100 * ok / max(total, 1):.0f}%)")
    return total, ok, levels, failures


def cmd_probe(
    sample: int = 60,
    per_playlist: int = 2,
    unblock: bool = False,
    liked: bool = False,
    name_filter: str = "",
) -> int:
    state = load_state()
    cookie, uid = state.get("cookie"), state.get("uid")
    if not (cookie and uid):
        print("❌ 还没登录")
        return 1

    playlists = state.get("playlists") or []
    if not playlists:
        print("❌ 先跑 playlists")
        return 1

    # 决定测哪些
    targets: list[tuple[str, list[dict]]] = []

    if liked:
        ids = state.get("liked_ids") or _liked_song_ids(cookie, uid)
        songs = _song_meta_batch(ids, cookie)
        if sample:
            songs = songs[:sample]
        targets.append(("我喜欢的音乐", songs))

    picks = [p for p in playlists if not name_filter or name_filter in (p.get("name") or "")]
    picks = sorted(picks, key=lambda p: -(p.get("trackCount") or 0))[:per_playlist]
    for p in picks:
        detail = api_get("/playlist/track/all", {
            "id": p["id"], "cookie": cookie, "limit": 1000, "offset": 0,
            "timestamp": int(time.time() * 1000),
        })
        songs = detail.get("songs") or []
        if not songs:
            print(f"⚠️  歌单「{p['name']}」取不到曲目：{str(detail)[:160]}")
            continue
        if sample:
            songs = songs[:sample]
        targets.append((f"歌单「{p['name']}」", songs))

    if not targets:
        print("❌ 没有可探测的目标")
        return 1

    print(f"=== 可下载率探测（unblock={'开' if unblock else '关'}）===")
    started = time.time()

    grand_total = grand_ok = 0
    all_levels: dict[str, int] = {}
    all_failures: list[dict] = []

    for label, songs in targets:
        total, ok, levels, failures = _probe_songs(
            label, songs, cookie, state, unblock=unblock
        )
        grand_total += total
        grand_ok += ok
        for level, count in levels.items():
            all_levels[level] = all_levels.get(level, 0) + count
        all_failures.extend(failures)
        save_state(state)

    print()
    print("=" * 66)
    print(f"总结果：{grand_ok}/{grand_total} 首能拿到下载链接"
          f"（{100 * grand_ok / max(grand_total, 1):.0f}%）")
    print(f"耗时 {time.time() - started:.0f} 秒")
    print()
    print("能拿到的音质分布：")
    for level, count in sorted(all_levels.items(), key=lambda x: -x[1]):
        print(f"    {LEVEL_LABEL.get(level, level):<10} {count:>4} 首")
    if all_failures:
        print()
        print(f"拿不到的 {len(all_failures)} 首（前 20）：")
        for f in all_failures[:20]:
            hint = PRIVILEGE_HINT.get(f.get("fee"), f"fee={f.get('fee')}")
            print(f"    {(f['name'] or '')[:30]:<32} {(f['artist'] or '')[:14]:<16} {hint}")
    print()
    print("完整结果：", STATE_FILE)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "login":
        return cmd_login()
    if cmd == "wait":
        return cmd_wait()
    if cmd == "playlists":
        return cmd_playlists()
    if cmd == "probe":
        def _num(flag: str, default: int) -> int:
            return int(argv[argv.index(flag) + 1]) if flag in argv else default

        return cmd_probe(
            sample=_num("--sample", 60),
            per_playlist=_num("--playlists", 2),
            unblock="--unblock" in argv,
            liked="--liked" in argv,
            name_filter=(argv[argv.index("--name") + 1] if "--name" in argv else ""),
        )
    print(__doc__)
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
