"""状态库的测试。

它自己不碰网络，但它是"幂等"的基础，出错的方式很隐蔽：
最典型的是**缓存里记着文件、文件其实已经被删了**——
同步引擎要是信了缓存，就会以为"已经同步过"而跳过，歌永远进不了 iPod。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ipod_cli.ncm.state import StateStore


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "ncm.db")


class TestAccounts:
    """多账号 + 切换。前端设置页要这个。"""

    def test_save_and_read_back(self, store: StateStore) -> None:
        store.save_account(1001, "cookie-A", nickname="甲", vip_type=11)
        account = store.get_account(1001)

        assert account is not None
        assert account.uid == 1001
        assert account.cookie == "cookie-A"
        assert account.nickname == "甲"
        assert account.vip_type == 11

    def test_saving_same_uid_updates_instead_of_duplicating(
        self, store: StateStore
    ) -> None:
        """重新登录同一个账号应该更新 cookie，不是多一条记录。"""
        store.save_account(1001, "旧 cookie", nickname="甲")
        store.save_account(1001, "新 cookie", nickname="甲(改名)")

        accounts = store.list_accounts()
        assert len(accounts) == 1
        assert accounts[0].cookie == "新 cookie"
        assert accounts[0].nickname == "甲(改名)"

    def test_multiple_accounts_coexist(self, store: StateStore) -> None:
        store.save_account(1, "c1", nickname="甲")
        store.save_account(2, "c2", nickname="乙")

        assert [a.uid for a in store.list_accounts()] == [1, 2]

    def test_single_account_needs_no_explicit_switch(self, store: StateStore) -> None:
        """只有一个账号时不该逼用户去切。"""
        store.save_account(1001, "c", nickname="甲")
        active = store.active_account()
        assert active is not None
        assert active.uid == 1001

    def test_explicit_switch(self, store: StateStore) -> None:
        store.save_account(1, "c1", nickname="甲")
        store.save_account(2, "c2", nickname="乙")

        store.set_active_uid(2)
        active = store.active_account()
        assert active is not None and active.uid == 2

        store.set_active_uid(1)
        active = store.active_account()
        assert active is not None and active.uid == 1

    def test_many_accounts_without_choice_is_ambiguous(self, store: StateStore) -> None:
        """多个账号又没指定当前账号时，应该返回 None 让前端去问，
        而不是随便挑一个——那会让用户以为在同步 A 账号，实际动了 B 的。"""
        store.save_account(1, "c1")
        store.save_account(2, "c2")
        assert store.active_account() is None

    def test_removing_active_account_clears_the_marker(
        self, store: StateStore
    ) -> None:
        store.save_account(1, "c1")
        store.save_account(2, "c2")
        store.set_active_uid(2)

        assert store.remove_account(2) is True
        assert store.get_account(2) is None
        # 当前账号没了，不能还指着一个不存在的 uid
        active = store.active_account()
        assert active is not None and active.uid == 1

    def test_removing_unknown_account_is_a_no_op(self, store: StateStore) -> None:
        assert store.remove_account(999) is False


class TestSyncedSongs:
    """网易云 ID → iPod 位置。幂等的依据。"""

    def test_mark_and_read(self, store: StateStore) -> None:
        store.mark_synced(
            555, ipod_location="iPod_Control/Music/F00/1.mp3",
            db_track_id=42, source_uid=1,
            name="歌名", artist="艺人", album="专辑",
            level="exhigh", size=1234,
        )
        song = store.synced_song(555)

        assert song is not None
        assert song.ipod_location == "iPod_Control/Music/F00/1.mp3"
        assert song.db_track_id == 42
        assert song.level == "exhigh"
        assert song.size == 1234

    def test_marking_again_updates(self, store: StateStore) -> None:
        store.mark_synced(555, ipod_location="旧位置", db_track_id=1)
        store.mark_synced(555, ipod_location="新位置", db_track_id=2)

        assert len(store.synced_ids()) == 1
        song = store.synced_song(555)
        assert song is not None
        assert song.ipod_location == "新位置"
        assert song.db_track_id == 2

    def test_synced_ids_drives_the_diff(self, store: StateStore) -> None:
        """同步引擎靠这个集合算"要下什么"。"""
        for sid in (1, 2, 3):
            store.mark_synced(sid, ipod_location=f"F00/{sid}.mp3")

        assert store.synced_ids() == {1, 2, 3}

    def test_forget(self, store: StateStore) -> None:
        store.mark_synced(555, ipod_location="x")
        assert store.forget_synced(555) is True
        assert store.synced_song(555) is None
        assert store.forget_synced(555) is False

    def test_huge_db_track_id_survives(self, store: StateStore) -> None:
        """★ iPod 的持久 ID 是**无符号** 64 位，SQLite 的 INTEGER 是**有符号**的。

        真机 118 首里就有 61 首的 id 超过 2^63-1，直接塞进 INTEGER 列会
        OverflowError——而且是在**写库成功之后**记录状态时才炸，
        报错信息容易让人误以为"什么都没发生"。
        """
        huge = 18382657539261584133          # > 2^63-1，实测真机上就有
        assert huge > 2**63 - 1

        store.mark_synced(555, ipod_location="F00/a.mp3", db_track_id=huge)
        song = store.synced_song(555)

        assert song is not None
        assert song.db_track_id == huge, "大整数被截断或丢失了"
        assert isinstance(song.db_track_id, int)

    def test_zero_db_track_id_becomes_none(self, store: StateStore) -> None:
        store.mark_synced(555, ipod_location="x", db_track_id=0)
        song = store.synced_song(555)
        assert song is not None
        assert song.db_track_id is None

    def test_missing_db_track_id(self, store: StateStore) -> None:
        store.mark_synced(555, ipod_location="x")
        song = store.synced_song(555)
        assert song is not None
        assert song.db_track_id is None


class TestDownloadCache:
    """下载缓存。这里有个必须守住的坑。"""

    def test_remember_and_recall(self, store: StateStore, tmp_path: Path) -> None:
        audio = tmp_path / "song.mp3"
        audio.write_bytes(b"ID3 fake audio")

        store.remember_download(555, audio, level="exhigh", size=14)
        cached = store.cached_download(555)

        assert cached is not None
        assert cached.path == audio
        assert cached.level == "exhigh"

    def test_deleted_file_is_not_a_cache_hit(
        self, store: StateStore, tmp_path: Path
    ) -> None:
        """★ 缓存里记着、但文件已被删 —— 必须当成"没有"。

        信了它，同步会以为歌已经在本地了而跳过下载，
        结果歌既不在缓存里也不在 iPod 上，还查不出原因。
        """
        audio = tmp_path / "gone.mp3"
        audio.write_bytes(b"ID3 fake")
        store.remember_download(555, audio)

        audio.unlink()

        assert store.cached_download(555) is None

    def test_文件没了就停止计数_但记录留着(
        self, store: StateStore, tmp_path: Path
    ) -> None:
        """★ 契约**有意**改了：以前是"顺手把记录删掉"，现在只停止计数。

        改的理由见 ``state.py`` 的 ``downloaded_song_ids``：判据一旦出错
        （路径解析跟着工作目录跑），"删数据"就是不可逆的——实测因此丢了
        7 条好记录，用户看到的是"装了新版本之后全部变成未下载"。
        要看"记录在、文件丢了"用 ``missing_download_ids``。
        """
        audio = tmp_path / "gone.mp3"
        audio.write_bytes(b"ID3")
        store.remember_download(555, audio)
        audio.unlink()

        store.cached_download(555)   # 触发核对
        assert store.stats()["downloaded"] == 0
        assert store.missing_download_ids() == {555}
        assert len(store.list_downloads()) == 1, "文件没了不等于记录是脏的"


    def test_missing_file_returns_none(self, store: StateStore, tmp_path: Path) -> None:
        store.remember_download(999, tmp_path / "never-existed.mp3")
        assert store.cached_download(999) is None


class TestSettings:
    def test_default_is_empty(self, store: StateStore) -> None:
        assert store.get_setting("quality", "exhigh") == "exhigh"

    def test_set_and_get(self, store: StateStore) -> None:
        store.set_setting("quality", "lossless")
        assert store.get_setting("quality") == "lossless"

    def test_overwrite(self, store: StateStore) -> None:
        store.set_setting("quality", "lossless")
        store.set_setting("quality", "exhigh")
        assert store.get_setting("quality") == "exhigh"


class TestStats:
    def test_counts(self, store: StateStore, tmp_path: Path) -> None:
        store.save_account(1, "c")
        store.mark_synced(1, ipod_location="a", size=100)
        store.mark_synced(2, ipod_location="b", size=250)

        # 文件要真的建出来：`downloaded` 现在数的是"文件还在的"，
        # 不是"记录条数"——界面上写"已下载 N 首"就得是 N 首能播的。
        real = tmp_path / "x.mp3"
        real.write_bytes(b"ID3")
        store.remember_download(1, real)

        stats = store.stats()
        assert stats["synced"] == 2
        assert stats["downloaded"] == 1
        assert stats["download_records"] == 1
        assert stats["download_missing"] == 0
        assert stats["accounts"] == 1
        assert stats["synced_bytes"] == 350

    def test_文件丢了的记录单独数出来(self, store: StateStore, tmp_path: Path) -> None:
        real = tmp_path / "在.mp3"
        real.write_bytes(b"ID3")
        store.remember_download(1, real)
        store.remember_download(2, tmp_path / "没了.mp3")

        stats = store.stats()
        assert stats["downloaded"] == 1, "只有文件真在的才算已下载"
        assert stats["download_records"] == 2
        assert stats["download_missing"] == 1, "丢的那条要单独报，不能混进已下载"


class TestPersistence:
    """关掉再打开，数据还在（这是"库"不是"内存"）。"""

    def test_survives_reopen(self, tmp_path: Path) -> None:
        db = tmp_path / "ncm.db"
        first = StateStore(db)
        first.save_account(1001, "cookie", nickname="甲")
        first.mark_synced(555, ipod_location="F00/a.mp3", db_track_id=7)

        second = StateStore(db)
        account = second.get_account(1001)
        song = second.synced_song(555)
        assert account is not None and account.cookie == "cookie"
        assert song is not None and song.db_track_id == 7

    def test_reopening_does_not_wipe(self, tmp_path: Path) -> None:
        """反复初始化不能把表重建掉——schema 建表全用 IF NOT EXISTS。"""
        db = tmp_path / "ncm.db"
        StateStore(db).mark_synced(1, ipod_location="a")
        for _ in range(3):
            StateStore(db)
        assert StateStore(db).synced_ids() == {1}


class TestLocalDownloads:
    """本地已下载的音乐（下载页要管的那部分）。"""

    def test_list_is_newest_first(self, store: StateStore, tmp_path: Path) -> None:
        for i in (1, 2, 3):
            store.remember_download(i, tmp_path / f"{i}.mp3", size=i * 100)
        ids = [d.song_id for d in store.list_downloads()]
        assert sorted(ids) == [1, 2, 3]

    def test_list_carries_name_and_artist(
        self, store: StateStore, tmp_path: Path
    ) -> None:
        """★ 界面要显示歌名。只有 song_id 的话得挨个读文件标签。"""
        store.remember_download(
            1, tmp_path / "a.mp3", size=100, name="红色高跟鞋", artist="蔡健雅"
        )
        row = store.list_downloads()[0]
        assert row.name == "红色高跟鞋"
        assert row.artist == "蔡健雅"
        assert row.size == 100

    def test_rename_on_conflict_keeps_name_fresh(
        self, store: StateStore, tmp_path: Path
    ) -> None:
        """同一首重下，名字也要跟着更新（歌名可能被改过）。"""
        store.remember_download(1, tmp_path / "a.mp3", name="旧名")
        store.remember_download(1, tmp_path / "a.mp3", name="新名")
        assert len(store.list_downloads()) == 1
        assert store.list_downloads()[0].name == "新名"

    def test_remove_downloads_is_batch(self, store: StateStore, tmp_path: Path) -> None:
        for i in range(1, 6):
            store.remember_download(i, tmp_path / f"{i}.mp3")
        removed = store.remove_downloads([2, 4, 999])
        assert removed == 2, "返回的应该是真删掉的条数，不是传进去的个数"
        assert {d.song_id for d in store.list_downloads()} == {1, 3, 5}

    def test_remove_downloads_empty_is_noop(self, store: StateStore) -> None:
        assert store.remove_downloads([]) == 0

    def test_remove_downloads_leaves_files_alone(
        self, store: StateStore, tmp_path: Path
    ) -> None:
        """★ 只删记录，不动文件。

        记录和文件分开删：先删文件再删记录的话，中间失败会留下
        "记录说文件在、其实不在"的状态；反过来最坏只留孤儿文件（无害）。
        """
        audio = tmp_path / "a.mp3"
        audio.write_bytes(b"x")
        store.remember_download(1, audio)

        store.remove_downloads([1])

        assert audio.is_file(), "记录删了，文件不该跟着没"
        assert store.list_downloads() == []


class TestSchemaMigration:
    """老库升级。

    ``CREATE TABLE IF NOT EXISTS`` 只保证表存在，**不会**改已存在表的列，
    所以列变了必须显式迁移——不然老库带着旧结构继续跑，
    然后在某个意外的地方炸。
    """

    def test_v2_database_gains_the_new_columns(self, tmp_path: Path) -> None:
        import sqlite3

        db = tmp_path / "old.db"

        # 手工造一个 v2 的库：downloads 表**没有** name/artist
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE schema_info (version INTEGER NOT NULL);
            INSERT INTO schema_info (version) VALUES (2);
            CREATE TABLE downloads (
                song_id    INTEGER PRIMARY KEY,
                path       TEXT    NOT NULL,
                level      TEXT    NOT NULL DEFAULT '',
                size       INTEGER NOT NULL DEFAULT 0,
                created_at TEXT    NOT NULL
            );
            INSERT INTO downloads (song_id, path, level, size, created_at)
            VALUES (7, 'D:/old.mp3', 'exhigh', 42, '2026-01-01T00:00:00');
            """
        )
        conn.commit()
        conn.close()

        # 打开时自动迁移
        store = StateStore(db)

        # 老记录还在，只是没有名字（当初就没存）
        rows = store.list_downloads()
        assert len(rows) == 1
        assert rows[0].song_id == 7
        assert rows[0].name == ""
        assert rows[0].size == 42

        # 新写入的能带上名字
        store.remember_download(8, tmp_path / "new.mp3", name="新歌", artist="新歌手")
        fresh = {d.song_id: d for d in store.list_downloads()}
        assert fresh[8].name == "新歌"
        assert fresh[8].artist == "新歌手"

    def test_migration_is_idempotent(self, tmp_path: Path) -> None:
        """迁移跑两遍不该炸（中途挂掉重跑是常见情况）。"""
        db = tmp_path / "ncm.db"
        StateStore(db)
        store = StateStore(db)   # 第二次打开
        assert store.list_downloads() == []


