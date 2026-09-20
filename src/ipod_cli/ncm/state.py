"""同步状态库（SQLite）。

## 为什么需要它

iPod 的 iTunesDB 里**没有"网易云歌曲 ID"这个字段**——所以"这首歌是不是
已经同步过了"没法问设备。只能本地记一份映射。

它解决三件事：

1. **多账号**：前端要能切换账号，每个账号的 cookie 分开存。
2. **幂等**：网易云歌曲 ID → iPod 上的位置。重复跑同步不会重复导入，
   也不会因为"换个账号看同一首歌"就再下一遍。
3. **下载缓存**：已经下到本地的文件不用重下。

## 放在哪

默认 `.ncm/ncm.db`（项目根下），已被 `.gitignore` 排除——**里面有 cookie**。

## 关于删除

状态库丢了不会破坏 iPod，最坏是"重新匹配一遍"。同步引擎的设计前提是
**状态库只是加速，iPod 才是真相**：判断"这首歌在不在设备上"时，状态库
命中之后仍会核对文件确实存在。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

#: 状态库默认位置（相对项目根）
DEFAULT_DB_RELPATH = Path(".ncm") / "ncm.db"

SCHEMA_VERSION = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER NOT NULL
);

-- 多账号：扫码登录拿到的 cookie 按 uid 存，前端可切换
CREATE TABLE IF NOT EXISTS accounts (
    uid        INTEGER PRIMARY KEY,
    nickname   TEXT NOT NULL DEFAULT '',
    cookie     TEXT NOT NULL,
    vip_type   INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT    NOT NULL
);

-- 当前生效的账号（单行表，id 恒为 1）
CREATE TABLE IF NOT EXISTS active_account (
    id  INTEGER PRIMARY KEY CHECK (id = 1),
    uid INTEGER
);

-- 歌曲 ID 是网易云全局的，跟账号无关，所以主键只有 song_id。
-- source_uid 只作记录用（"这首歌当初是哪个账号带过来的"）。
--
-- db_track_id 存成 TEXT：iPod 的持久 ID 是**随机 64 位无符号整数**，
-- 而 SQLite 的 INTEGER 是**有符号** 64 位——真机上 118 首里就有 61 首超过
-- 2^63-1，直接塞进去会 OverflowError。这个值我们只做"原样传回写入器"，
-- 从不参与算术，所以字符串存最省事也最不容易出错。
CREATE TABLE IF NOT EXISTS synced_songs (
    song_id       INTEGER PRIMARY KEY,
    source_uid    INTEGER,
    name          TEXT NOT NULL DEFAULT '',
    artist        TEXT NOT NULL DEFAULT '',
    album         TEXT NOT NULL DEFAULT '',
    ipod_location TEXT NOT NULL DEFAULT '',
    db_track_id   TEXT,
    level         TEXT NOT NULL DEFAULT '',
    size          INTEGER NOT NULL DEFAULT 0,
    synced_at     TEXT NOT NULL
);

-- 下载缓存：已经下到本地的原始文件（还没进 iPod，或进了也留着做备份）
--
-- name/artist 是**为了界面**存的：下载页要列出"电脑上已下载的音乐"，
-- 只有 song_id 和路径的话，界面得挨个读文件标签才能显示歌名——
-- 几百首就是几百次磁盘解析。下载的时候顺手记下来，两边都省事。
--
-- source_kind / source_playlist_id / source_playlist_name 记的是
-- **这首歌当初是从哪儿下的**——界面要按来源歌单分组显示本地音乐。
-- kind 用 'liked' / 'playlist' / ''（''=老记录，下载的时候还没记这个）。
--
-- 三列分开存而不是只存个名字：名字会变（歌单可以改名），id 不会。
-- 按 id 归组、用名字显示，改完名刷新一下就对了。
CREATE TABLE IF NOT EXISTS downloads (
    song_id              INTEGER PRIMARY KEY,
    path                 TEXT    NOT NULL,
    level                TEXT    NOT NULL DEFAULT '',
    size                 INTEGER NOT NULL DEFAULT 0,
    name                 TEXT    NOT NULL DEFAULT '',
    artist               TEXT    NOT NULL DEFAULT '',
    source_kind          TEXT    NOT NULL DEFAULT '',
    source_playlist_id   INTEGER NOT NULL DEFAULT 0,
    source_playlist_name TEXT    NOT NULL DEFAULT '',
    created_at           TEXT    NOT NULL
);

-- 设置（音质档位等）
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Account:
    """一个登录过的账号。"""

    uid: int
    nickname: str
    cookie: str
    vip_type: int


@dataclass(frozen=True)
class SyncedSong:
    """一首"已经进过 iPod"的歌。"""

    song_id: int
    source_uid: int | None
    name: str
    artist: str
    album: str
    ipod_location: str
    db_track_id: int | None
    level: str
    size: int
    synced_at: str


def _row_to_synced_song(row) -> SyncedSong:
    """数据库行 → ``SyncedSong``。

    ``db_track_id`` 在库里是字符串（无符号 64 位放不进 SQLite 的 INTEGER），
    这里转回整数给调用方——写入器要的就是整数。
    """
    data = dict(row)
    raw = data.get("db_track_id")
    try:
        data["db_track_id"] = int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        data["db_track_id"] = None
    return SyncedSong(**data)


@dataclass(frozen=True)
class DownloadedFile:
    """一首"已经下到本地"的歌。"""

    song_id: int
    path: Path
    level: str
    size: int

    #: 下面三个是给界面用的。老库里的记录没有名字（那时候没存），
    #: 会是空字符串——界面显示成"（未知曲目）"，重新下一次就有了。
    name: str = ""
    artist: str = ""
    created_at: str = ""

    #: 当初从哪儿下的。``source_kind`` 是 'liked' / 'playlist' /
    #: ''（老记录，下载时还没记这个）。界面按这个分组。
    source_kind: str = ""
    source_playlist_id: int = 0
    source_playlist_name: str = ""

    @property
    def source_key(self) -> str:
        """分组的稳定标识。**字符串**，避开 playlist_id=0 的歧义。

        "我喜欢的音乐"的 playlist_id 也是 0，跟"老记录没来源"撞在一起。
        只用数字做键的话，这两类会被归成一组。
        """
        if self.source_kind == "liked":
            return "liked"
        if self.source_kind == "playlist":
            return f"playlist:{self.source_playlist_id}"
        return "unknown"

    @property
    def source_label(self) -> str:
        """给界面显示的名字。"""
        if self.source_kind == "liked":
            return self.source_playlist_name or "我喜欢的音乐"
        if self.source_kind == "playlist":
            return self.source_playlist_name or "（歌单已改名或删除）"
        return "未记录来源"


class StateStore:
    """状态库。

    每次操作开一个新连接——SQLite 在 Windows 上跨线程共享连接会出问题
    （前端是 FastAPI，会有多线程），而这里的调用频率极低，开销可以忽略。
    """

    def __init__(self, path: Path | str = DEFAULT_DB_RELPATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            row = conn.execute("SELECT version FROM schema_info").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_info (version) VALUES (?)", (SCHEMA_VERSION,)
                )
                return
            self._migrate(conn, int(row["version"]))

    def _migrate(self, conn: sqlite3.Connection, from_version: int) -> None:
        """按版本补齐结构变更。

        ``CREATE TABLE IF NOT EXISTS`` 只保证表存在，**不会**改已存在表的列。
        所以列类型/约束变了必须显式迁移，否则老库会带着旧结构继续跑，
        然后在某个意外的地方炸（``db_track_id`` 溢出就是这么来的）。
        """
        if from_version >= SCHEMA_VERSION:
            return

        if from_version < 2:
            # v1 → v2：synced_songs.db_track_id 从 INTEGER 改成 TEXT。
            # iPod 的持久 ID 是无符号 64 位，塞不进 SQLite 的有符号 INTEGER。
            columns = {
                r["name"]: (r["type"] or "").upper()
                for r in conn.execute("PRAGMA table_info(synced_songs)")
            }
            if columns.get("db_track_id") == "INTEGER":
                # 结构不对就重建。这一版发布时还没有真实的同步记录，
                # 所以直接丢数据是安全的；真有数据的话这里得做搬迁。
                conn.execute("DROP TABLE synced_songs")
                conn.executescript(_SCHEMA)

        if from_version < 3:
            # v2 → v3：downloads 加 name/artist，界面要显示歌名。
            #
            # 老库里的记录补不上名字（当初就没存），只能是空字符串——
            # 界面会显示成"（未知曲目）"。重新下一次就有了，不值得为它
            # 去逐首读标签。加 PRAGMA 检查是为了幂等：万一某次迁移
            # 跑到一半挂了，重跑不会因为"列已存在"而整个失败。
            have = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(downloads)")
            }
            for column in ("name", "artist"):
                if column not in have:
                    conn.execute(
                        f"ALTER TABLE downloads ADD COLUMN {column} "
                        "TEXT NOT NULL DEFAULT ''"
                    )

        if from_version < 4:
            # v3 → v4：downloads 记下"从哪个歌单下的"。
            #
            # 老记录补不上（当初没记），保持 ''/0——界面会归到
            # "未记录来源"那一组。用户看得到、也说得清，比瞎猜一个来源好。
            have = {r["name"] for r in conn.execute("PRAGMA table_info(downloads)")}
            for column, decl in (
                ("source_kind", "TEXT NOT NULL DEFAULT ''"),
                ("source_playlist_id", "INTEGER NOT NULL DEFAULT 0"),
                ("source_playlist_name", "TEXT NOT NULL DEFAULT ''"),
            ):
                if column not in have:
                    conn.execute(
                        f"ALTER TABLE downloads ADD COLUMN {column} {decl}"
                    )

        conn.execute("UPDATE schema_info SET version = ?", (SCHEMA_VERSION,))

    # ── 账号 ──────────────────────────────────────────────────────────

    def save_account(
        self, uid: int, cookie: str, *, nickname: str = "", vip_type: int = 0
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO accounts (uid, nickname, cookie, vip_type, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(uid) DO UPDATE SET
                    nickname   = excluded.nickname,
                    cookie     = excluded.cookie,
                    vip_type   = excluded.vip_type,
                    updated_at = excluded.updated_at
                """,
                (uid, nickname, cookie, vip_type, _now()),
            )

    def list_accounts(self) -> list[Account]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT uid, nickname, cookie, vip_type FROM accounts ORDER BY uid"
            ).fetchall()
        return [Account(**dict(r)) for r in rows]

    def get_account(self, uid: int) -> Account | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT uid, nickname, cookie, vip_type FROM accounts WHERE uid = ?",
                (uid,),
            ).fetchone()
        return Account(**dict(row)) if row else None

    def remove_account(self, uid: int) -> bool:
        """删掉一个账号。如果它正好是当前账号，顺手清掉当前标记。"""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM accounts WHERE uid = ?", (uid,))
            if cur.rowcount:
                conn.execute(
                    "UPDATE active_account SET uid = NULL WHERE uid = ?", (uid,)
                )
        return bool(cur.rowcount)

    def set_active_uid(self, uid: int | None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO active_account (id, uid) VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET uid = excluded.uid
                """,
                (uid,),
            )

    def active_account(self) -> Account | None:
        """当前生效的账号。没设过就退回"唯一那个账号"（只有多账号才需要切）。"""
        with self._connect() as conn:
            row = conn.execute("SELECT uid FROM active_account WHERE id = 1").fetchone()
        uid = row["uid"] if row else None
        if uid is None:
            accounts = self.list_accounts()
            return accounts[0] if len(accounts) == 1 else None
        return self.get_account(uid)

    # ── 已同步到 iPod 的歌 ───────────────────────────────────────────

    def mark_synced(
        self,
        song_id: int,
        *,
        ipod_location: str,
        db_track_id: int | None = None,
        source_uid: int | None = None,
        name: str = "",
        artist: str = "",
        album: str = "",
        level: str = "",
        size: int = 0,
    ) -> None:
        # 存成字符串：iPod 的持久 ID 是无符号 64 位，超出 SQLite 的有符号范围
        # （见建表注释）。读的时候再转回来。
        stored_id = str(db_track_id) if db_track_id else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO synced_songs (
                    song_id, source_uid, name, artist, album,
                    ipod_location, db_track_id, level, size, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(song_id) DO UPDATE SET
                    source_uid    = excluded.source_uid,
                    name          = excluded.name,
                    artist        = excluded.artist,
                    album         = excluded.album,
                    ipod_location = excluded.ipod_location,
                    db_track_id   = excluded.db_track_id,
                    level         = excluded.level,
                    size          = excluded.size,
                    synced_at     = excluded.synced_at
                """,
                (
                    song_id, source_uid, name, artist, album,
                    ipod_location, stored_id, level, size, _now(),
                ),
            )

    def synced_song(self, song_id: int) -> SyncedSong | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM synced_songs WHERE song_id = ?", (song_id,)
            ).fetchone()
        if row is None:
            return None
        return _row_to_synced_song(row)

    def synced_ids(self) -> set[int]:
        """所有"已同步"的歌曲 ID。同步引擎靠它算 diff。"""
        with self._connect() as conn:
            rows = conn.execute("SELECT song_id FROM synced_songs").fetchall()
        return {r["song_id"] for r in rows}

    def forget_synced(self, song_id: int) -> bool:
        """把一首歌从"已同步"里划掉（从 iPod 删掉之后要调）。"""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM synced_songs WHERE song_id = ?", (song_id,))
        return bool(cur.rowcount)

    # ── 下载缓存 ─────────────────────────────────────────────────────

    def remember_download(
        self,
        song_id: int,
        path: Path | str,
        *,
        level: str = "",
        size: int = 0,
        name: str = "",
        artist: str = "",
        source_kind: str = "",
        source_playlist_id: int = 0,
        source_playlist_name: str = "",
    ) -> None:
        """记一条下载缓存。

        ``name`` / ``artist`` 记下歌名和艺人（搜索、日志、排查时用）。
        只传 song_id 的话界面得挨个读文件标签，几百首就是几百次磁盘解析——
        下载的时候顺手记下来，两边都省事。

        ``source_*`` 记的是"当初从哪个歌单下的"，界面按它分组。
        **重下同一首会覆盖来源**——同一首歌从两个歌单都能下到，
        后下的那个赢。这没问题：本地只有一份文件，来源本来也就只能有一个。
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO downloads
                    (song_id, path, level, size, name, artist,
                     source_kind, source_playlist_id, source_playlist_name,
                     created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(song_id) DO UPDATE SET
                    path                 = excluded.path,
                    level                = excluded.level,
                    size                 = excluded.size,
                    name                 = excluded.name,
                    artist               = excluded.artist,
                    source_kind          = excluded.source_kind,
                    source_playlist_id   = excluded.source_playlist_id,
                    source_playlist_name = excluded.source_playlist_name,
                    created_at           = excluded.created_at
                """,
                (
                    song_id,
                    str(path),
                    level,
                    size,
                    name,
                    artist,
                    source_kind,
                    source_playlist_id,
                    source_playlist_name,
                    _now(),
                ),
            )

    def cached_download(self, song_id: int) -> DownloadedFile | None:
        """取下载缓存——**会核对文件真的还在**。

        缓存里记着但文件被删了（比如手动清理过），那就是没有，不能拿它糊弄。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM downloads WHERE song_id = ?", (song_id,)
            ).fetchone()
        if row is None:
            return None
        path = Path(row["path"])
        if not path.is_file():
            self.forget_download(song_id)
            return None
        return DownloadedFile(
            song_id=row["song_id"], path=path,
            level=row["level"], size=row["size"],
        )

    def forget_download(self, song_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM downloads WHERE song_id = ?", (song_id,))
        return bool(cur.rowcount)

    def count_downloads(self) -> int:
        """下载记录条数。和缓存目录里的实际文件数比对，
        不一致说明缓存被外部动过（手删了文件、或从别处拷来）。"""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM downloads").fetchone()
        return int(row["n"]) if row else 0

    def downloaded_song_ids(self) -> set[int]:
        """所有"本地缓存里**确实有文件**"的歌 ID 集合。

        给歌单列表用：界面要标出每首歌"已下载/未下载"，逐首调
        ``cached_download`` 就是 N 次查询——6000 首的歌单会卡住界面。
        一次查全更省事，反正这个集合本来就要全量。

        ★ 以前这里直接返回全部记录，**含"记录在、文件不在"的脏记录**
        （注释还说这是故意的，让 ``cached_download`` 去自我修正）。但后果是：
        歌单页标着"已下载"、本地其实没有；而规划判断"要不要下"用的是
        ``cached_download``（会核对文件）。两边不一致，用户看到的就是
        自相矛盾——"它说已下载，怎么又去下了／怎么点下载没反应"。

        现在逐条核对，并把脏记录**顺手清掉**（自愈）：
        一次 SELECT 拿路径、逐个 stat，最后一次性 DELETE 收尾。

        代价是 N 次 stat，N = **已下载的曲目数**（几百），不是歌单长度
        （几千）——可以接受。这也让"已下载"这个说法重新变得可信。
        """
        with self._connect() as conn:
            rows = conn.execute("SELECT song_id, path FROM downloads").fetchall()

        alive: set[int] = set()
        stale: list[int] = []
        for row in rows:
            song_id = int(row["song_id"])
            if Path(row["path"]).is_file():
                alive.add(song_id)
            else:
                stale.append(song_id)

        if stale:
            # 一次性删除，不在循环里逐条开事务
            self.remove_downloads(stale)

        return alive

    def list_downloads(self) -> list[DownloadedFile]:
        """所有下载记录，**新的在前**。

        不在这里核对文件是否还在（那是 ``cached_download`` 的活）——
        列表类接口逐条 stat 几百个文件会很慢，而且要的是"记录里有什么"。
        真删文件之前仍然走 ``cached_download``，那里会核对。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM downloads ORDER BY created_at DESC, song_id DESC"
            ).fetchall()
        return [
            DownloadedFile(
                song_id=row["song_id"],
                path=Path(row["path"]),
                level=row["level"],
                size=row["size"],
                name=row["name"],
                artist=row["artist"],
                created_at=row["created_at"],
                source_kind=row["source_kind"],
                source_playlist_id=row["source_playlist_id"],
                source_playlist_name=row["source_playlist_name"],
            )
            for row in rows
        ]

    def remove_downloads(self, song_ids: Iterable[int]) -> int:
        """批量删下载记录，返回删掉几条。**不动磁盘上的文件。**

        记录和文件要分开删：先删文件再删记录的话，中间失败会留下
        "记录说文件在、其实不在"的状态；先删记录再删文件，最坏是
        留下孤儿文件（无害，下次清缓存会带走）。跟 remover 是同一套道理。
        """
        ids = [int(i) for i in song_ids]
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        with self._connect() as conn:
            cur = conn.execute(
                f"DELETE FROM downloads WHERE song_id IN ({marks})", ids
            )
        return int(cur.rowcount)

    def clear_downloads(self) -> int:
        """清空下载记录，返回清掉多少条。

        清缓存时必须连记录一起清：只删文件的话记录还在，
        下次 `cached_download` 才会发现文件没了并自我修正——中间那段时间
        界面会显示"已下载"，而文件其实不在，属于自欺欺人。
        """
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM downloads")
        return int(cur.rowcount)

    # ── 设置 ────────────────────────────────────────────────────────

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def get_setting(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    # ── 统计（前端要显示） ──────────────────────────────────────────

    def stats(self) -> dict[str, int]:
        with self._connect() as conn:
            synced = conn.execute("SELECT COUNT(*) AS n FROM synced_songs").fetchone()["n"]
            downloaded = conn.execute("SELECT COUNT(*) AS n FROM downloads").fetchone()["n"]
            accounts = conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]
            total_size = conn.execute(
                "SELECT COALESCE(SUM(size), 0) AS n FROM synced_songs"
            ).fetchone()["n"]
        return {
            "synced": synced,
            "downloaded": downloaded,
            "accounts": accounts,
            "synced_bytes": total_size,
        }
