# iOpenPod 源码架构分析

> 仓库：https://github.com/TheRealSavi/iOpenPod （MIT，作者 John Gibbons）
> 本地：`<iOpenPod 源码>`
> 版本：1.68.1 ｜ 394 commits ｜ 140,171 行 Python ｜ 265 个 .py
> 分析日期：2026-09-19

## 一句话定位

跨平台（Win/macOS/Linux）iPod Classic / Mini / Nano 管理工具，**纯 Python 直接读写 iTunesDB 二进制数据库**，不依赖 libgpod。PyQt6 桌面 GUI。

核心差异化：绕开 libgpod 老库的 checksum 问题 —— 自己实现了 HASH58 / HASH72 / HASHAB 三代签名，能安全写 6/7 代 Classic 和现代 Nano 的数据库。

## 技术栈

| 项 | 选型 |
| --- | --- |
| Python | 3.11（`.python-version` 锁定） |
| 包管理 | **uv**（严禁 pip/poetry/conda），`.venv` 本地 |
| GUI | PyQt6 ≥6.9 |
| 构建 | hatchling；打包 PyInstaller（`iOpenPod.spec`）+ Nuitka（dev dep）+ Flatpak |
| 测试 | pytest + pytest-qt |
| Lint/类型 | ruff（line-length 320）+ mypy（仅白名单文件子集） |
| 外部依赖 | ffmpeg/ffprobe（转码）、Chromaprint（声学指纹） |
| 发布 | GitHub Actions（release.yml / flatpak.yml / health.yml）+ PyPI |

## 分层架构（受架构守卫强制约束）

```
__main__.py  (CLI 入口, 只做 argparse + 延迟 import PyQt)
     ↓
application/   应用层 / composition root —— 唯一能访问 runtime 单例的层
     ↓
gui/  ←→  application/jobs.py（后台 worker 线程）  ←→  sync/core/engine.py（SyncEngine 门面）
     ↓                                                        ↓
infrastructure/ 设置持久化 + 主题渲染              sync/ 同步引擎（43 文件）
                                                     ↓
                              itunesdb_parser/ ↔ itunesdb_writer/ ↔ itunesdb_shared/
                              artworkdb_*  /  sqlitedb_writer/  /  podcasts/
                                                     ↓
                                                 device/ 设备层（扫描/写入守卫/虚拟 iPod）
```

### 各包职责

| 包 | 文件数 | 职责 | 关键文件 |
| --- | --- | --- | --- |
| `application/` | 20 | 组合根、AppContext、后台 jobs、同步会话编排、设备身份、设置胶水 | `bootstrap.py` `context.py` `jobs.py` `sync_session.py` `runtime.py` `controllers.py` |
| `gui/` | 56 | PyQt6 表现层 | `app.py`(143KB, MainWindow) `widgets/`(42 个) `styles.py` `theme` 集成 |
| `sync/` | 43 | 同步引擎：差异计算、计划、执行、备份、转码 | `sync_executor.py`(201KB 核心) `fingerprint_diff_engine.py` `pc_library.py` `backup_manager.py` `transcoder.py` `photos.py` `core/engine.py`(类型化门面) |
| `device/` | 35 | 设备探测/身份/写入安全 | `scanner.py`(跨平台 USB 探测) `info.py` `capabilities.py` `authority.py` `write_guard.py` `write_readiness.py` `path_safety.py` `durability.py` `virtual.py`(虚拟 iPod 测试用) `vpd_*.py`(USB VPD 识别: libusb/iokit/linux/windows) |
| `itunesdb_parser/` | 19 | iTunesDB / iTunesCDB 透明解压 + 二进制解析 | `parser.py` `mh*b_parser.py` `forensics.py` `playcounts.py` |
| `itunesdb_writer/` | 16 | 写回 + 签名 | `mhbd_writer.py` `mhit_writer.py` `hash58.py` `hash72.py` `hashab.py` `wasm/` |
| `itunesdb_shared/` | 18 | **声明式字段定义层**，parser/writer 共用 | `field_base.py` `mhit_defs.py` `mhod_defs.py` `constants.py` `device_time.py` `playlist_*.py` |
| `artworkdb_*` | 24 | 封面数据库 + `.ithmb` 图像编解码 | `artwork_writer.py` `ithmb_codecs.py` `rgb565.py` `art_extractor.py` |
| `sqlitedb_writer/` | 9 | Classic 6/7 与 Nano 6/7 的 SQLite 侧库 | `library_writer.py` `extras_writer.py` `genius_writer.py` `cbk_writer.py` |
| `podcasts/` | 9 | 播客订阅/搜索/下载 | `feed_parser.py` `itunes_search.py` `downloader.py` |
| `infrastructure/` | 11 | 设置持久化 + 主题系统 | `settings_*.py` `theme_catalog/renderer/runtime.py` |
| `themes/` | 9 JSON | 主题定义（catppuccin×4 / dune-plover / gravity / northern-lights / orchid / sea-glass） | — |

## 数据流（一次同步）