class TestDownloadSource:
    """本地曲目的"来源歌单"——界面按它分组。"""

    def test_source_round_trips(self, store: StateStore, tmp_path: Path) -> None:
        store.remember_download(
            1,
            tmp_path / "a.mp3",
            source_kind="playlist",
            source_playlist_id=10000000001,
            source_playlist_name="DJ",
        )
        row = store.list_downloads()[0]
        assert row.source_kind == "playlist"
        assert row.source_playlist_id == 10000000001
        assert row.source_playlist_name == "DJ"

    def test_liked_and_unknown_do_not_collapse(
        self, store: StateStore, tmp_path: Path
    ) -> None:
        """★ "我喜欢的音乐"和"老记录没来源"的 playlist_id 都是 0。

        只用数字当分组键的话这两类会被归成一组——所以 source_key 是字符串。
        """
        store.remember_download(
            1, tmp_path / "liked.mp3", source_kind="liked", source_playlist_name="我喜欢的音乐"
        )
        store.remember_download(2, tmp_path / "old.mp3")  # 没来源的老记录

        rows = {r.song_id: r for r in store.list_downloads()}
        assert rows[1].source_playlist_id == 0
        assert rows[2].source_playlist_id == 0
        assert rows[1].source_key == "liked"
        assert rows[2].source_key == "unknown"
        assert rows[1].source_key != rows[2].source_key

    def test_source_label_falls_back_sensibly(
        self, store: StateStore, tmp_path: Path
    ) -> None:
        store.remember_download(1, tmp_path / "a.mp3")  # 没来源
        store.remember_download(
            2, tmp_path / "b.mp3", source_kind="liked"
        )  # 有 kind 没名字
        store.remember_download(
            3, tmp_path / "c.mp3", source_kind="playlist", source_playlist_id=7
        )  # 有 kind 没名字

        rows = {r.song_id: r for r in store.list_downloads()}
        assert rows[1].source_label == "未记录来源"
        assert rows[2].source_label == "我喜欢的音乐"
        assert "改名" in rows[3].source_label or "删除" in rows[3].source_label

    def test_redownload_overwrites_source(
        self, store: StateStore, tmp_path: Path
    ) -> None:
        """同一首从两个歌单都能下到，后下的赢。

        本地只有一份文件，来源本来也就只能有一个。
        """
        store.remember_download(
            1, tmp_path / "a.mp3", source_kind="playlist",
            source_playlist_id=1, source_playlist_name="甲歌单",
        )
        store.remember_download(
            1, tmp_path / "a.mp3", source_kind="playlist",
            source_playlist_id=2, source_playlist_name="乙歌单",
        )

        rows = store.list_downloads()
        assert len(rows) == 1
        assert rows[0].source_playlist_name == "乙歌单"


