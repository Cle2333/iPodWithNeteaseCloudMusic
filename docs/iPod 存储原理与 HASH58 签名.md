# iPod 存储原理与 HASH58 签名

> 技术参考笔记。相关项目：[ipod-cli 精简版 iPod 工具](ipod-cli 精简版 iPod 工具.md)
> 所有偏移、常量、类型号均从 iOpenPod 源码（`src/iopenpod/itunesdb_shared/`）核对得出，非推测。
> 整理日期：2026-09-19 ｜ **已在真机（iPod Classic 6th Gen 80GB Silver / MB029）验收通过**
> 第六、八章含真机实测才发现的结论（封面 song_id 解析顺序、播放列表丢失陷阱）

## 目录

- [一、目录布局](#一目录布局)
- [二、iTunesDB 二进制结构](#二itunesdb-二进制结构)
- [三、为什么不能直接拷文件](#三为什么不能直接拷文件)
- [四、HASH58 签名（核心）](#四hash58-签名核心)
- [五、三代签名对比](#五三代签名对比)
- [六、封面：ArtworkDB 与 ithmb](#六封面artworkdb-与-ithmb)
- [七、FAT32 落盘这一道坑](#七fat32-落盘这一道坑)
- [八、裁剪方法论（从 14 万行裁到 3 千行）](#八裁剪方法论从-14-万行裁到-3-千行)
- [九、ipod-cli 功能清单](#九ipod-cli-功能清单)
- [十、已知边界](#十已知边界)

---

## 一、目录布局

插上 iPod，看到的是一个普通 U 盘。里面：

```
iPod_Control/
├── Music/F00 … F49/        ← 音频文件本体（50 个目录轮流放）
├── iTunes/iTunesDB         ← ★ 核心数据库，二进制
├── Artwork/ArtworkDB       ← 封面数据库
├── Artwork/F1055_1.ithmb   ← 封面像素数据（一种尺寸档一个文件）
├── Device/SysInfo          ← 设备标识（型号、序列号、FireWire GUID）
└── Device/HashInfo         ← 签名密钥材料（Nano 5G 才必需）
```

**关键认知**：音频文件本身没有文件名语义。真实文件名形如 `X7SY.m4a`、`DUKI.mp3`
（4 位随机大写字母数字）。歌名、艺人、专辑、时长、码率、播放次数、评分
**全都在 `iTunesDB` 里**，与文件靠一条冒号分隔的路径字符串关联：

```
:iPod_Control:Music:F00:X7SY.m4a
```

---

## 二、iTunesDB 二进制结构

chunk 嵌套格式。每个 chunk 开头 4 字节是 ASCII 魔数（小写），后面跟 header 长度、总长度。

```
mhbd  (数据库头, 244 字节 = 0xF4)
├── mhsd type=1 → mhlt (曲目表) → mhit × N (每首歌一条)
│                                    └── mhod × M (每首歌的字符串字段)
├── mhsd type=2 → mhlp (播放列表) → mhyp × N → mhip × M (列表项)
├── mhsd type=3 → mhlp_podcast (播客列表)
├── mhsd type=4 → mhla (专辑表) → mhia (专辑条目)
├── mhsd type=5 → mhlp_smart (智能播放列表)
└── mhsd type=6/10 → 空 mhlt 占位（iTunes 也会写，用途未知）
```

**约束**：type=3 的 mhsd 必须夹在 type=1 和 type=2 之间，固件按顺序找。

### 2.1 MHBD 头关键偏移

| 偏移 | 大小 | 字段 | 说明 |
| --- | --- | --- | --- |
| 0x00 | 4 | 魔数 | `mhbd` |
| 0x0C | 4 | `compressed` | iTunesCDB 压缩标志（Classic 不用） |
| 0x10 | 4 | `version` | 数据库版本（Classic 6G+ 是 0x30） |
| 0x18 | 8 | `db_id` | ← **签名前要清零** |
| 0x30 | 2 | `hashing_scheme` | ← **签名方案**（Classic=1） |
| 0x32 | 20 | `unk0x32` | 未知，但**参与签名**，签名前要清零 |
| 0x48 | 8 | `db_persistent_id` | |
| 0x58 | 20 | `hash58` | ← **HASH58 签名值写在这** |
| 0x6C | 4 | `timezone_offset` | |
| 0x70 | 2 | `hash_type_indicator` | |
| 0x72 | 46 | `hash72` | Nano 5G 用 |
| 0xAB | 57 | `hashab` | Nano 6/7G 用 |

### 2.2 MHSD

header 96 字节，`dataset_type` 在 0x0C。其下只有一个子 chunk（`mhlt`/`mhlp`/`mhla`/`mhli`）。

### 2.3 MHIT（单曲记录）

现代头 **624 字节（0x270）**。老固件更小，写入时要按 `db_version` 选对头长度，否则
固件找不到子 mhod：

| db_version | MHIT 头 |
| --- | --- |
| ≤ 0x12（iTunes 7 之前） | 156 (0x9C) |
| ≤ 0x19（iTunes 7.x） | 328 (0x148) |
| ≤ 0x2D（iTunes 9.x） | 504 (0x1F8) |
| 其它（iTunes 10+ / 现代） | 624 (0x270) |

关键字段：`track_id` @0x10、`child_count` @0x0C、`db_track_id`。
子 `mhod` 挂在 `children` 里。

### 2.4 MHOD（字符串字段）

字符串类型的 mhod 用 **24 字节（0x18）子头**，`mhod_type` 在 0x0C，载荷是 **UTF-16LE**。

| type | 字段 | type | 字段 |
| --- | --- | --- | --- |
| 1 | 标题 Title | 14 | Description |
| **2** | **路径 Location** ← 关联文件 | 15 | 播客 enclosure URL |
| 3 | 专辑 Album | 16 | 播客 RSS URL |
| 4 | 艺人 Artist | 17 | 章节数据（大端 atom 树） |
| 5 | 流派 Genre | 18 | 副标题 |
| 6 | 文件类型 Filetype | 19 | 节目名 |
| 7 | EQ 设置 | 20 | 剧集 ID |
| 8 | 注释 Comment | 21 | 网络名 |
| 9 | 分类 Category | 22 | **专辑艺人 Album Artist** |
| 10 | 歌词 Lyrics | 23 | 排序艺人 |
| 12 | 作曲 Composer | 24 | 关键词 |
| 13 | Grouping | 27–31 | 各种排序字段 |

播放列表侧另有：50/51（智能播放列表数据/规则）、52/53（列表索引/跳转表）、
55（属性 plist）、100（列宽排序）、102（列表设置）；专辑侧 200–204；
艺人侧 300。

**中文为什么安全**：载荷是 UTF-16LE，所有非 ASCII 字符天然支持，不涉及任何代码页转换。

---

## 三、为什么不能直接拷文件

**因为固件不认文件系统，只认 iTunesDB。**

- 把 MP3 拷进 `F00/`，iPod 完全看不见——数据库里没有对应记录
- 反过来，DB 里有记录但文件被删了，iPod 会显示这首歌但播不出来（很常见的现象）

所以"导入"的真实语义是三步：

1. 把文件拷进去
2. **在 DB 里加一条 mhit 记录**指向它
3. **重算签名**——否则固件判定数据库损坏并重建，用户的歌全没了

这也解释了为什么**写库是整库重写而不是增量**：DB 是单文件结构，
chunk 长度字段、`child_count`、总长度都要重算，没法就地插入。

---

## 四、HASH58 签名（核心）

**这是整件事真正的技术壁垒。** iPod Classic 固件校验 `iTunesDB` 头部的 HMAC 签名，
算错就直接重建数据库。算法从 libgpod 逆向而来。

### 第一步：从 FireWire GUID 派生 64 字节密钥

来源是 `SysInfo` 文件里的 `FirewireGuid`（8 字节，如 `C5508AB14FE1CC2F`）：

```python
# 8 字节 GUID 分成 4 对
for i in range(4):
    a, b = firewire_id[i*2], firewire_id[i*2+1]
    cur_lcm = lcm(a, b)                    # ← 取两字节的最小公倍数
    hi, lo  = (cur_lcm >> 8) & 0xFF, cur_lcm & 0xFF
    y[i*4+0] = TABLE1[hi]                  # ← 查表置换（防逆向）
    y[i*4+1] = TABLE2[hi]
    y[i*4+2] = TABLE1[lo]
    y[i*4+3] = TABLE2[lo]

key = SHA1(FIXED + y)                      # FIXED 是 18 字节常量
key = key.ljust(64, b'\x00')               # 补零到 64 字节
```

- `TABLE1` / `TABLE2`：两张写死的 256 字节置换表
- `FIXED`：18 字节常量（`67 23 FE 30 45 33 F8 90 99 21 07 C1 D0 12 B2 A1 07 81`）

### 第二步：HMAC-SHA1

```python
inner = SHA1((key ^ 0x36) + itdb_data)     # ipad
sig   = SHA1((key ^ 0x5C) + inner)         # opad
```

标准 HMAC-SHA1，键是上面派生的 64 字节。

### ★ 第三步：算之前的清零与恢复（顺序错一步就废）

```
1. 备份 db_id (0x18, 8字节) 和 unk0x32 (0x32, 20字节)
2. 把 db_id / unk0x32 / hash58 三个区域全部置零
3. hashing_scheme (0x30) 设为 1
4. 对整份数据算 HMAC-SHA1 → 20 字节
5. 写进 0x58
6. 恢复 db_id 和 unk0x32
```

**注意**：HASH58 与 HASH72 有个关键差别——**HASH58 算之前要把 `unk0x32` 清零**，HASH72 不清。
这类细节就是为什么不能手搓一个"简版写库程序"：错一个字节 iPod 就重建库。

---

## 五、三代签名对比

| 签名 | 用于 | 前置条件 | 能否自足 |
| --- | --- | --- | --- |
| **无签名** | 1G–5G / Mini / Photo / Nano 1–2G | — | ✅ 最简单 |
| **HASH58** | **Classic 全代**、Nano 3–4G | SysInfo 里的 FireWire GUID（设备自带） | ✅ |
| HASH72 | Nano 5G | 需要 `HashInfo` 文件，**必须先被 iTunes 同步过一次**才有密钥材料 | ⚠️ 硬门槛 |
| HASHAB | Nano 6–7G | FireWire GUID + 白盒 AES（13KB wasm 模块） | ✅ |

`mhbd` 头里还有个约定：`hashing_scheme` 字段，HASH58=1、HASH72=2、HASHAB=**4**（注意枚举值是 3 但线上值是 4）。

### 机型识别锚点

| 零售型号 | 机型 |
| --- | --- |
| **A1238** | **iPod Classic 6th Gen (2007) / 6.5th Gen (2008 Rev A) / 7th Gen (2009 Rev B/C)** |
| — | 型号号如 MB147（6G 80GB 黑）、MC297（7G 160GB 黑） |

`SysInfo` 里的 `ModelNumStr` 可能是 `xA623` 这种形式，前缀 `x` 要换成 `M` 再查表。

---

## 六、封面：ArtworkDB 与 ithmb

`ArtworkDB` 结构与 iTunesDB 类似：

```
mhfd (文件头)
└── mhsd → mhli (图像列表) → mhni (图像条目) → mhod (字符串)
```

但**像素数据不在 ArtworkDB 里**，而在 `.ithmb` 文件里。

`ithmb` 是苹果自研的未压缩位图格式（RGB565、YUV 等变体），**每种"尺寸档"一个文件**。
Classic 固件要求四个档：

```
F1055_1.ithmb     32,768 字节
F1060_1.ithmb    204,800 字节
F1061_1.ithmb      6,272 字节
F1068_1.ithmb     32,768 字节
```

**格式写错轻则不显示封面，重则卡死/损坏 ArtworkDB。** 所以 iOpenPod 的内核在
"设备身份未识别"时**拒绝写封面**，报：

```
ART: no artwork definitions available for device unknown; refusing to guess
     an unrelated device format
```

这个"宁可不写"的设计是对的——猜错格式比不写危害大得多。

### 6.1 曲目怎么关联到封面（真机才搞清）

曲目侧有两个可能的关联字段：`mhii_link` 和 `artwork_id_ref`。真机上实测：

```
has_artwork=1  artwork_count=1  artwork_id_ref=238  mhii_link=None
```

**`mhii_link` 是空的**，用的是 `artwork_id_ref`。而 ArtworkDB 侧的条目键是
`['formats', 'img_id', 'song_id', 'src_img_size']`——有 `song_id`。

写入器的解析顺序是**先 song_id 后 link**：

```python
resolved_img_id = existing_by_song_id.get(db_track_id)   # ① 按 song_id（= db_track_id）
if resolved_img_id is None:
    mhii_link = mhii_link or mhiiLink or artwork_id_ref   # ② 退回各种 link 字段
    if mhii_link and mhii_link in existing_art:
        resolved_img_id = mhii_link
```

**这个顺序很关键**：`db_track_id` 在重写前后是**稳定不变**的，而 `img_id` 会被
重新编号。所以即使重写后所有 img_id 都变了，靠 `song_id` 匹配依然能把封面全部找回来。

### 6.2 反直觉：封面条目数变了不等于封面丢了

真机实测：241 条 → 写入后 117 条。

原因：写入器会**去重**（241 条 → 95 个唯一图 → 117 条按曲目条目）。

**判据不是条目数，而是悬空引用数**：

```python
# 每一首：它的关联（song_id 或 artwork_id_ref）是否还指向存在的条目
if sid in by_song or (ref and ref in art_ids): linked += 1
elif ref: dangling += 1        # ← 这才是"封面丢了"
```

真机结果：`117/118 有可用封面，悬空 0` → 健康。

只看条目数从 241 掉到 117 会误判成"封面丢了一大半"。

---

## 七、FAT32 落盘这一道坑

Windows 写 FAT32 有写缓存。**不刷缓存就拔线，数据可能只写了一半**——
数据库半截比完全不写更糟。

内核的处理（`device/durability.py`）：

```
写临时文件 → FlushFileBuffers（含刷卷锚点）→ 改名
```

改名是**原子操作**，所以要么是完整的旧库、要么是完整的新库，不存在半截状态。

自己写工具时如果跳过这一步，会得到"显示成功但库已损坏"的经典故障。

---

## 八、裁剪方法论（从 14 万行裁到 3 千行）

这一节是可复用的方法论，处理"这库太大我只想要一块"这类场景。

### 8.1 先量化，别凭感觉

写导入闭包分析器，从真正的入口沿依赖往外走：

| 方案 | 文件 | 行数 |
| --- | --- | --- |
| iOpenPod 全量 | 265 | 140,171 |
| 天真复用（import 就拖） | 94 | **34,030** |
| 实际裁剪结果 | 116 | **30,617** |

**"天真复用"为什么翻倍**：`device/__init__.py` 会把整个 device 层（31 文件 16,193 行）
全导出来，还顺带拖进 `artworkdb_writer`(3,341) 和 `sync/transcoder`(2,292)。
"我只 import 一下"这个直觉在大库里经常是错的。

### 8.2 关键发现：懒加载依赖不构成负担

`device/info.py` 有 2,752 行，看似很重。但它的重活
（scanner / vpd_libusb / authority）**全是函数内懒加载**，模块级只 import
标准库 + 一个 180 行的 `diagnostic_log`。

→ 所以它能**原样搬进来而不拖泥带水**：100 行 CLI 用不到的 USB 探测代码只是躺着，
永远不会执行。

**教训**：判断一个模块能不能搬，要看**模块级 import**，不是看它引用了多少东西。

### 8.3 实证而非猜测

写了两个 checker：

- `tools/closure.py` —— 算导入闭包，量化裁剪规模
- `tools/find_missing_imports.py` —— 找出 vendored 代码引用了哪些还没搬的模块

**checker 自己踩过两个 bug**（值得记）：

1. 检查 `from x import y` 时要求 `x.y` 也作为模块存在 —— 但 `y` 可能是**函数/类，
   不是模块**。正确的是只检查 `x` 存在。
2. `__init__.py` 里相对导入的锚点包名，要把 `__init__` 这一层去掉
   （`a/b/__init__.py` 的锚点是 `a.b`，不是 `a.b.__init__`）。

修完才是真实清单。最终只补搬了 5 个文件。

### 8.4 改动最小化：保住 cherry-pick 能力

**只改 2 个文件**，其余 90 个逐字节原样：

| 文件 | 改动 | 原因 |
| --- | --- | --- |
| `device/__init__.py` | 删掉未搬模块的 import | 上游会全导出整个 device 层 |
| `__init__.py` | 去掉 `infrastructure.version` 依赖 | 不想为一个版本号搬一整个包 |

这样以后从上游拿修复不会冲突：

```bash
git remote add upstream https://github.com/SomeOrg/upstream.git
git fetch upstream && git cherry-pick <commit>
```

**原则**：搬运时宁可留死代码，也不要改逻辑。死代码不执行，改错的逻辑会静默坏事。

### 8.5 桥接时的两个关键点

#### ① 设备必须"注册"到内核，否则依赖身份的功能会拒绝工作

内核的封面代码走 `get_current_device_for_path(路径)` 取设备。没注册时它报
"device unknown" 并拒绝写封面。

所以要构造一个带 `model_number` 的 `DeviceInfo` 并 `set_current_device()` 注册进去——
注册时内部会校验"能不能选出唯一安全的写入档"，缺 `model_number` 会被拒。

**通用教训**：从大项目裁出来的子集，往往依赖一个"全局当前状态"（当前设备/当前用户/
当前会话）。搬代码时必须连这个状态的**注册入口**一起搬，否则功能静默失效。

#### ② 整库重写必须保全已有数据

`write_itunesdb()` 不是增量，是整库重写。所以链路必须是：

```
读现有库 → 转回写模型 → 拼上新数据 → 整库写回
```

两个函数是配对的（实测形状匹配）：

- `load_ipod_library()` → 扁平字典（键是 `Title`/`Location`/`Artist` 这种可读名）
- `track_dict_to_info()` → 转回写模型

**必须写测试守着这条**——"第二次操作把第一次的数据抹掉"是最要命的失败场景，
而且往往在开发末期才会暴露。

### 8.6 自查修掉的两个静默损坏缺陷

测试没报出来，是读代码时发现的。两个都会**悄悄产出坏数据**：

#### ① 没转码器时把 FLAC 字节拷成 `.m4a`

原逻辑 `if 有转码器: 转码 else: 原样拷贝` —— 需要转码但没传转码器时会落进 `else`，
把 FLAC 字节直接写进 `xxx.m4a`。结果：文件名像 AAC、内容是 FLAC，播不了且报错对不上。

→ 改成：有转码需求但无转码器时**直接报错，绝不拷贝**。

#### ② 没有封面时在 Artwork 目录留下临时文件

原子写入用"临时文件+改名"。没有封面可写时留下了 `.iop-*.tmp` 没人清理。

→ 改成：只在真有封面时才启用封面链路 + 兜底清扫。

**通用教训**：`if 条件: 做A else: 做B` 这种二分结构，在"条件为假但 A 其实是必需的"
场景下会静默走错分支。审查时专门检查这种地方。

### 8.7 测试策略：无硬件也能开发

内核自带 `create_virtual_ipod()`，能造带完整 SysInfo/HashInfo/序列号/GUID 的
**虚拟设备**，文件布局和真机一致。

→ 全部 90 项测试跑在 tmp 目录下，**绝不碰真机**。没有 iPod 也能完成开发验证。

测试素材用 ffmpeg 现场生成（含中文标签），不依赖预置文件；缺 ffmpeg 时相关测试
**自动 skip 而非假通过**。

### 8.8 ★ 真机测试暴露的教训：虚拟设备测不出"你漏做了什么"

这一节是整篇笔记最值得记住的部分。

虚拟设备能验证**代码逻辑对不对**，验证不了**你没想到要写什么**。真机测试抓出两个
虚拟测试根本不可能发现的缺陷：

#### ① 播放列表被整库重写抹掉（静默数据丢失）

`write_itunesdb()` 是整库重写。不传 `playlists` 参数，它只写一个默认主播放列表——
名字默认叫 "iPod"。后果：

| | 写入前 | 写入后 |
| --- | --- | --- |
| 主播放列表名 | 我的 iPod | **iPod** |
| 用户播放列表 | On-The-Go 2 | **丢失** |
| 智能播放列表 | 5 个 | **全部丢失** |

**iPod 的名字本身存在主播放列表标题里**，所以连设备名都被改了。而且**不报错**。

**为什么虚拟测试抓不到**：`create_virtual_ipod()` 造的是空库，本来就没有播放列表，
"丢失"无从体现。测试覆盖了"曲目不丢"，没覆盖"播放列表不丢"。

→ 补搬 `sync/_playlist_builder.py` + `path_identity.py` + `spl_evaluator.py`，
用 `build_and_evaluate_playlists()` 从原库重建，把 7 元组传给 `write_itunesdb()`。
重建失败时**拒绝写入**而非退回默认值。

#### ② `--playlist` 筛选从未生效

播放列表条目用 `track_id`（数据库内的**位置序号**）引用曲目，代码按 `db_track_id` 找。
两个完全不同的东西，永远匹配不上，静默返回空。

→ 优先用 `track_persistent_id`（不受顺序变动影响），退回 `track_id`。

#### 教训

> **"导入成功"的提示说明不了任何事。**
> 必须**写入后逐项核对**：曲目数、播放列表数、设备名、封面悬空数、文件孤儿数、
> 签名方案、逐字段比对原有曲目。
> 这两个缺陷都是靠这一步发现的，不是靠命令的返回码。

而且：**测试要按真实数据的形状写**。如果播放列表测试用的是"一个空虚拟 iPod 加一首歌"，
它照样会通过。故意用真机形状（中文主列表名、用户列表、多曲目）才守得住。

### 8.9 写入前保护的意外收获

内核里有两道我不知道的写入前保险，真机测试时才发现：

1. **挂载点检查**：`inspect_device_write_readiness()` 拒绝"不是独立挂载卷"的路径。
   往临时目录副本写库会被挡下——防止"iPod 没挂载成功，结果往一个空目录写一堆东西"。
   所以彩排需要 `iPodInfo.json` 标记把目录伪装成"虚拟 iPod"（内核支持这种身份）。
2. **卷文件系统检查**：物理 iPod 必须是 FAT/HFS，NTFS 拒绝。

另外：**备份范围要够**。我最初只备份了 `iTunesDB`，但写入会同时重写
`Artwork/ArtworkDB` 和 `.ithmb`，只还原数据库会让封面引用 **100% 悬空**（实测）。
正确做法是备份整个 `iPod_Control/`（不含 Music 约 60MB）。

---

## 九、ipod-cli 功能清单

源码 `<仓库根>`，详见 [ipod-cli 精简版 iPod 工具](ipod-cli 精简版 iPod 工具.md)。

```bash
uv run ipod doctor                       # 环境 + 设备自检
uv run ipod info  --ipod D:\             # 型号/序列号/签名方案/容量/统计
uv run ipod list  --ipod D:\ --by-album  # 按专辑分组
uv run ipod list  --artist 周杰伦 --search 晴天 --json
uv run ipod import ~/Music --ipod D:\    # 导入（默认先显示计划等确认）
uv run ipod import ~/Music --dry-run     # 只预览
uv run ipod export ~/备份 --ipod D:\ --csv 清单.csv
```

| 能力 | 实现要点 |
| --- | --- |
| 设备识别 | 纯文件系统：读 SysInfo → 查 210 机型能力表 → 得签名方案。不走 USB VPD/IOCTL，无驱动依赖 |
| 导入 | 拷文件 + 写库 + HASH58 签名 + 读回校验；目录 `F00–F49` 轮转，文件名 4 位随机（苹果惯例） |
| 自动判重 | 标题+艺人+专辑+文件大小四元组；批内重复也拦；`--force` 可越 |
| 保全已有曲目 | 整库重写前先读回，有专门测试守着 |
| 转码 | 无损源（FLAC/APE/WavPack/WAV/AIFF）→ ALAC；有损源（OGG/Opus/WMA）→ AAC 256k；转码后重新探测产物属性写库 |
| 封面 | 内嵌图或同目录 `cover.jpg`/`folder.jpg` → ArtworkDB + ithmb |
| 导出 | 按艺人/专辑分目录、音轨号前缀、字节级原样拷贝、CSV/JSON 清单 |
| 安全 | 写前自动备份 + 原子写入 + FAT32 刷盘 + 写后读回校验 |
| 编码 | iTunesDB UTF-16LE 无损；CSV 带 BOM（Excel 不乱码）；终端按显示宽度对齐（CJK 占两格） |

### 已实测验证

**虚拟设备上（90 项自动化测试）：**

| 验证项 | 结果 |
| --- | --- |
| HASH58 签名生效 | ✅ 库头 `hashing_scheme=1`，`hash58` 字段非零 |
| 中文往返 | ✅ 含 emoji/全角标点/引号/制表符全部无损 |
| 转码产物 | ✅ ffprobe 确认 `codec_name=alac` |
| 封面关联 | ✅ `artwork_id_ref` 有值、`has_artwork=1`、4 个 ithmb 生成 |
| 导出字节完整性 | ✅ 4/4 SHA256 一致 |
| 播放列表保全 | ✅ 主列表名/用户列表/成员数全部保留 |
| 测试 | ✅ 90 项通过，ruff 全绿 |

**真机上（iPod Classic 6th Gen 80GB Silver / MB029 / 116 首 / 2+5+1 个播放列表）：**

| 验证项 | 结果 |
| --- | --- |
| 设备识别 | ✅ 型号 MB029、GUID `0x000A270000000001`、HASH58，全对 |
| 读库 | ✅ 116 首中文标签全部正确解析 |
| 导入 2 首（1 转码 + 1 原生带封面） | ✅ 116 → 118 首 |
| 原有曲目字段 | ✅ 逐字段比对，**零差异**（Artist/Album/Genre/size/length/bitrate/track_number/year/rating/play_count/filetype/Location） |
| 播放列表 | ✅ 2+5+1 个全部保留，成员数正确 |
| iPod 名字 | ✅ 「我的 iPod」保留 |
| 封面 | ✅ 117/118 有可用封面，**悬空 0** |
| 文件一致性 | ✅ 118 引用 = 118 磁盘文件，0 孤儿 0 缺失 |
| 签名 | ✅ `hashing_scheme=1`，`hash58` 非零 |

---

## 十、已知边界

| 项 | 说明 |
| --- | --- |
| 播放列表**创建/编辑** | 能**保全**已有播放列表，但不能新建或改成员（内核 `mhyp_writer` 在，缺用户交互设计） |
| 智能播放列表规则编辑 | 内核 `mhod_spl_writer` 在，未接 |
| 增量同步 | 现在是"判重+追加"，不是按 mtime/size 做差异同步 |
| 视频 / 照片 / 播客 | 只做音频 |
| 真机验证 | ✅ 已完成（2026-09-19）。**固件接受签名**这一项已由设备正常显示确认 |

已具备的能力（十个命令）：`doctor` / `info` / `list` / `import` / `export` /
`remove` / `verify` / `backup` / `restore` / `rename`。

其中 `verify`（8 项健康检查）和 `backup`/`restore` 是**真机测试后才补的**——
当时这两件事都是临时写脚本干的，说明它们本该是命令。

### 真机写入流程（已验证 8 步）

1. **备份整个 `iPod_Control/` 到电脑**（用 `ipod backup`）——
   只备份 `iTunesDB` **不够**，写入会重写 ArtworkDB 和 ithmb，
   只还原数据库会让封面引用 100% 悬空
2. `ipod doctor` → 确认设备被认出，**签名方案必须是 HASH58**
3. `ipod list` → 确认能读出已有曲目
4. **彩排**：`tools/build_rehearsal.py` 或 `tools/rehearsal_from_backup.py`
   （真机数据 + `iPodInfo.json` 虚拟标记，跑完整写入链路，不碰真机）
5. `ipod import ... --dry-run` 看计划
6. 真机写入
7. **`ipod verify`** ← 最关键，两个真机缺陷都是靠这步发现的
8. 看设备屏幕确认固件接受，然后「安全移除硬件」卸载再拔线

出问题的退路：`ipod restore <备份目录>`（会先另存当前状态，还原本身也可逆）。

---

## 相关

- [ipod-cli 精简版 iPod 工具](ipod-cli 精简版 iPod 工具.md) —— 本项目（源码结构、开发手记）
- [iOpenPod 源码架构](iOpenPod 源码架构.md) —— 上游项目完整框架分析（14 万行 Python + PyQt6）
- 上游仓库：https://github.com/TheRealSavi/iOpenPod （MIT）
- 参考实现：libgpod（C 库，HASH58 算法的原始逆向来源）