1. `device/scanner.py` 跨平台找 iPod 挂载点 → `device/info.py` 读 SysInfo/HashInfo → `capabilities.py` 按 family+gen 解析能力
2. `application/sync_session.py` 接管编排（用户选定同步意向后）
3. `sync/pc_library.py` 扫 PC 媒体库；`sync/fingerprint_diff_engine.py` 用 Chromaprint 声学指纹跨重编码匹配曲目
4. `sync/plan_validator.py` + `planning_stages.py` 生成计划 → GUI `syncReview.py` 让用户逐项确认
5. `sync/sync_executor.py` 执行 → `sync/transcoder.py` 转码（FLAC→ALAC，带 `transcode_cache`）
6. `sync/database_commit.py` + `_db_io.py` 落盘（经 `device/write_guard.py` + `durability.py` 保护）→ `itunesdb_writer` 写库 + 打校验和
7. `sync/backup_manager.py` 事前存快照，可回滚

## 架构守卫（`scripts/check_architecture.py`，pre-commit 强制）

这是改造时最容易踩坑的地方。规则（`scripts/architecture_rules.json`）：

- **GUI 禁止直接 import** `iopenpod.sync` / `iopenpod.device` / `iopenpod.podcasts` / `settings`，必须走 `application/` 层。仅 `sync.contracts`、`sync.review_selection` 是公开例外；其余文件级豁免写在 json 里
- **同步编排必须走 `sync/core/engine.py` 的 `SyncEngine` 门面**，不能绕过直接用 `fingerprint_diff_engine` / `sync_executor`
- 禁止访问 `SyncExecutor` 私有成员（`_SyncContext` 等）
- 禁止旧顶层命名空间 import（`GUI`、`SyncEngine`、`iTunesDB_Parser` 等无前缀写法）
- runtime 单例（`DeviceManager`/`iTunesDBCache`）只在 `application/` 层访问；`gui/app.py` 额外禁止直接 `.get_instance()`
- runtime 私有属性（`_device_path`/`_is_loading`/`_user_playlists`）禁止跨模块访问
- `settings_runtime` 只允许 `application/` 引入
- `except Exception: pass` 有**逐文件配额**，超了直接 fail
- 允许的 import 环写在 `allowed_import_cycles` 里（parser 子模块间、device 子模块间）

## 开发命令（全部走 uv）

```bash
uv sync                                   # 装依赖
uv run iopenpod                           # 运行
uv run python scripts/dev.py check        # 全量门禁: lint + types + test + arch
uv run python scripts/dev.py lint|types|test|arch|fmt|doctor
uv run python scripts/dev.py test tests/test_foo.py -k pattern
```

## 改造切入点参考

| 想改什么 | 落点 | 注意 |
| --- | --- | --- |
| UI / 界面 | `gui/widgets/*` + `gui/app.py` | 架构守卫拦直接 import sync/device，需经 application |
| 配色 / 皮肤 | `themes/*.json` + `infrastructure/theme_renderer.py` | 主题渲染器owns色值派生，GUI 只读不解算 |
| 同步行为 | `sync/` + `application/sync_session.py` | 必须走 SyncEngine 门面 |
| 数据库格式 | `itunesdb_*/artworkdb_*/sqlitedb_writer/` | 字段定义改 `*_shared/*_defs.py`，parser+writer 自动跟随 |
| 新机型支持 | `device/capabilities.py` 的 `capabilities_for_family_gen()` | AGENTS.md 说在 `ipod_models.py`——**该文件已不存在，能力查询现在是 `capabilities.py`**（文档过时） |
| 测试 | `tests/`，用 `create_virtual_ipod()` | 绝不可写真 iPod 挂载点 |

## 测试基础设施

- `device/virtual.py` 的 `create_virtual_ipod()` + `available_virtual_ipod_models()` 造虚拟设备（含 SysInfo/HashInfo/序列号/GUID 造假）
- `scripts/generate_fake_music_library.py` 造假音乐库
- `scripts/export_itunesdb_json.py` 导出库结构做取证
- `scripts/ipod_caption_probe.py` 探测 iPod 显示名
- 180+ 个测试文件

## 已知坑 / 注意

1. **Python 版本锁 3.11**——uv 会自动拉；本机全局是 3.13，别用它
2. 长行是刻意的：ruff `line-length = 320`
3. mypy 只检查 `pyproject.toml` 里列的白名单文件，改别的文件不报类型错
4. `docs/agents/AGENTS.md` 说能力查询在 `ipod_models.py`，实际已迁到 `device/capabilities.py`——文档滞后
5. 本项目**不是我的 Remotion 项目**，无 2880×2160/50fps 等约定
6. Windows 下 PyQt6 走 `pyqt6` wheel，无需额外 Qt 环境

## 相关

- [iPod 存储原理与 HASH58 签名](iPod 存储原理与 HASH58 签名.md) —— **原理篇**：iTunesDB 二进制结构、HASH58 算法全流程、
  三代签名对比、封面 ithmb、FAT32 落盘，以及"从大项目裁剪出最小工具"的方法论
- [ipod-cli 精简版 iPod 工具](ipod-cli 精简版 iPod 工具.md) —— 从本仓库裁出的最小 CLI 工具（实际落地的裁剪案例）