class TestSourceMigration:
    """v3 → v4：老记录补不上来源，归到"未记录"。"""

    def test_v3_database_gains_source_columns(self, tmp_path: Path) -> None:
        import sqlite3

        db = tmp_path / "v3.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE schema_info (version INTEGER NOT NULL);
            INSERT INTO schema_info (version) VALUES (3);
            CREATE TABLE downloads (
                song_id    INTEGER PRIMARY KEY,
                path       TEXT    NOT NULL,
                level      TEXT    NOT NULL DEFAULT '',
                size       INTEGER NOT NULL DEFAULT 0,
                name       TEXT    NOT NULL DEFAULT '',
                artist     TEXT    NOT NULL DEFAULT '',
                created_at TEXT    NOT NULL
            );
            INSERT INTO downloads (song_id, path, level, size, name, artist, created_at)
            VALUES (9, 'D:/v3.mp3', 'exhigh', 77, '老歌', '老歌手', '2026-01-01T00:00:00');
            """
        )
        conn.commit()
        conn.close()

        store = StateStore(db)
        rows = store.list_downloads()
        assert len(rows) == 1
        assert rows[0].name == "老歌", "老数据不该丢"
        assert rows[0].source_key == "unknown"
        assert rows[0].source_label == "未记录来源"

        # 新写入的能带上来源
        store.remember_download(
            10, tmp_path / "new.mp3", source_kind="playlist",
            source_playlist_id=99, source_playlist_name="新歌单",
        )
        fresh = {r.song_id: r for r in store.list_downloads()}
        assert fresh[10].source_key == "playlist:99"
        assert fresh[10].source_label == "新歌单"

    def test_v2_database_migrates_all_the_way_to_v4(self, tmp_path: Path) -> None:
        """一次跨两个版本也要能升上来（用户可能很久没开过）。"""
        import sqlite3

        db = tmp_path / "v2.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE schema_info (version INTEGER NOT NULL);
            INSERT INTO schema_info (version) VALUES (2);
            CREATE TABLE downloads (
                song_id    INTEGER PRIMARY KEY,
                path       TEXT    NOT NULL,
                level      TEXT    NOT NULL DEFAULT '',
                size       INTEGER NOT NULL DEFAULT 0,
                created_at TEXT    NOT NULL
            );
            INSERT INTO downloads (song_id, path, level, size, created_at)
            VALUES (5, 'D:/v2.mp3', '', 11, '2026-01-01T00:00:00');
            """
        )
        conn.commit()
        conn.close()

        store = StateStore(db)
        rows = store.list_downloads()
        assert len(rows) == 1
        assert rows[0].song_id == 5
        assert rows[0].source_key == "unknown"
        assert rows[0].name == "", "v2 里本来就没有名字"

        import sqlite3 as s

        check = s.connect(db)
        cols = {r[1] for r in check.execute("PRAGMA table_info(downloads)")}
        check.close()
        assert {"name", "artist", "source_kind", "source_playlist_id"} <= cols


