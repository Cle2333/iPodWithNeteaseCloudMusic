"""同步引擎：算清"要下什么"，然后下载。

## 为什么先做"规划"再做"执行"

同步是破坏性动作的邻居（会写 iPod），所以拆成两步：

1. **规划**（``plan_sync``）：纯计算，不碰网络也不碰设备。算出
   哪些歌要新增、哪些已经有了、哪些根本拿不到。
2. **执行**（``execute_downloads``）：把规划里的歌下到本地缓存。

P1 只做这两步 + 一个 ``--dry-run`` 给人看；
**写进 iPod** 是 P2（``import`` + 建播放列表）。

## API 调用省着花

规划默认**不逐首查下载链接**——那要为每首歌发 1~3 次请求，
246 首红心歌就是几百次请求，白白增加风控压力。

所以默认走"按状态库判断 + 按档位估体积"，链接留到真下载时才问。
想看精确可用性再加 ``--check-urls``，那时候也会告诉你代价。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from ipod_cli.ncm.client import NcmClient, Song
from ipod_cli.ncm.downloader import DownloadResult, download_song
from ipod_cli.ncm.state import StateStore

#: 各档位的单曲体积中位数（MB），来自 P0 对 350 首的实测。
#: 只用于规划阶段"估"体积——精确大小只有问过链接才知道。
TYPICAL_SIZE_MB = {
    "lossless": 25.0,
    "exhigh": 7.0,
    "standard": 3.0,
}

#: 要下载 / 跳过 / 拿不到
ACTION_DOWNLOAD = "download"
ACTION_SKIP = "skip"

#: 这次规划要达成什么。**跳过条件完全不同**，混用就会出
#: "本地文件没了却不去下载"这种事。
#:
#: * ``TARGET_IPOD``  —— 让设备上有这些歌（下载 + 写入）
#: * ``TARGET_LOCAL`` —— 只要电脑上有这些文件（下载完就结束）
TARGET_IPOD = "ipod"
TARGET_LOCAL = "local"
ACTION_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SyncSource:
    """同步什么。"""

    kind: str                    # "liked" 或 "playlist"
    name: str
    playlist_id: int = 0

    @classmethod
    def liked(cls, name: str = "我喜欢的音乐", playlist_id: int = 0) -> SyncSource:
        """「我喜欢的音乐」。

        ``playlist_id`` 是它在网易云那边的**歌单 id**（specialType=5）。
        界面调用时顺手带上（歌单列表里本来就有），省掉一次解析请求。
        传 0 就由客户端自己去解析。
        """
        return cls(kind="liked", name=name, playlist_id=playlist_id)

    @classmethod
    def playlist(cls, playlist_id: int, name: str) -> SyncSource:
        return cls(kind="playlist", name=name, playlist_id=playlist_id)


@dataclass
class PlannedSong:
    song: Song
    action: str
    reason: str = ""
    #: 估算体积（字节）。规划阶段拿不到精确值。
    est_bytes: int = 0
    #: ``--check-urls`` 时填上真实信息
    actual_level: str = ""
    actual_bytes: int = 0
    #: 本地缓存里已经有这个文件了吗。
    #:
    #: 这**不是** "跳过" 的理由——歌没进 iPod 就还是得处理。
    #: 它只影响"要不要真的去下载"。P1 时我把它当成了跳过条件，
    #: 结果 push 显示"新增 0 首"然后又真写了 3 首，前后矛盾。
    cached: bool = False


@dataclass
class SyncPlan:
    source: SyncSource
    level: str
    items: list[PlannedSong] = field(default_factory=list)

    @property
    def to_download(self) -> list[PlannedSong]:
        """需要弄到 iPod 上的歌（含本地已经缓存好的）。"""
        return [i for i in self.items if i.action == ACTION_DOWNLOAD]

    @property
    def needs_fetch(self) -> list[PlannedSong]:
        """还没下到本地的那些——真正要发下载请求的。"""
        return [i for i in self.to_download if not i.cached]

    @property
    def to_skip(self) -> list[PlannedSong]:
        return [i for i in self.items if i.action == ACTION_SKIP]

    @property
    def unavailable(self) -> list[PlannedSong]:
        return [i for i in self.items if i.action == ACTION_UNAVAILABLE]

    @property
    def estimated_bytes(self) -> int:
        return sum(i.actual_bytes or i.est_bytes for i in self.to_download)

    @property
    def estimated_mb(self) -> float:
        return self.estimated_bytes / 1048576


#: 一首歌在下载过程中的状态。
SONG_DOWNLOADING = "downloading"
SONG_DONE = "done"
SONG_FAILED = "failed"


@dataclass
class SongEvent:
    """下载过程中"某一首歌现在怎么样了"。

    存在的理由：文本日志（``progress``）是给人看的，界面没法用——
    要去 parse "✓ [3/246] 歌名 → 7.2 MB" 这种句子才能拿到数字，
    改一次措辞就坏，而且歌名里带数字（《7/11》）会被认成进度。
    数字归数字、文本归文本。
    """

    #: 第几首（从 1 数起，跟界面上的序号一致）。
    index: int
    #: 这一批一共几首。
    total: int
    song: Song
    #: SONG_DOWNLOADING / SONG_DONE / SONG_FAILED
    status: str
    #: 字节数，只在 done 时有意义。
    size: int = 0
    #: 失败原因，只在 failed 时有意义。
    reason: str = ""


def fetch_source_songs(
    client: NcmClient,
    source: SyncSource,
    *,
    uid: int = 0,
    cookie: str = "",
    limit: int = 0,
    only_ids: set[int] | None = None,
) -> list[Song]:
    """把同步源的曲目取全（含元数据）。

    ``uid`` 是**必须的**——``/likelist`` 靠它定位"谁的喜欢的音乐"，
    传 0 会拿到空列表（而且不报错，最难查的那种）。

    ``limit`` 用来小规模验证——**先下 3 首试试**，别一上来就几百首。

    ``only_ids`` 只保留这些歌（界面上的"下载选中的 N 首"）。过滤放在这里
    而不是规划完之后，是因为歌单曲目本来就得整份取回来（不然不知道歌单里
    有什么），在源头筛掉不影响请求数，却让后面的 limit / 已就绪判断全部
    作用在正确的集合上。

    ``None`` = 不过滤（整单）；**空集合和 None 是不同的**——空集合表示
    "一首都没选"，结果就是空列表。调用方要自己判断这算不算错误。
    """
    if source.kind == "liked":
        # 「我喜欢的音乐」走**歌单接口**，不走 /likelist。
        #
        # `/likelist` 只保证"有哪些歌"，顺序是网易云内部存的，对外不可解释：
        # 实测 246 首，两边集合完全一样、**同位置一个都不匹配**。用它的话，
        # iPod 里歌的排列跟用户在 App 里看到的完全是两回事。
        #
        # `/playlist/track/all` 顺带把元数据也带回来（name/ar/dt 都有），
        # 所以这么做**反而更省请求**：246 首从 4 次（likelist + 3×song/detail）
        # 降到 1 次；6000 首从 61 次降到 6 次。
        liked_id = source.playlist_id or client.liked_playlist_id(
            uid=uid, cookie=cookie
        )
        if liked_id:
            tracks = client.playlist_tracks(liked_id, cookie=cookie)
        else:
            # 兜底：读不到歌单 id（接口抽风、账号没有这个歌单）就退回 likelist。
            # 顺序会不对，但**有歌**远好过整个同步失败。
            tracks = client.song_details(
                client.liked_song_ids(uid=uid, cookie=cookie), cookie=cookie
            )
    elif source.kind == "playlist":
        tracks = client.playlist_tracks(source.playlist_id, cookie=cookie)
    else:
        raise ValueError(f"不认识的同步源：{source.kind!r}")

    # 过滤放在取全之后的统一位置。歌单曲目本来就得整份取回来（不然不知道
    # 里面有什么），在源头筛掉不影响请求数。
    if only_ids is not None:
        tracks = [t for t in tracks if t.id in only_ids]
    if limit:
        tracks = tracks[:limit]
    return tracks


def plan_sync(
    client: NcmClient,
    store: StateStore,
    source: SyncSource,
    *,
    level: str,
    uid: int = 0,
    cookie: str = "",
    limit: int = 0,
    check_urls: bool = False,
    library=None,
    progress: Callable[[str], None] | None = None,
    only_ids: set[int] | None = None,
    target: str = TARGET_IPOD,
) -> SyncPlan:
    """算出要下什么。

    ``check_urls=True`` 会逐首问下载链接（能拿到精确的体积和可用性），
    代价是**每首歌 1~3 次请求**——几十首还行，几百首就别开。

    ``library`` 是目标设备的库（可选）。给了它就用**设备实际内容**判断
    "哪些已经同步过"，而不是只信状态库——状态库可能记着别的设备（或彩排）
    的同步记录，只看它会漏掉真正要写的歌。

    ``only_ids`` 只规划这些歌（界面上的"下载选中的 N 首"）。

    ``target`` 决定**跳过条件**，这是两件不同的事：

    * :data:`TARGET_IPOD`：设备上已经有了才跳过。本地有没有文件不影响
      ——没文件就补下，有文件就直接写入。
    * :data:`TARGET_LOCAL`：本地文件已经在才跳过。**iPod 上有没有跟这无关**，
      也**不查状态库**——那不分成"用户只要本地"的场景，查了反而会误跳。
    """
    def say(message: str) -> None:
        if progress:
            progress(message)

    songs = fetch_source_songs(
        client, source, uid=uid, cookie=cookie, limit=limit, only_ids=only_ids
    )
    say(f"取到 {len(songs)} 首，正在比对本地记录…")

    # ── 跳过什么，取决于用户要的是什么 ──
    #
    # 「同步到 iPod」和「下载到本地」是**两件事**，以前被混成一件：
    # 只要歌在 iPod 上就无条件跳过。于是——删掉本地文件、再点「下载」——
    # 因为歌还在 iPod 上，全部被跳过，一首都不下，界面还回一句
    # "都已经就绪"。用户的原话："我把本地的歌删了重新下载，它提示已准备就绪。"
    #
    # * 要 iPod 上有  → 设备上已经有了才跳过
    # * 只要本地有    → 本地文件已经在才跳过（iPod 有没有跟这无关）
    if target == TARGET_IPOD:
        # 有设备库就对着设备核对；没有就退而信状态库（比如彩排/离线）
        synced = (
            synced_on_device(store, library)
            if library is not None
            else store.synced_ids()
        )
    else:
        # 本地下载**根本不查 iPod 状态**。
        #
        # 这一条顺带堵掉一个更隐蔽的坑：`store.synced_ids()` 是状态库，
        # 它**不分设备**——彩排或另一台机器写过的记录照样算数。
        # 本地下载本来就不关心 iPod，那就不该让一份不可靠的记录
        # 决定"要不要下载"。
        synced = set()

    plan = SyncPlan(source=source, level=level)
    typical = TYPICAL_SIZE_MB.get(level, 7.0) * 1048576

    for song in songs:
        if song.id in synced:
            plan.items.append(
                # 措辞跟界面上的三个状态用词一致（未下载/已下载/已同步）：
                # 这个 reason 会**显示在规划预览对话框里**，界面上写"已同步"、
                # 弹窗里写"已在 iPod 上"，就是自相矛盾。
                PlannedSong(song=song, action=ACTION_SKIP, reason="已同步")
            )
            continue

        # 本地缓存命中只意味着"不用重新下载"，**不代表不用处理**：
        # 歌还没进 iPod 就还是要写进去。所以对 iPod 目标只打标记，不跳过。
        cached = store.cached_download(song.id)
        is_cached = cached is not None

        if target == TARGET_LOCAL and is_cached:
            # 只要本地有，而本地已经有了 —— 这才是真的没事可做
            plan.items.append(
                PlannedSong(song=song, action=ACTION_SKIP, reason="已下载")
            )
            continue

        if check_urls:
            url = client.song_url(song.id, level, cookie=cookie)
            if not url.available:
                plan.items.append(
                    PlannedSong(
                        song=song, action=ACTION_UNAVAILABLE,
                        reason="无版权或已下架",
                    )
                )
                continue
            plan.items.append(
                PlannedSong(
                    song=song, action=ACTION_DOWNLOAD,
                    actual_level=url.level, actual_bytes=url.size,
                    cached=is_cached,
                )
            )
        else:
            size = cached.size if is_cached else int(typical)
            plan.items.append(
                PlannedSong(
                    song=song, action=ACTION_DOWNLOAD,
                    est_bytes=size, cached=is_cached,
                )
            )

    say(
        f"规划完成：新增 {len(plan.to_download)} 首，"
        f"跳过 {len(plan.to_skip)} 首，无法获取 {len(plan.unavailable)} 首"
    )
    return plan


@dataclass
class DownloadOutcome:
    downloaded: list[DownloadResult] = field(default_factory=list)
    failed: list[tuple[Song, str]] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(r.size for r in self.downloaded)

    @property
    def total_mb(self) -> float:
        return self.total_bytes / 1048576


def execute_downloads(
    client: NcmClient,
    store: StateStore,
    plan: SyncPlan,
    dest_dir: Path,
    *,
    cookie: str = "",
    progress: Callable[[str], None] | None = None,
    on_item: Callable[[int, int], None] | None = None,
    on_song: Callable[[SongEvent], None] | None = None,
) -> DownloadOutcome:
    """按规划把歌下到本地缓存。**串行，一首一首来。**

    刻意不并发：并发下几十首对网易云来说是明显的异常行为，
    而且用户明确要求"别把号搞封了"。串行慢一点，但安全。

    ``cookie`` 必须往上传到 ``download_song``——**不带 cookie 请求下载链接
    会拿到试听片段**（实测踩过）。

    下载成功的会记进状态库的下载缓存，下次规划直接跳过。
    """
    dest_dir = Path(dest_dir)
    outcome = DownloadOutcome()
    # 只下"还没到本地"的那些。已经不缺的文件重下是白费时间和带宽，
    # 也白白多几次请求。
    todo = plan.needs_fetch
    total = len(todo)

    # 先报一次 0/总数——界面在下载第一首之前就能显示"共 N 首"，
    # 而不是等到第一首下完才知道总数
    if on_item is not None:
        on_item(0, total)

    for index, item in enumerate(todo, 1):
        label = f"[{index}/{total}] {item.song.label}"
        # 开始下载就报一次：界面要能显示"正在下载《X》"，
        # 而不是等这一首下完了才知道在干嘛（大文件可能要等十几秒）
        if on_song is not None:
            on_song(SongEvent(index, total, item.song, SONG_DOWNLOADING))
        try:
            result = download_song(
                client, item.song, dest_dir,
                level=plan.level, cookie=cookie, progress=progress,
            )
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            outcome.failed.append((item.song, reason))
            if progress:
                progress(f"✗ {label} —— {exc}")
            if on_song is not None:
                on_song(
                    SongEvent(index, total, item.song, SONG_FAILED, reason=reason)
                )
            # 失败的也要计数：进度条不动的话看起来像卡死了
            if on_item is not None:
                on_item(index, total)
            continue

        store.remember_download(
            result.song_id,
            result.path,
            level=result.level,
            size=result.size,
            # 顺手记下歌名/艺人：下载页要列出"电脑上已下载的音乐"，
            # 不存的话界面得挨个读文件标签，几百首就是几百次磁盘解析
            name=item.song.name,
            artist=item.song.artist_text,
            # 记下来源：界面按"当初从哪个歌单下的"给本地音乐分组。
            # 规划对象里就有，不用额外传参——这也是为什么它在这儿取得到。
            source_kind=plan.source.kind,
            source_playlist_id=plan.source.playlist_id,
            source_playlist_name=plan.source.name,
        )
        outcome.downloaded.append(result)
        if progress:
            progress(f"✓ {label} → {result.megabytes:.1f} MB")
        if on_song is not None:
            on_song(
                SongEvent(index, total, item.song, SONG_DONE, size=result.size)
            )
        if on_item is not None:
            on_item(index, total)

    if progress:
        progress(
            f"下载完成：成功 {len(outcome.downloaded)} 首"
            f"（{outcome.total_mb:.1f} MB），失败 {len(outcome.failed)} 首"
        )
    return outcome


def iter_sources(playlists: Iterable, names: Iterable[str] = ()) -> list[SyncSource]:
    """从歌单列表里挑出要同步的源。给了名字就按名字挑，否则全要。"""
    wanted = list(names)
    out: list[SyncSource] = []
    for playlist in playlists:
        if wanted and playlist.name not in wanted:
            continue
        if getattr(playlist, "is_liked", False):
            out.append(SyncSource.liked(playlist.name))
        else:
            out.append(SyncSource.playlist(playlist.id, playlist.name))
    return out


# ──────────────────────────────────────────────────────────────────────
# 写进 iPod（P2）
# ──────────────────────────────────────────────────────────────────────


@dataclass
class IpodSyncOutcome:
    """一次"写进 iPod"的结果。"""

    added: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    playlist_names: list[str] = field(default_factory=list)
    playlist_size: int = 0
    verified: bool = False
    note: str = ""
    bytes_copied: int = 0
    request_count: int = 0

    @property
    def copied_mb(self) -> float:
        return self.bytes_copied / 1048576


def synced_on_device(store: StateStore, library) -> set[int]:
    """**真正**已经在这台设备上的网易云歌曲 ID。

    状态库记的是"我同步过"，但设备可能被还原过、可能换了台设备，
    或者你拿另一台设备做了彩排——那时状态库照样会被写成"已同步"。
    实测踩过：彩排环境把 3 首歌记成已同步，真机同步时被误判成
    "已在设备上"，汇总显示"要写入 0 首"（其实要写 3 首），
    播放列表还被算成 6 个成员（3 个是幽灵条目，最后靠写入器丢弃）。

    所以要用 iPod 上**实际存在的文件位置**核对一遍。
    状态库只是加速，设备才是真相。
    """
    locations = {track.location for track in library.tracks}
    out: set[int] = set()
    for song_id in store.synced_ids():
        record = store.synced_song(song_id)
        if record and record.ipod_location in locations:
            out.add(song_id)
    return out


def _song_ids_by_path(store: StateStore, plan: SyncPlan) -> dict[str, int]:
    """本地缓存文件路径 → 网易云歌曲 ID。

    播放列表条目引用的是 iPod 的 ``db_track_id``，但状态库是按网易云 ID 记的，
    所以要有个反查表。缓存路径里其实也带了 ID（文件名前缀），
    但那属于"实现细节"——用状态库查才是可靠来源。
    """
    out: dict[str, int] = {}
    for item in plan.items:
        cached = store.cached_download(item.song.id)
        if cached is not None:
            out[str(cached.path)] = item.song.id
    return out


def _existing_db_track_id(store: StateStore, song_id: int) -> int | None:
    """已经进过 iPod 的歌，之前分到的 db_track_id。"""
    record = store.synced_song(song_id)
    return record.db_track_id if record else None


def sync_to_ipod(
    client: NcmClient,
    store: StateStore,
    plan: SyncPlan,
    device,
    library,
    *,
    cookie: str = "",
    make_playlist: bool = True,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
    on_item: Callable[[int, int], None] | None = None,
) -> IpodSyncOutcome:
    """把规划里的歌写进 iPod，并按歌单建好同名播放列表。

    流程：拷文件 → 整库重写（顺带建播放列表）→ 读回校验 → 记录状态。

    **曲目和播放列表在同一次写入里完成**——播放列表条目引用的是
    ``db_track_id``，而它在导入规划阶段就已经分配好了，所以没必要写两遍
    （多一遍就是多一个整库重写的风险窗口）。

    ``on_item`` 会把**下载阶段**的逐首进度透传出去（写入阶段的进度走
    ``progress`` 文本流）——界面在下载那一段需要进度条。
    """
    def say(message: str) -> None:
        if progress:
            progress(message)

    outcome = IpodSyncOutcome()

    # 1. 本地必须有文件。还没有的（比如只跑过 plan）就先下下来。
    missing = [
        item for item in plan.to_download
        if store.cached_download(item.song.id) is None
    ]
    if missing:
        say(f"本地还缺 {len(missing)} 首，先下载…")
        download_plan = SyncPlan(source=plan.source, level=plan.level, items=missing)
        execute_downloads(
            client, store, download_plan, _cache_dir_of(store, plan),
            cookie=cookie, progress=say, on_item=on_item,
        )

    song_by_path = _song_ids_by_path(store, plan)
    files = [Path(p) for p in song_by_path]
    if not files:
        say("没有可写入的文件。")
        return outcome

    # 2. 导入规划（不产生副作用）
    from ipod_cli.importer import build_import_plan, execute_import
    from ipod_cli.transcode import transcode_for_import

    import_plan = build_import_plan(
        device, library, files, force=force, allow_transcode=True, progress=say
    )

    # 3. 算播放列表成员：本次要加的 + 已经在设备上的。
    #    这样 *部分同步*（--limit）建出的列表也如实反映"设备上确实有这个歌单的哪些歌"，
    #    而不是假装 246 首都在。
    #
    #    "已经在设备上的"必须拿设备实际内容核对（synced_on_device），
    #    不能只信状态库——彩排跑一次就会把记录写成"已同步"，
    #    真机同步时会被算进列表，变成指向不存在曲目的幽灵条目。
    #    （那种条目最后会被写入器丢掉，但计数会骗人，而且白跑一趟。）
    track_ids: list[int] = []
    for item in import_plan.to_add:
        if item.action == "error":
            continue
        if _looks_like_our_file(item.pc.source_path, song_by_path):
            track_ids.append(item.db_track_id)

    on_device = synced_on_device(store, library)
    already = 0
    for item in plan.items:
        if item.song.id not in on_device:
            continue
        db_id = _existing_db_track_id(store, item.song.id)
        if db_id:
            track_ids.append(db_id)
            already += 1

    extra_playlists = []
    if make_playlist and track_ids:
        from iopenpod.itunesdb_writer import PlaylistInfo

        extra_playlists.append(
            PlaylistInfo(name=plan.source.name, track_ids=track_ids)
        )

    say(
        f"准备写入：新增 {len(import_plan.to_add)} 首，"
        f"播放列表「{plan.source.name}」{len(track_ids)} 首"
        f"（其中 {already} 首已在设备上）"
    )

    if not import_plan.to_add and not extra_playlists:
        say("没有要写入的内容。")
        return outcome

    # 4. 真正写入
    if import_plan.to_add:
        result = execute_import(
            import_plan,
            progress=say,
            transcode=transcode_for_import,
            extra_playlists=extra_playlists or None,
        )
        outcome.added = result.added
        outcome.failed = list(result.failed)
        outcome.bytes_copied = result.bytes_copied
        outcome.verified = result.verified
        outcome.note = result.verification_note
    else:
        # **没有新曲目，但播放列表仍要写**（比如你往网易云歌单里加了歌，
        # 或者只是重跑一次让成员保持最新）。
        #
        # 不能走 execute_import：它在"没有需要导入的新曲目"时会提前返回，
        # 播放列表根本写不进去，而且 verified 还是默认的 False，
        # 于是会报一个假的"读回校验未通过"。实测踩到过。
        from ipod_cli.dbwrite import build_track_infos, write_library

        infos, _ = build_track_infos(import_plan.existing_dicts, progress=say)
        if not infos:
            say("设备上没有可写入的曲目。")
            return outcome
        write_result = write_library(
            device, library, infos,
            progress=say,
            pc_file_paths=None,            # 不动封面库
            extra_playlists=extra_playlists,
        )
        outcome.verified = write_result.verified
        outcome.note = write_result.verification_note
    outcome.request_count = client.request_count
    if extra_playlists:
        outcome.playlist_names = [p.name for p in extra_playlists]
        outcome.playlist_size = len(track_ids)

    # 5. 记录状态。**只记真正写进去的**——没进去的记了，
    #    下次同步会以为"已经有了"而跳过，歌就永远进不去。
    for item in import_plan.to_add:
        if item.action == "error":
            continue
        song_id = song_by_path.get(str(item.pc.source_path))
        if not song_id:
            continue
        store.mark_synced(
            song_id,
            ipod_location=item.ipod_location,
            db_track_id=item.db_track_id,
            name=item.pc.title or "",
            artist=item.pc.artist or "",
            album=item.pc.album or "",
            level=plan.level,
            size=item.pc.size,
        )

    return outcome


def _looks_like_our_file(source_path, song_by_path: dict[str, int]) -> bool:
    return str(source_path) in song_by_path


def _cache_dir_of(store: StateStore, plan: SyncPlan) -> Path:
    """从状态库已有的缓存记录里推断缓存目录。

    规划里已经用了同一批文件，所以随便挑一条记录取其父目录即可。
    一条都没有的话（理论上不会走到这）退回项目默认位置。
    """
    for item in plan.items:
        cached = store.cached_download(item.song.id)
        if cached is not None:
            return cached.path.parent
    return Path(".ncm") / "cache"
