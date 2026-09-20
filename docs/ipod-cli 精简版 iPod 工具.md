# ipod-cli（iPod Classic 导入导出工具）

> 仓库：`<仓库根>`
> 内核来源：[iOpenPod](https://github.com/TheRealSavi/iOpenPod)（MIT，commit a202424 / v1.68.1）
> 更新：2026-09-19 ｜ **132 项测试全通过 ｜ 已在真机验收**
> 规模：132 文件 / 35,059 行（自写 ~4,100 行 + 搬运内核 22,526 行 + 测试）
> 十个命令：doctor / info / list / import / export / **remove** / **verify** /
> **backup** / **restore** / **rename**

## 一句话

给 iPod Classic 做歌曲导入导出，中文命令行界面。从 iOpenPod 裁出可复用内核，
自己写 2,600 行 CLI 包上去——不从零重写。

## 我的真机（已验收）

| 项 | 值 |
| --- | --- |
| 机型 | iPod Classic **6th Gen 80GB Silver** |
| 型号 | **MB029** |
| 序列号 | TESTSERIAL01 |
| FireWire GUID | `0x000A270000000001` |
| 签名方案 | **HASH58** |
| 设备名 | 我的 iPod |
| 容量 | 79.8 GB（可用 78.5 GB） |
| 曲目 | 116 → **118**（测试导入后） |
| 播放列表 | 普通 2 + 智能 5 + 播客 1 |

## 设备与关键结论

**目标机型：A1238** = iPod Classic **6th Gen (2007) / 6.5th Gen (2008 Rev A) / 7th Gen (2009 Rev B/C)**，
80GB / 120GB / 160GB，三个版本处理方式完全一致。

这是**阻力最小的一档**，因为：

| 检查项 | Classic (A1238) | 对比 |
| --- | --- | --- |
| 签名方案 | **HASH58** | 只需 SysInfo 里的 FireWire GUID，设备自带 |
| 要不要 HashInfo 文件 | 不要 | Nano 5G 需要（要先用 iTunes 同步生成）→ 硬门槛 |
| 要不要写 SQLite 库 | 不要 | Nano 5G/6G/7G 需要 → 省掉 2,171 行 |
| 要不要 iTunesCDB 压缩 | 不要 | Nano 5G+ 专属 |

签名方案速查（iOpenPod 能力表）：

| 机型 | 签名 | 前置条件 | 能自足 |
| --- | --- | --- | --- |
| 1G–5G / Mini / Photo / Nano 1–2G | 无 | — | ✅ |
| Classic 全代 / Nano 3–4G | HASH58 | FireWire GUID（设备自带） | ✅ |
| Nano 5G | HASH72 | 必须先被 iTunes 同步一次生成 HashInfo | ⚠️ |
| Nano 6–7G | HASHAB | FireWire GUID + wasmtime + 13KB wasm | ✅ |

## 量化：为什么不从零写

| 方案 | 文件 | 行数 |
| --- | --- | --- |
| iOpenPod 全量 | 265 | 140,171 |
| 天真复用（import 就拖） | 94 | 34,030 |
| **实际裁剪结果** | **124** | **32,669** |

**为什么"天真复用"会翻倍**：`device/__init__.py` 会把整个 device 层（31 文件 16,193 行）
全导出来，还顺带拖进 `artworkdb_writer` 和 `sync/transcoder`。

从零重写看似只要 2,500–3,500 行，但要从头实现七种 chunk 的二进制布局、
三代签名、FAT32 落盘安全——这正是 iOpenPod 作者用几百次提交和 163 个测试踩平的坑，
而第一次写入就可能把 iPod 数据库写坏。

## 架构

```
src/iopenpod/      内核（原样搬运，仅 2 个文件被改）
  itunesdb_shared/   字段定义层（parser/writer 共用）
  itunesdb_parser/   解析 + load_ipod_library 高层读库
  itunesdb_writer/   写库 + HASH58/72/AB 签名
  artworkdb_*/       封面库 + .ithmb 编解码
  device/            设备表/能力表/SysInfo/FAT32 安全写入
  sync/              _track_conversion（读↔写模型转换）
                     _playlist_builder（播放列表重建）★ 真机后补搬
                     path_identity / spl_evaluator（智能列表规则求值）

src/ipod_cli/      自写部分（2,627 行）
  discovery.py   设备识别（纯文件系统，不走 USB 硬件探测）
  library.py     读库（保留原始字典以便重写保真）
  mediafile.py   mutagen 读标签
  importer.py    导入
  exporter.py    导出
  transcode.py   ffmpeg 转码
  cli.py         info/list/import/export/doctor

tools/             开发辅助（不属于交付物）
  build_rehearsal.py  搭"真机数据彩排环境"
  check_rehearsal.py  写入后逐项核对
  dress_rehearsal.py  完整彩排
  wait_for_ipod.py    等设备插上
  closure.py / find_missing_imports.py  裁剪时的分析器
```

被改动的 2 个内核文件：`device/__init__.py`（收敛导出）、`__init__.py`（去掉 infrastructure 依赖）。
`sync/__init__.py` 是自写的（原来被裁掉了，补搬后重建）。其余全部原样，方便从上游 cherry-pick。

## 与内核交互的三个关键点（都踩过）

### 1. 设备必须注册到内核，否则写不了封面

内核封面代码走 `get_current_device_for_path(路径)` 取设备；没注册就认为"设备未知"并
**拒绝写封面**（这个设计是对的——宁可不写也不猜错二进制格式）。
`IpodDevice.activate()` 必须在大批量写入前调用。
`set_current_device()` 内部会校验能否选出唯一安全写入档，所以 `DeviceInfo` 必须带 SysInfo 的 `model_number`。

### 2. 重写数据库是整库覆盖，必须保全已有曲目

`write_itunesdb()` 不是增量，是整库重写。正确链路：

```
load_ipod_library() 拿扁平字典
  → track_dict_to_info() 把已有曲目转回写模型
  → 拼上新曲目 → 整库写回
```

这两个函数是配对的：`load_ipod_library` 返回的键（`Title`/`Location`/`Artist`）
正好是 `track_dict_to_info` 期望的形状。测试里有专门一条守着这个场景。

### 3. ★ 还必须重建播放列表，否则连 iPod 名字都被改

**这是真机上才发现的**，见下一节。

## 四个静默数据损坏/丢失缺陷

### 读代码自查发现的两个（虚拟测试测不出）

1. **没转码器时会把 FLAC 字节拷成 .m4a** —— 原逻辑 `if 转码器 else 原样拷贝`
   会让 FLAC 落进 else 分支，产出"文件名像 AAC、内容是 FLAC"的坏文件。
   改成：有转码需求但没转码器时直接报错，绝不拷贝。
2. **没封面时在 Artwork 目录留 `.iop-*.tmp` 残留** —— 内核的原子写入用
   "临时文件+改名"，没有封面可写时临时文件没人清理。
   改成：只在确实有封面时启用封面链路 + 兜底清扫。

### ★ 真机测试暴露的两个（虚拟设备根本不可能测出）

#### ① 播放列表被整库重写抹掉

**现象**：真机导入 2 首歌后，116 首曲目和封面全好，但——

| | 写入前 | 写入后 |
| --- | --- | --- |
| 主播放列表名 | **我的 iPod** | **iPod** |
| 用户播放列表 | On-The-Go 2 | **丢失** |
| 智能播放列表 | 5 个 | **全部丢失** |

**根因**：`write_itunesdb()` 是整库重写。不传 `playlists` 参数就只写一个默认主播放列表
——名字默认叫 "iPod"。而 **iPod 的名字本身就存在主播放列表标题里**，所以连设备名都被改了。
**整个过程不报错。**

**为什么虚拟测试抓不到**：`create_virtual_ipod()` 造的是空库，本来就没有播放列表，
"丢失"无从体现。测试只覆盖了"曲目不丢"，没覆盖"播放列表不丢"。

**修法**：补搬 `sync/_playlist_builder.py` + `path_identity.py` + `spl_evaluator.py`，
导入前用 `build_and_evaluate_playlists()` 从原库重建播放列表，把返回的 7 元组传给
`write_itunesdb()`。重建失败时**拒绝写入**而不是退回默认值。

新增 `tests/test_playlist_preservation.py`（7 项）守着，故意用真机数据的形状。

#### ② `--playlist` 导出筛选从未生效

**现象**：`ipod export --playlist 某歌单` 永远返回空。

**根因**：播放列表条目用 `track_id`（数据库内的**位置序号**）引用曲目，
代码却按 `db_track_id` 查找。两者是完全不同的东西，顺序一变就串。

**修法**：优先用 `track_persistent_id`（不受顺序变动影响），退回 `track_id`。

### 教训

> **虚拟设备能验证代码逻辑，验证不了"你漏做了什么"。**
> 前者是"我写的代码对不对"，后者是"我没想到要写什么"。
> 两个真机缺陷都属于后者，只能靠**写入后逐项核对**发现——不是靠"导入成功"的提示。

## 封面：一个反直觉的发现

真机有 241 条封面条目，写入后被重写成 117 条（去重：241 → 95 个唯一图 → 117 条按曲目条目）。
**条目数变了但封面没丢。**

写入器的解析顺序：
1. 先按 `song_id`（= `db_track_id`，重写前后**稳定不变**）查找已有关联
2. 再退回 `mhii_link` / `artwork_id_ref`

所以判据不是"条目数是否一致"，而是**逐曲检查是否有悬空引用**。
真机验证：117/118 有可用封面，**悬空 0**。

## 三个"事后补的"命令（都是当时临时写脚本干的事）

真机测试时我手写了 4 遍核对脚本、手工 `cp -r` 做备份、为了删掉 2 首测试曲只能
来回滚备份。这三件事本该是命令，所以补齐了。

### verify —— 健康检查（8 项）

设备识别 / 数据库文件头 / 可读性 / 签名 / 曲目字段 / 文件对应 / 封面 / 播放列表 / 备份。
分 `✅ 通过 / ⚠ 提醒 / ❌ 失败` 三档，失败时退出码为 3（脚本可判）。

写这个时发现一个**真盲区**：解析器对垃圾文件是**宽容**的——遇到不认识的 chunk
会跳过，于是全是垃圾的 `iTunesDB` 也能"成功解析"成 0 首曲目、报告一片绿。
所以加了一条**文件头校验**（`mhbd` 魔数），不看内容只看头 4 字节。

**判据要点**：封面不能看条目数。真机 241 条 → 写入后 117 条是正常去重。
真正的判据是**逐曲检查悬空引用数**。

### backup / restore —— 整机备份

* 备份 `Device` + `iTunes` + `Artwork` **三块**（不含 Music 约 60MB）
* `manifest.json` 记逐文件 SHA256，**还原前先校验**（备份坏了要在写设备之前发现）
* **还原前先另存当前状态**——还原本身也应该是可逆的
* 默认拒绝覆盖非空目录

诚实限制：不含 Music 的备份**不还原音频文件**，所以备份后删过的歌还原回来会
"有记录没文件"。CLI 会提前警告，`verify` 能检出。实测警告的预言和检出完全吻合。

### remove —— 删除曲目

* **先写库、后删文件**。反过来写库失败会留下"有记录没文件"的坏状态
  （iPod 显示却播不出来）；先写库最坏只留孤儿文件（无害）
* **默认不动封面库**（`pc_file_paths=None`）——剩余曲目封面引用原样有效，零风险
* **播放列表幽灵条目自动清理**：重建机制会从最终曲目清单算出合法集合并丢弃无效条目，
  不需要单独写逻辑
* 硬性要求筛选条件；拒绝清空整个曲库

### 顺带重构：dbwrite.py

`import` 和 `remove` 方向相反但写入阶段完全一样，抽出 `write_library()` 共用。
`importer.py` 501 → 417 行，修一处即修两处。

`pc_file_paths` 三种传法的含义：

| 传什么 | 行为 |
| --- | --- |
| `{id: PC路径}` | 为这些曲目写封面，其余曲目已有封面**保留** |
| `None` | **完全不动 ArtworkDB**（删除用，最安全） |
| `{}` | 重写封面库（去重收孤儿，用于 `--compact-artwork`） |

## 数据安全设计

- **写前自动备份**：每次写库生成 `iTunesDB.backup`（内核自带）
- **写后读回校验**：重读数据库核对曲目数，对不上报错
- **FAT32 刷盘**：`write_itunesdb` 内置 `durable_replace` + `flush_written_file`
- **原子写入**：临时文件 + 改名
- **内核的两道写入前保险**（很有用）：
  1. `inspect_device_write_readiness()` 拒绝"不是独立挂载卷"的路径——
     防止"iPod 没挂载成功，结果往一个空目录写一堆东西"
  2. 物理 iPod 必须是 FAT/HFS，NTFS 拒绝
- **测试全在虚拟 iPod 上跑**：`create_virtual_ipod()`，绝不碰真机

### 备份范围（踩过坑）

**只备份 `iTunesDB` 是不够的。** 写入会同时重写 `Artwork/ArtworkDB` 和 `.ithmb`，
只还原数据库会让封面引用 100% 悬空（实测过）。

**正确做法：备份整个 `iPod_Control/`**（不含 Music 约 60MB）。

## 中文处理

- iTunesDB 里字符串是 UTF-16LE，中文天然安全；测试覆盖 emoji、全角标点、引号、制表符
- CSV 清单用 `utf-8-sig`（带 BOM），否则 Excel 打开乱码
- 终端对齐：中日韩字符占两格，用按显示宽度截断避免列错位
- 导出文件名剔除 Windows 非法字符、避开保留名（CON/PRN/...）

## 环境坑（本机特有）

**PYTHONPATH 被设成 Hermes 自己的 venv site-packages**，排在项目 venv 之前会遮蔽依赖，
表现为"Python 3.12 装了 cp311 的 numpy"这种诡异错误。
跑这个项目用 `env -u PYTHONPATH uv run ...` 或先 `unset PYTHONPATH`。

项目钉 Python 3.11（`.python-version`），与内核代码目标一致。

## 常用命令

```bash
uv run pytest tests/ -q                    # 132 项测试
uv run ruff check src/ipod_cli tests tools

# 看
uv run ipod doctor                         # 环境+设备自检
uv run ipod info                           # 型号/序列号/签名方案/容量
uv run ipod list --by-album                # 列曲目
uv run ipod verify                         # ★ 健康检查（8 项）

# 改
uv run ipod import ~/Music --dry-run       # 先预览
uv run ipod import ~/Music -y
uv run ipod export ~/备份 --csv 清单.csv
uv run ipod remove --artist "测试" --dry-run   # 删除（必须先筛选）
uv run ipod rename "铁头的 iPod"

# 保命
uv run ipod backup ~/iPod备份              # 备份整个 iPod_Control
uv run ipod restore ~/iPod备份 -y          # 还原（先另存当前状态）

# 用真机备份搭彩排环境（不碰真机）
uv run python tools/rehearsal_from_backup.py <备份目录> <彩排目录>
uv run python tools/build_rehearsal.py D:/ <彩排目录>   # 从真机搭
uv run python tools/check_rehearsal.py <彩排目录>
```

## 真机写入流程（已验证有效）

1. **备份整个 `iPod_Control/`** 到电脑
2. `ipod doctor` → 确认签名方案是 HASH58
3. `ipod list` → 确认能读出已有曲目
4. 彩排：`tools/build_rehearsal.py` → `tools/check_rehearsal.py`（真机数据 + 虚拟标记，不碰真机）
5. `ipod import --dry-run` → 看计划
6. 真机写入
7. **逐项核对**：曲目数、播放列表数、iPod 名字、封面悬空数、文件孤儿数、
   签名方案、逐字段比对原有曲目
8. 看设备屏幕确认固件接受

第 7 步最关键——两个真机缺陷都是靠它发现的。

## 相关

- [网易云 → iPod 同步方案](网易云 → iPod 同步方案.md) —— **下一步方向**：调研结论、档位陷阱、运维坑
- [iPod 存储原理与 HASH58 签名](iPod 存储原理与 HASH58 签名.md) —— **原理篇**：iTunesDB 二进制结构、HASH58 算法全流程、
  三代签名对比、封面 ithmb、FAT32 落盘，以及从大项目裁剪的方法论
- [iOpenPod 源码架构](iOpenPod 源码架构.md) —— 上游项目的完整框架分析