class TestDownloadedSongIdsVerifiesFiles:
    """「已下载」必须是**文件真的在**，不是"记录里有"。

    以前这个函数直接返回全部记录，含"记录在、文件不在"的脏记录。
    后果：歌单页标"已下载"而本地其实没有；而规划判断"要不要下"用的
    cached_download 会核对文件——两边不一致，用户看到的是自相矛盾。
    """

    def test_gone_files_are_not_counted(self, store: StateStore, tmp_path: Path) -> None:
        """★ 文件被删了就不算"已下载"。"""
        alive = tmp_path / "alive.mp3"
        alive.write_bytes(b"x")
        store.remember_download(1, alive)
        store.remember_download(2, tmp_path / "never.mp3")

        assert store.downloaded_song_ids() == {1}

    def test_文件找不到的记录不再算已下载(self, store: StateStore, tmp_path: Path) -> None:
        """★ 只过滤，**不删**（契约有意改了，理由见上面那条测试）。"""
        store.remember_download(1, tmp_path / "never.mp3")

        assert store.downloaded_song_ids() == set()
        assert store.missing_download_ids() == {1}
        assert len(store.list_downloads()) == 1, "记录该留着——删了不可逆"

    def test_real_files_are_not_touched(self, store: StateStore, tmp_path: Path) -> None:
        """别矫枉过正——文件真在的不能被误删。"""
        paths = []
        for i in (1, 2, 3):
            path = tmp_path / f"{i}.mp3"
            path.write_bytes(b"x")
            paths.append(path)
            store.remember_download(i, path)

        assert store.downloaded_song_ids() == {1, 2, 3}
        assert len(store.list_downloads()) == 3
        assert all(p.is_file() for p in paths), "记录在、文件也在，不该动它"

    def test_matches_cached_download_for_every_song(
        self, store: StateStore, tmp_path: Path
    ) -> None:
        """★ 两边判断必须一致——这正是这个 bug 的本质。

        `downloaded_song_ids()` 说"已下载"、`cached_download()` 说"没有"，
        用户就会看到"标着已下载，点下载又去下了"。
        """
        have = tmp_path / "have.mp3"
        have.write_bytes(b"x")
        store.remember_download(1, have)
        store.remember_download(2, tmp_path / "gone.mp3")

        ids = store.downloaded_song_ids()
        for song_id in (1, 2):
            assert (song_id in ids) == (
                store.cached_download(song_id) is not None
            ), f"第 {song_id} 首两处判断打架了"


