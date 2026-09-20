"""数据目录解析与迁移的测试。

这一组测试守的是**"用户数据不会凭空消失"**：嵌入模式下状态库要从数据目录推导，
而用户以前的登录态和下载记录都在仓库根的 `.ncm/` 里。推导错了、或者迁移写错了，
表现都是"打开应用发现没登录了"——用户只会认为数据丢了。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ipod_web import paths


class TestDataDirFromEnv:
    """环境变量读取：空串和没设是一回事。"""

    def test_none_when_unset(self) -> None:
        assert paths.data_dir_from_env({}) is None

    @pytest.mark.parametrize("value", ["", "   ", "\t"])
    def test_blank_counts_as_unset(self, value: str) -> None:
        """空白字符串当没设——不然宿主传个空串就会把数据写进当前目录。"""
        assert paths.data_dir_from_env({paths.DATA_DIR_ENV: value}) is None

    def test_reads_value(self, tmp_path: Path) -> None:
        got = paths.data_dir_from_env({paths.DATA_DIR_ENV: str(tmp_path)})
        assert got == tmp_path

    def test_expands_user(self) -> None:
        """宿主可能传 `~`，得展开。"""
        got = paths.data_dir_from_env({paths.DATA_DIR_ENV: "~/某个目录"})
        assert got is not None
        assert "~" not in str(got)


class TestDbAndCache:
    """布局：状态库和缓存都在 `<数据目录>/.ncm/` 下。

    保留 `.ncm/` 这一层是有意的——"备份数据目录"和"备份仓库里的 `.ncm/`"
    就变成同一件事，文档和用户习惯都不用改。
    """

    def test_layout(self, tmp_path: Path) -> None:
        db, cache = paths.db_and_cache(tmp_path)
        assert db == tmp_path / ".ncm" / "ncm.db"
        assert cache == tmp_path / ".ncm" / "cache"

    def test_both_under_data_dir(self, tmp_path: Path) -> None:
        """两个路径都必须在数据目录**底下**——不然就是两份数据散在两处。"""
        db, cache = paths.db_and_cache(tmp_path)
        for p in (db, cache):
            assert tmp_path in p.parents


class TestMigration:
    """迁移：只在目标不存在时复制，**永远不删源**。"""

    def _make_db(self, path: Path, marker: str = "旧数据") -> Path:
        """造一个真 SQLite 库（不是空文件——空文件分不出"迁移成功"和"复制了个寂寞"）。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.execute("INSERT INTO t VALUES (?)", (marker,))
        conn.commit()
        conn.close()
        return path

    def test_copies_when_target_missing(self, tmp_path: Path) -> None:
        old = self._make_db(tmp_path / "旧" / ".ncm" / "ncm.db")
        target = tmp_path / "新" / ".ncm" / "ncm.db"

        assert paths.migrate_if_needed(target, [old]) is True
        assert target.is_file()
        # 内容真搬过来了
        conn = sqlite3.connect(target)
        assert conn.execute("SELECT v FROM t").fetchone()[0] == "旧数据"
        conn.close()

    def test_source_is_never_deleted(self, tmp_path: Path) -> None:
        """★ 源文件必须还在。

        复制而不是移动：万一新位置后来不可用（权限、路径变更），
        移动的话两头都没有了。这类"看起来只是搬个家"的操作，丢了代价不对等。
        """
        old = self._make_db(tmp_path / "旧" / ".ncm" / "ncm.db")
        paths.migrate_if_needed(tmp_path / "新" / ".ncm" / "ncm.db", [old])
        assert old.is_file(), "迁移把源文件删了——这是不可恢复的数据丢失"

    def test_does_not_overwrite_existing_target(self, tmp_path: Path) -> None:
        """★ 目标已存在就**绝不动它**。

        否则是另一种数据丢失：在新位置已经用了一阵子（新下载、新登录），
        旧位置的残留又把新记录顶掉。
        """
        old = self._make_db(tmp_path / "旧" / ".ncm" / "ncm.db", "旧数据")
        target = self._make_db(tmp_path / "新" / ".ncm" / "ncm.db", "新数据")

        assert paths.migrate_if_needed(target, [old]) is False
        conn = sqlite3.connect(target)
        assert conn.execute("SELECT v FROM t").fetchone()[0] == "新数据"
        conn.close()

    def test_uses_first_existing_candidate(self, tmp_path: Path) -> None:
        """候选按优先级取第一个存在的。"""
        first = self._make_db(tmp_path / "一" / ".ncm" / "ncm.db", "第一个")
        second = self._make_db(tmp_path / "二" / ".ncm" / "ncm.db", "第二个")
        target = tmp_path / "新" / ".ncm" / "ncm.db"

        assert paths.migrate_if_needed(target, [first, second]) is True
        conn = sqlite3.connect(target)
        assert conn.execute("SELECT v FROM t").fetchone()[0] == "第一个"
        conn.close()

    def test_skips_missing_candidates(self, tmp_path: Path) -> None:
        """前面的候选不存在就往后找，不是直接放弃。"""
        target = tmp_path / "新" / ".ncm" / "ncm.db"
        real = self._make_db(tmp_path / "真" / ".ncm" / "ncm.db")
        assert paths.migrate_if_needed(
            target, [tmp_path / "没有" / "ncm.db", real]
        ) is True
        assert target.is_file()

    def test_nothing_found_is_not_an_error(self, tmp_path: Path) -> None:
        """全都找不到 → 返回 False，不抛异常。

        找不到最多是重新扫码登录一次，**不该让应用起不来**。
        """
        target = tmp_path / "新" / ".ncm" / "ncm.db"
        assert paths.migrate_if_needed(target, [tmp_path / "没有" / "ncm.db"]) is False
        assert not target.exists()

    def test_empty_candidates(self, tmp_path: Path) -> None:
        assert paths.migrate_if_needed(tmp_path / "x.db", []) is False


class TestMigrationCandidates:
    """候选位置：从近到远，去重、可解析。"""

    def test_includes_cwd_and_data_dir_parent(self, tmp_path: Path) -> None:
        cwd = tmp_path / "工作目录"
        cwd.mkdir()
        data_dir = tmp_path / "数据" / "data"
        got = paths.migration_candidates(data_dir, cwd=cwd)
        assert (cwd / ".ncm" / "ncm.db") in got
        assert (data_dir.parent / ".ncm" / "ncm.db") in got

    def test_no_duplicates(self, tmp_path: Path) -> None:
        """cwd 和数据目录同级时不能出现重复项（重复只会白读一遍盘）。"""
        got = paths.migration_candidates(tmp_path / "数据", cwd=tmp_path / "数据")
        keys = [str(p).lower() for p in got]
        assert len(keys) == len(set(keys))

    def test_all_resolved(self, tmp_path: Path) -> None:
        """必须是绝对路径——相对路径在嵌入环境里毫无意义（cwd 不可靠）。"""
        for p in paths.migration_candidates(tmp_path / "数据", cwd=tmp_path):
            assert p.is_absolute()
