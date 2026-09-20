"""``ipod-sync`` 命令行入口：把网易云的歌弄到本地缓存。

这一步**不碰 iPod**——只管从网易云取歌、下到本地、把状态记下来。
真正写进设备是下一步（P2）的事。这样划分的好处是：
下载可以慢慢来、可以重试、可以随时停，而碰设备的那一刻才是"严肃操作"。

界面全中文，跟 ``ipod`` 命令保持一致。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ipod_cli.ncm.client import (
    DEFAULT_BASE_URL,
    DEFAULT_MIN_INTERVAL,
    DEFAULT_QUALITY,
    QUALITY_LABEL,
    QUALITY_LADDER,
    NcmClient,
    NcmError,
    NotLoggedInError,
    Playlist,
)
from ipod_cli.ncm.qrterm import render_qr
from ipod_cli.ncm.state import DEFAULT_DB_RELPATH, Account, StateStore
from ipod_cli.ncm.sync import (
    SyncPlan,
    SyncSource,
    execute_downloads,
    plan_sync,
    sync_to_ipod,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USER_ABORT = 2
EXIT_VERIFY_FAILED = 3

#: 默认下载缓存目录（相对项目根）
DEFAULT_CACHE_RELPATH = Path(".ncm") / "cache"

SETTING_QUALITY = "quality"


def _out(text: str = "") -> None:
    print(text)


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _end_progress() -> None:
    """清掉原地刷新的行（只有真终端才有必要）。"""
    if sys.stdout.isatty():
        sys.stdout.write("\r" + " " * 78 + "\r")
        sys.stdout.flush()


# ──────────────────────────────────────────────────────────────────────
# 上下文
# ──────────────────────────────────────────────────────────────────────


class Context:
    """一次命令运行所需的东西。"""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.store = StateStore(Path(args.db))
        self.client = NcmClient(
            args.api,
            min_interval=args.interval,
        )
        self.cache_dir = Path(args.cache)

    def quality(self) -> str:
        """音质：命令行 > 状态库设置 > 默认值。"""
        explicit = getattr(self.args, "level", "")
        if explicit:
            return explicit
        return self.store.get_setting(SETTING_QUALITY, DEFAULT_QUALITY)

    def account(self, *, required: bool = True) -> Account | None:
        account = self.store.active_account()
        if account is None and required:
            raise NotLoggedInError(
                "还没有登录，或者有多个账号但没指定当前用哪个。\n"
                "  登录：ipod-sync login\n"
                "  切换：ipod-sync use <uid>"
            )
        if account is not None:
            # ★ 顺手把 cookie 挂到 client 上，**每个调用点都别忘**。
            # 漏传 cookie 的后果不是报错，而是静默拿到试听片段——
            # 文件能播、标签正常，只有时长不对。实测踩过这个坑。
            self.client.cookie = account.cookie
        return account


# ──────────────────────────────────────────────────────────────────────
# login / accounts
# ──────────────────────────────────────────────────────────────────────


def cmd_login(ctx: Context) -> int:
    """扫码登录。已经登录过的账号会更新 cookie，不会多出一条。"""
    if not ctx.client.ping():
        _err(f"❌ 连不上网易云 API 服务（{ctx.args.api}）")
        _err("   先把服务跑起来：")
        _err("     cd source/repos/api-enhanced")
        _err("     PORT=4000 node app.js")
        return EXIT_ERROR

    unikey, png = ctx.client.qr_login_start()

    art = render_qr(png)
    if art:
        _out(art)
        _out()
        _out("用网易云音乐 App 扫上面的二维码。")
    else:
        qr_path = Path(ctx.args.db).parent / "login-qrcode.png"
        qr_path.write_bytes(png)
        _out(f"二维码已保存：{qr_path}")
        _out("（用图片查看器打开它，再用网易云音乐 App 扫）")

    _out()
    _out("等待扫码…（在手机上确认）")

    import time

    deadline = time.time() + 180
    last = None
    while time.time() < deadline:
        code, cookie = ctx.client.qr_login_poll(unikey)
        if code != last:
            _out(
                {
                    800: "  二维码已过期，请重新运行 login",
                    801: "  等待扫码…",
                    802: "  已扫描，请在手机上点确认",
                    803: "  授权成功 ✅",
                }.get(code, f"  未知状态 {code}")
            )
            last = code
        if code == 803:
            if not cookie:
                _err("授权成功但没拿到 cookie，请重试")
                return EXIT_ERROR
            # 验证 cookie 并拿到账号信息
            probe = NcmClient(ctx.args.api, cookie=cookie,
                              min_interval=ctx.args.interval)
            account = probe.login_status()
            ctx.store.save_account(
                account.uid, account.cookie,
                nickname=account.nickname, vip_type=account.vip_type,
            )
            ctx.store.set_active_uid(account.uid)
            _out()
            _out(f"账号：{account.nickname}  uid={account.uid}")
            _out(f"会员：{'VIP' if account.vip_type else '普通用户'}"
                 f"（vipType={account.vip_type}）")
            _out(f"本次共发出 {probe.request_count} 次请求")
            _out()
            _out("下一步：ipod-sync playlists")
            return EXIT_OK
        if code == 800:
            return EXIT_ERROR
        time.sleep(2)

    _err("等待超时。重新运行 login 换一张二维码。")
    return EXIT_ERROR


def cmd_accounts(ctx: Context) -> int:
    accounts = ctx.store.list_accounts()
    if not accounts:
        _out("还没有登录过任何账号。运行：ipod-sync login")
        return EXIT_OK

    active = ctx.store.active_account()
    _out(f"共 {len(accounts)} 个账号：")
    _out()
    for account in accounts:
        mark = "→" if active and account.uid == active.uid else " "
        vip = "VIP" if account.vip_type else "普通"
        _out(f" {mark} {account.uid:<12} {account.nickname:<20} {vip}")
    _out()
    _out("切换账号：ipod-sync use <uid>")
    return EXIT_OK


def cmd_use(ctx: Context, uid: int) -> int:
    account = ctx.store.get_account(uid)
    if account is None:
        _err(f"没有 uid={uid} 的账号。用 accounts 看看有哪些。")
        return EXIT_ERROR
    ctx.store.set_active_uid(uid)
    _out(f"已切换到：{account.nickname}（uid={uid}）")
    return EXIT_OK


# ──────────────────────────────────────────────────────────────────────
# playlists
# ──────────────────────────────────────────────────────────────────────


def cmd_playlists(ctx: Context) -> int:
    account = ctx.account()
    assert account is not None

    playlists = ctx.client.user_playlists(account.uid, cookie=account.cookie)

    # 曲目数直接从歌单列表里取——**别再单独问一次 /likelist**：
    # 那次请求只为数个数，而且 likelist 的顺序本来就不可信（跟 App 对不上），
    # 拿它当"我喜欢的音乐有多少首"的来源纯属多此一举。
    liked_playlist = next((p for p in playlists if p.is_liked), None)

    _out(f"账号：{account.nickname}")
    if liked_playlist is not None:
        # 用歌单自己的名字：用户可能改过（实测改成了"被改过名的喜欢的音乐"）
        _out(f"「{liked_playlist.name}」：{liked_playlist.track_count} 首")
    _out()
    _out(f"歌单共 {len(playlists)} 个：")
    _out()
    _out(f"{'曲目数':>7}  {'类型':<5} 名字")
    _out("-" * 64)
    for playlist in sorted(playlists, key=lambda p: -p.track_count):
        kind = "喜欢" if playlist.is_liked else ("收藏" if playlist.subscribed else "自建")
        _out(f"{playlist.track_count:>7}  {kind:<5} {playlist.name}")
    _out()
    _out(f"本次共发出 {ctx.client.request_count} 次请求")
    _out()
    _out("同步我喜欢的音乐：ipod-sync plan --liked")
    _out("同步某个歌单：    ipod-sync plan --playlist \"歌单名\"")
    return EXIT_OK


# ──────────────────────────────────────────────────────────────────────
# 同步源的解析
# ──────────────────────────────────────────────────────────────────────


def resolve_source(ctx: Context, name: str = "") -> SyncSource:
    """决定同步哪个源。没给就默认「我喜欢的音乐」。"""
    account = ctx.account()
    assert account is not None

    if not name:
        return SyncSource.liked()

    playlists = ctx.client.user_playlists(account.uid, cookie=account.cookie)
    match = _match_playlist(playlists, name)
    if match is None:
        raise NcmError(
            f"没找到叫「{name}」的歌单。用 `ipod-sync playlists` 看看有哪些。"
        )
    if match.is_liked:
        return SyncSource.liked(match.name)
    return SyncSource.playlist(match.id, match.name)


def _match_playlist(playlists: list[Playlist], name: str) -> Playlist | None:
    """按名字找歌单：先精确，再忽略大小写的包含匹配。"""
    for playlist in playlists:
        if playlist.name == name:
            return playlist
    lowered = name.lower()
    for playlist in playlists:
        if lowered in playlist.name.lower():
            return playlist
    return None


# ──────────────────────────────────────────────────────────────────────
# plan / download
# ──────────────────────────────────────────────────────────────────────


def _build_plan(
    ctx: Context, source_name: str, *, check_urls: bool, library=None
) -> SyncPlan:
    account = ctx.account()
    assert account is not None

    source = resolve_source(ctx, source_name)
    level = ctx.quality()
    limit = ctx.args.limit

    _out(f"同步源：{source.name}")
    _out(f"音质：{QUALITY_LABEL.get(level, level)}")
    if limit:
        _out(f"（--limit {limit}：本次只处理前 {limit} 首）")
    _out()

    if check_urls:
        _out("⚠  已开启 --check-urls：会逐首查询下载链接，")
        _out("   每首歌要 1~3 次请求。几十首还行，几百首请慎重。")
        _out()

    return plan_sync(
        ctx.client,
        ctx.store,
        source,
        level=level,
        uid=account.uid,
        cookie=account.cookie,
        limit=limit,
        check_urls=check_urls,
        library=library,
        progress=lambda m: _out(f"  {m}"),
    )


def _print_plan(ctx: Context, plan: SyncPlan) -> None:
    todo = plan.to_download
    fetch = plan.needs_fetch
    _out()
    _out("=" * 68)
    _out(f"  要处理   {len(todo):>5} 首"
         f"（其中 {len(fetch)} 首需要下载，{len(todo) - len(fetch)} 首本地已有）")
    _out(f"  已跳过   {len(plan.to_skip):>5} 首（已同步）")
    _out(f"  拿不到   {len(plan.unavailable):>5} 首")
    _out(f"  预计体积 {plan.estimated_mb:>8.1f} MB"
         f"  （{'精确值' if ctx.args.check_urls else '估算，误差可能较大'}）")
    _out("=" * 68)

    if todo:
        _out()
        _out("要处理的歌（最多显示 30 首）：")
        for item in todo[:30]:
            size = item.actual_bytes or item.est_bytes
            level = item.actual_level or plan.level
            mark = "已有" if item.cached else "要下"
            _out(f"  [{mark}] {item.song.label[:48]:<50} "
                 f"{size / 1048576:>6.1f} MB  {level}")
        if len(todo) > 30:
            _out(f"  … 还有 {len(todo) - 30} 首")

    if plan.unavailable:
        _out()
        _out(f"拿不到的 {len(plan.unavailable)} 首（无版权/已下架）：")
        for item in plan.unavailable[:10]:
            _out(f"  {item.song.label[:60]}")
        if len(plan.unavailable) > 10:
            _out(f"  … 还有 {len(plan.unavailable) - 10} 首")

    _out()
    _out(f"本次共发出 {ctx.client.request_count} 次 API 请求")


def cmd_plan(ctx: Context) -> int:
    """只看要下什么，不动任何东西。"""
    plan = _build_plan(ctx, ctx.args.playlist, check_urls=ctx.args.check_urls)
    _print_plan(ctx, plan)
    _out()
    _out("（--dry-run：以上只是预览，什么都没下）")
    _out("确认无误后执行：ipod-sync download"
         + (f' --playlist "{ctx.args.playlist}"' if ctx.args.playlist else " --liked"))
    return EXIT_OK


def cmd_download(ctx: Context) -> int:
    """真的下载（**不碰 iPod**，只下到本地缓存）。"""
    plan = _build_plan(ctx, ctx.args.playlist, check_urls=ctx.args.check_urls)
    _print_plan(ctx, plan)

    todo = plan.needs_fetch
    if not todo:
        _out()
        _out("本地缓存已经齐全，没有需要下载的歌。")
        return EXIT_OK

    _out()
    if not ctx.args.yes:
        _out(f"将下载 {len(todo)} 首到：{ctx.cache_dir}")
        _out("（串行下载，一首一首来——并发会触发网易云风控）")
        try:
            answer = input("继续？[y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            _out("已取消。")
            return EXIT_USER_ABORT

    _out()
    account = ctx.account()
    outcome = execute_downloads(
        ctx.client, ctx.store, plan, ctx.cache_dir,
        cookie=account.cookie if account else "",
        progress=lambda m: _out(f"  {m}"),
    )

    _out()
    _out("=" * 68)
    _out(f"  成功   {len(outcome.downloaded):>5} 首  {outcome.total_mb:.1f} MB")
    _out(f"  失败   {len(outcome.failed):>5} 首")
    _out("=" * 68)

    if outcome.failed:
        _out()
        _out("失败清单：")
        for song, reason in outcome.failed[:20]:
            _out(f"  {song.label[:50]:<52} {reason[:60]}")
        if len(outcome.failed) > 20:
            _out(f"  … 还有 {len(outcome.failed) - 20} 首")

    _out()
    _out(f"本次共发出 {ctx.client.request_count} 次 API 请求")
    _out(f"缓存目录：{ctx.cache_dir}")
    _out()
    _out("下一步（P2）：把这些歌写进 iPod。")
    return EXIT_OK


# ──────────────────────────────────────────────────────────────────────
# push：写进 iPod（P2）
# ──────────────────────────────────────────────────────────────────────


def cmd_push(ctx: Context) -> int:
    """把歌写进 iPod，并按歌单建好同名播放列表。"""
    from ipod_cli.discovery import DeviceNotFoundError, require_ipod
    from ipod_cli.library import read_library

    account = ctx.account()
    assert account is not None

    # 先找设备。找不到就早点说，别白规划一场。
    try:
        device = require_ipod(ctx.args.ipod)
    except DeviceNotFoundError as exc:
        _err(f"❌ {exc}")
        return EXIT_ERROR

    library = read_library(device.root)
    _out(f"设备：{device.display_name}")
    _out(f"名字：{library.ipod_name}")
    _out(f"设备上现有 {len(library.tracks)} 首曲目"
         f"（剩余空间 {device.free_text}）")
    _out()

    # 把设备库传下去：判断"哪些已经同步过"要对着**设备实际内容**，
    # 不能只信状态库（彩排跑一次就会污染它）。
    plan = _build_plan(ctx, ctx.args.playlist, check_urls=False, library=library)

    fetch = plan.needs_fetch
    _out()
    _out("=" * 68)
    _out(f"  要写入     {len(plan.to_download):>5} 首"
         f"（其中 {len(fetch)} 首需要先下载）")
    _out(f"  已在设备上 {len(plan.to_skip):>5} 首")
    _out(f"  拿不到     {len(plan.unavailable):>5} 首")
    if ctx.args.make_playlist:
        _out(f"  播放列表   会新建/覆盖「{plan.source.name}」")
    _out("=" * 68)

    if not plan.to_download and not plan.to_skip:
        _out()
        _out("没有可写入的内容。")
        return EXIT_OK

    _out()
    _out("⚠  这会修改 iPod 上的数据库。")
    _out("   写入前会自动备份（iTunesDB.backup），写后会读回校验。")
    if not ctx.args.yes:
        try:
            answer = input("确认写入？[y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes", "是"):
            _out("已取消。")
            return EXIT_USER_ABORT

    _out()
    device.activate()      # ★ 必须先注册到内核，否则写入器认为设备未知而拒绝

    try:
        outcome = sync_to_ipod(
            ctx.client, ctx.store, plan, device, library,
            cookie=account.cookie,
            make_playlist=ctx.args.make_playlist,
            force=ctx.args.force,
            progress=lambda m: _out(f"  {m}"),
        )
    except Exception as exc:
        _end_progress()
        _err("")
        _err(f"❌ 写入过程中出错：{type(exc).__name__}: {exc}")
        # 别一口咬定"设备没被改"——写入本身可能已经成功了，
        # 出错的是之后的状态记录。用 verify 看真实情况才是准的。
        _err("   设备可能已被修改。用 `ipod verify` 确认真实状态；")
        _err("   需要回退就用 iPod_Control/iTunes/iTunesDB.backup。")
        return EXIT_ERROR
    _end_progress()

    _out()
    _out("=" * 68)
    _out(f"  已写入   {outcome.added:>5} 首  {outcome.copied_mb:.1f} MB")
    if outcome.playlist_names:
        _out(f"  播放列表 {outcome.playlist_names[0]}"
             f"（{outcome.playlist_size} 首）")
    if outcome.failed:
        _out(f"  失败     {len(outcome.failed):>5} 首")
    _out(f"  本次 API 请求 {outcome.request_count} 次")
    _out("=" * 68)

    if outcome.failed:
        _out()
        _out("失败清单：")
        for source, reason in outcome.failed[:10]:
            _out(f"  {source}")
            _out(f"      {reason}")

    if not outcome.verified:
        _err("")
        _err(f"⚠  读回校验未通过：{outcome.note}")
        _err("   可以用 iPod_Control/iTunes/iTunesDB.backup 还原。")
        return EXIT_VERIFY_FAILED

    _out()
    _out(outcome.note)
    _out("完成。安全移除 iPod 后再拔线。")
    return EXIT_OK


# ──────────────────────────────────────────────────────────────────────
# status / settings
# ──────────────────────────────────────────────────────────────────────


def cmd_status(ctx: Context) -> int:
    stats = ctx.store.stats()
    account = ctx.store.active_account()

    _out("=== 本地状态 ===")
    _out()
    if account:
        _out(f"  当前账号   {account.nickname}（uid={account.uid}）")
    else:
        _out("  当前账号   （未登录）")
    _out(f"  已登录账号 {stats['accounts']} 个")
    _out()
    _out(f"  已下载     {stats['downloaded']} 首")
    _out(f"  已进 iPod  {stats['synced']} 首"
         f"（{stats['synced_bytes'] / 1048576:.1f} MB）")
    _out()
    _out(f"  音质设置   {ctx.store.get_setting(SETTING_QUALITY, DEFAULT_QUALITY)}"
         f"（默认 {DEFAULT_QUALITY}）")
    _out(f"  状态库     {ctx.store.path}")
    _out(f"  缓存目录   {ctx.cache_dir}")
    return EXIT_OK


def cmd_set_quality(ctx: Context, level: str) -> int:
    if level not in QUALITY_LADDER:
        _err(f"音质只能是 {'/'.join(QUALITY_LADDER)} 之一，收到 {level!r}")
        _err("（对 iPod 无意义的 hires/jymaster/jyeffect 不在候选里）")
        return EXIT_ERROR
    ctx.store.set_setting(SETTING_QUALITY, level)
    _out(f"音质已设为：{QUALITY_LABEL.get(level, level)}")
    return EXIT_OK


# ──────────────────────────────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ipod-sync",
        description="把网易云的歌下载到本地，准备同步进 iPod。",
        epilog=(
            "例：\n"
            "  ipod-sync login                        扫码登录\n"
            "  ipod-sync playlists                     看有哪些歌单\n"
            "  ipod-sync plan --liked --limit 5        只看前 5 首要下什么\n"
            "  ipod-sync download --liked --limit 5 -y 先下 5 首试试\n"
            "\n"
            "注意：所有请求都是串行的，并且有强制间隔——\n"
            "为的是别让网易云把账号当成异常流量。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--api", default=DEFAULT_BASE_URL,
        help=f"本地网易云 API 服务地址（默认 {DEFAULT_BASE_URL}）",
    )
    parser.add_argument(
        "--db", default=str(DEFAULT_DB_RELPATH),
        help=f"状态库路径（默认 {DEFAULT_DB_RELPATH}）",
    )
    parser.add_argument(
        "--cache", default=str(DEFAULT_CACHE_RELPATH),
        help=f"下载缓存目录（默认 {DEFAULT_CACHE_RELPATH}）",
    )
    parser.add_argument(
        "--interval", type=float, default=DEFAULT_MIN_INTERVAL,
        help=f"两次请求之间的最小间隔秒数（默认 {DEFAULT_MIN_INTERVAL}）",
    )

    sub = parser.add_subparsers(dest="command", metavar="命令")

    sub.add_parser("login", help="扫码登录（已有账号则更新 cookie）")
    sub.add_parser("accounts", help="列出已登录的账号")
    sub.add_parser("status", help="看本地状态")

    use = sub.add_parser("use", help="切换当前账号")
    use.add_argument("uid", type=int, help="账号 uid")

    sub.add_parser("playlists", help="列出歌单")

    quality = sub.add_parser("quality", help="设置下载音质")
    quality.add_argument("level", choices=list(QUALITY_LADDER))

    for name, help_text in (
        ("plan", "只看要下什么（不动任何东西）"),
        ("download", "下载到本地缓存（不碰 iPod）"),
        ("push", "下载 + 写进 iPod（会修改设备）"),
    ):
        node = sub.add_parser(name, help=help_text)
        node.add_argument("--liked", action="store_true",
                          help="同步「我喜欢的音乐」（默认）")
        node.add_argument("--playlist", default="", metavar="名字",
                          help="同步指定歌单（按名字匹配）")
        node.add_argument("--level", choices=list(QUALITY_LADDER), default="",
                          help=f"音质（默认取设置值，出厂 {DEFAULT_QUALITY}）")
        node.add_argument("--limit", type=int, default=0,
                          help="只处理前 N 首（验证时用，避免一次下太多）")
        node.add_argument("--check-urls", action="store_true",
                          help="逐首查询下载链接（慢，且请求数成倍增加）")
        if name == "download":
            node.add_argument("-y", "--yes", action="store_true",
                              help="不再确认")
        if name == "push":
            node.add_argument("--ipod", default=None, metavar="路径",
                              help="iPod 盘符/挂载点（默认自动找）")
            node.add_argument("--no-playlist", dest="make_playlist",
                              action="store_false",
                              help="只放歌，不在 iPod 上建播放列表")
            node.add_argument("--force", action="store_true",
                              help="即使判定为重复也导入")
            node.add_argument("-y", "--yes", action="store_true",
                              help="不再确认")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if not args.command:
        parser.print_help()
        return EXIT_OK

    ctx = Context(args)

    try:
        if args.command == "login":
            return cmd_login(ctx)
        if args.command == "accounts":
            return cmd_accounts(ctx)
        if args.command == "use":
            return cmd_use(ctx, args.uid)
        if args.command == "playlists":
            return cmd_playlists(ctx)
        if args.command == "status":
            return cmd_status(ctx)
        if args.command == "quality":
            return cmd_set_quality(ctx, args.level)
        if args.command == "plan":
            return cmd_plan(ctx)
        if args.command == "download":
            return cmd_download(ctx)
        if args.command == "push":
            return cmd_push(ctx)
    except NotLoggedInError as exc:
        _err(f"❌ {exc}")
        return EXIT_ERROR
    except NcmError as exc:
        _err(f"❌ 网易云接口出错：{exc}")
        return EXIT_ERROR
    except KeyboardInterrupt:
        _err("")
        _err("已中断。已下载的文件保留在缓存目录，下次会跳过。")
        return EXIT_USER_ABORT

    parser.print_help()
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