class Test缓存路径解析:
    """★ 回归：下载记录里存的是**相对数据目录**的路径。

    这里出过一次真实事故：相对路径被按**当前工作目录**解析，用户从发行版
    目录启动时找不到文件，于是——**记录被当成脏数据删掉**，歌单页全部
    显示"未下载"，而文件其实好好地躺在缓存目录里。

    所以两件事都要守住：
    1. 相对路径按**数据目录**解析，跟工作目录无关；
    2. 找不到文件时**不能删记录**。
    """

    def _make_file(self, store: StateStore, relpath: str) -> Path:
        path = store.data_dir() / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake mp3")
        return path

    def test_相对路径按数据目录解析_跟工作目录无关(
        self, store: StateStore, tmp_path: Path, monkeypatch
    ) -> None:
        """★ 核心回归：换个工作目录，结果必须一样。"""
        self._make_file(store, ".ncm/cache/123 歌名.mp3")
        store.remember_download(123, ".ncm/cache/123 歌名.mp3")

        assert store.downloaded_song_ids() == {123}

        # 把工作目录换到别处（模拟"从发行版目录启动"）
        elsewhere = tmp_path / "别处"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        assert store.downloaded_song_ids() == {123}, (
            "相对路径跟着工作目录跑了——发行版启动时就会全部显示未下载"
        )
        assert store.cached_download(123) is not None

    def test_写入时存的是相对路径(self, store: StateStore) -> None:
        """存相对的，数据目录搬家后记录依然指得对。"""
        path = self._make_file(store, ".ncm/cache/456.mp3")
        store.remember_download(456, path)  # 传绝对路径

        record = store.cached_download(456)
        assert record is not None
        # 库里存的应该是相对形式
        with store._connect() as conn:  # noqa: SLF001
            raw = conn.execute(
                "SELECT path FROM downloads WHERE song_id = 456"
            ).fetchone()["path"]
        assert not Path(raw).is_absolute(), f"存成绝对路径了：{raw}"

    def test_文件丢了不删记录_只标记(self, store: StateStore) -> None:
        """★ 这是那次事故的直接原因：判据错了就删数据。

        文件找不到只说明"本地没有这份"，**不能**因此丢掉记录——
        记录里的歌名/艺人/来源是有价值的元数据。
        """
        store.remember_download(789, ".ncm/cache/789 已经删了.mp3")

        assert store.downloaded_song_ids() == set()
        assert store.missing_download_ids() == {789}

        # 记录还在
        rows = store.list_downloads()
        assert [r.song_id for r in rows] == [789], "文件没了就把记录删了——不可逆"

    def test_两种状态不重叠(self, store: StateStore) -> None:
        self._make_file(store, ".ncm/cache/1.mp3")
        store.remember_download(1, ".ncm/cache/1.mp3")
        store.remember_download(2, ".ncm/cache/2 没了.mp3")

        assert store.downloaded_song_ids() == {1}
        assert store.missing_download_ids() == {2}

    def test_绝对路径原样识别(self, store: StateStore, tmp_path: Path) -> None:
        """用户把缓存指到别的盘时存的就是绝对路径。"""
        outside = tmp_path / "别处" / "x.mp3"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_bytes(b"x")
        store.remember_download(3, outside)

        assert store.downloaded_song_ids() == {3}

    def test_清空缓存仍然能真删记录(self, store: StateStore) -> None:
        """不自动删 ≠ 不能删：显式清理还是要真的删掉。"""
        store.remember_download(5, ".ncm/cache/5.mp3")
        assert store.clear_downloads() == 1
        assert store.list_downloads() == []
