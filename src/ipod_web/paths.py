"""运行时数据目录的解析与迁移。

## 为什么要单独管这件事

以前所有路径都是**相对当前工作目录**推的（`StateStore()` → `.ncm/ncm.db`）。那时后端
总是从仓库根启动，cwd 就是仓库根，看不出问题。

嵌入到 Flutter 应用里之后，cwd 变得**不可靠**——实测 `SeriousPython.run()` 里那句
`Directory.current = <app-support>/data` 并不等于 Python 看到的 `os.getcwd()`
（原因在宿主侧，不是我们能修的）。押注 cwd 的结果是：换一种启动方式，用户的登录态
和下载记录就"凭空消失"——而用户只会认为**数据丢了**。

所以这里改成**显式一个变量**：

    IPOD_MANAGER_DATA_DIR

由宿主（Flutter 侧用 `getApplicationSupportDirectory()` 解析）传进来，Python 从它
推导出状态库、缓存、日志的位置。没设这个变量时，行为跟以前**完全一样**（相对 cwd 的
`.ncm/`），所以命令行用法一个字符都不用改。

## 为什么迁移是「复制」而不是「移动」

老位置的 `.ncm/` 里是登录 cookie + 下载记录 + 作业日程。移动的话，万一新位置后来
不可用（权限、路径变更），数据就两头都没有了。复制最多留一份冗余，**永远不会造成
不可恢复的丢失**——这类"看起来只是搬个家"的操作，一旦丢了就是用户要重新扫码登录、
重新下载 40 GB，代价完全不对等。
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger("ipod_web")

#: 环境变量名。宿主传这个，其余路径全部由它推导。
DATA_DIR_ENV = "IPOD_MANAGER_DATA_DIR"

#: 相对数据目录的固定布局
_NCM = ".ncm"
_DB_NAME = "ncm.db"
_CACHE = "cache"


def data_dir_from_env(environ: dict[str, str] | None = None) -> Path | None:
    """读环境变量。没设或为空 → ``None``（表示"用老行为"）。"""
    env = os.environ if environ is None else environ
    raw = (env.get(DATA_DIR_ENV) or "").strip()
    return Path(raw).expanduser() if raw else None


def db_and_cache(data_dir: Path) -> tuple[Path, Path]:
    """数据目录 → (状态库路径, 缓存目录)。

    状态库放 `<数据目录>/.ncm/ncm.db`：**保留 `.ncm/` 这一层**是有意的，
    这样"把整个数据目录拷走/备份"和"把仓库里的 `.ncm/` 拷走"是同一件事，
    文档和习惯都不用改。
    """
    root = data_dir / _NCM
    return root / _DB_NAME, root / _CACHE


def log_file(data_dir: Path) -> Path:
    """嵌入模式的后端日志文件 → `<数据目录>/.ncm/logs/backend-YYYYMMDD.log`。

    **为什么嵌入模式也要写文件**：命令行启动时后端日志就在终端里，一眼能看到；
    嵌进去之后 stdout 只进界面的调试面板，而"事后排查"（尤其是应用已经关掉、
    或者用户来报障）就没有东西可看了。写一份文件是这类黑箱程序的最低要求。
    """
    import datetime

    today = datetime.date.today().strftime("%Y%m%d")
    return data_dir / _NCM / "logs" / f"backend-{today}.log"


def migrate_if_needed(target_db: Path, candidates: list[Path]) -> bool:
    """目标库不存在时，从候选位置**复制**一份过来。

    ``candidates`` 按优先级给：谁存在就用谁。返回是否真的迁移了。

    只在**目标不存在**时动手——已经在新位置跑过的用户，不会被旧位置的残留
    覆盖回去（那是另一种形式的数据丢失：新下的记录被旧的顶掉）。
    """
    if target_db.exists():
        return False

    for old in candidates:
        if not old.is_file():
            continue
        try:
            target_db.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old, target_db)
        except OSError as exc:
            log.warning("迁移状态库失败（%s → %s）：%s", old, target_db, exc)
            continue
        log.info("已把状态库从旧位置迁移过来：%s → %s", old, target_db)
        return True

    return False


def migration_candidates(data_dir: Path, cwd: Path | None = None) -> list[Path]:
    """去哪找用户的旧 `.ncm/`。

    从近到远：

    1. **当前工作目录**——命令行跑过 `uv run ipod-web` 的话就在这儿，
       这是一般情况
    2. **数据目录的同级**——万一宿主把数据目录放在 `<某处>/data`，
       而用户原来把 `.ncm/` 放在 `<某处>/`
    3. **可执行文件所在目录**——发行版解压后直接双击启动的情况

    全是**只读**的探测，找不到就算了（大不了重新登录一次）。
    """
    roots = [cwd or Path.cwd(), data_dir.parent, Path.cwd()]
    try:
        import sys

        if getattr(sys, "executable", None):
            roots.append(Path(sys.executable).parent)
    except Exception:  # noqa: BLE001 - 探测失败不该影响启动
        pass

    out: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        cand = (root / _NCM / _DB_NAME).resolve()
        key = str(cand).lower()
        if key not in seen:
            seen.add(key)
            out.append(cand)
    return out
