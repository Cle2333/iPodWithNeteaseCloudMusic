# iPodWithNeteaseCloudMusic

把**网易云音乐**的歌单和「喜欢的歌」下载到电脑，再自动同步进 **iPod Classic**。

命令行 + 中文桌面端（Flutter）。扫码登录即可用，不依赖 iTunes。

> 为什么需要这个：iPod Classic 在 macOS 10.15 之后被彻底移除了支持，Windows 上的
> iTunes 也早已停更；而网易云下载下来的歌（FLAC/加密 m4a）也不能直接丢进 iPod。
> 中间这一段——**格式转换、标签与封面补全、写进 iTunesDB、算对校验签名**——是这个
> 项目在做的事。

---

## ⚠️ 免责声明

* **本软件仅供学习与个人技术研究使用。**
* **禁止用于任何商业用途。**
* **严禁用于盗版传播**——不得用本工具分发、传播任何未经授权的音乐。

具体来说：

* 本工具**不提供、不内置、不分发任何音乐内容**，也不绕过任何付费机制。
  它做的只是「格式转换 + 写进 iPod」这一段，所有音乐都来自使用者**自己的账号**
  （需自行登录，且仅限该账号本就有权访问的内容）。
* 下载下来的内容**仅供个人在自有设备上收听**，请勿分享、上传或用于任何形式的传播。
* 请自行确认使用行为符合所在地区法律法规及音乐平台的用户协议。
* 因使用本软件产生的一切后果由使用者自行承担，作者不承担任何责任。

如果你只是想找一个「把本地已有的音乐文件放进 iPod」的工具，用
`ipod import` 这部分能力即可，完全不需要接入任何音乐平台。


---

## 它解决什么问题

| 环节 | 难点 | 这个项目怎么处理 |
| --- | --- | --- |
| 拿到歌 | 网易云没有官方的批量下载；直连 API 又要处理登录态、音质档位、风控 | 复用 [api-enhanced](https://github.com/Binaryify/NeteaseCloudMusicApi) 做本地 API，自己写**严格串行 + 限速**的下载器 |
| 文件变成能播的 | 网易云给的是"半成品"：FLAC 无标签、m4a 是加密容器、部分曲目只给 30 秒试听 | 补标签+内嵌封面、必要时 ffmpeg 转码、**下载后比对时长揪出试听片段** |
| 写进 iPod | iTunesDB 是私有二进制格式；算错 HASH58 签名 iPod 会判定"数据库损坏"并重建，用户几百首歌就没了 | 裁剪复用 [iOpenPod](https://github.com/TheRealSavi/iOpenPod) 的内核，写入前后各一道校验 |
| 好用 | 命令行记不住参数；同步状况看不见 | 中文桌面端，每首歌标出「未下载 / 已下载 / 已同步」 |

---

## 原理

### 一、iPod 侧：为什么是"裁剪"而不是"重写"

iTunesDB 的二进制格式和校验签名是真正的技术壁垒：

* **格式**：`mhbd`/`mhsd`/`mhit`/`mhod`/`mhyp`/`mhli`/`mhla` 七种 chunk 的精确布局，
  还有 iTunesCDB 的 zlib 变体。字段写错不会报错，iPod 直接重建数据库。
* **签名**：HASH58 要从 `SysInfo` 的 FireWire GUID 派生出密钥再算 LCM。算错了
  iPod 认为"数据库损坏"，**重建 = 用户几百首歌没了**。
* **落盘**：Windows 往 FAT32 写要 `FlushFileBuffers` 刷卷，不刷就拔线会丢数据。

这些重写一遍风险极高、收益为零，所以 `src/iopenpod/` 直接**裁剪复用** iOpenPod
（MIT）的对应模块——上游 265 文件 / 14 万行里，只留下这个项目真正需要的
约 2.8 万行，并且**几乎不改动**（改动只在 `device/__init__.py` 的导出面）。

自己写的是**它上面的一层**：设备识别、读库、导入/导出/删除、备份还原、健康检查，
以及整个网易云同步链路。

### 二、网易云侧：怎么把歌拿出来

```
登录（扫码 → cookie 落本地，不进版本库）
  ↓
拿歌单曲目  /playlist/track/all?id=<歌单 id>
  ↓        ★ 不按名字找歌单——用户会改名；「喜欢的歌」认 specialType=5
规划 plan    逐首判断：本地有没有文件、iPod 上有没有 → 算出"要下几首"
  ↓        ★ "同步到 iPod"和"下载到本地"是两件事，跳过条件不一样
下载        严格串行，每首间隔 ≥0.35s，失败退避重试
  ↓        ★ 绝不并发——网易云高频请求会风控封号
补全        FLAC 补标签+封面；必要时转码；**核对时长揪出 30 秒试听片段**
  ↓
写库        整库重写 iTunesDB + ArtworkDB + 重建播放列表 + HASH58 签名
  ↓
校验        写前备份 → 写后读回 → 刷盘，三道防线
```

### 三、串起来

```
┌─────────────────────────────────────────────────┐
│  Flutter 桌面端（app/）                          │
│  只渲染 + 交互，不做业务逻辑                       │
└──────────────────┬──────────────────────────────┘
                   │ HTTP 127.0.0.1:8765
┌──────────────────▼──────────────────────────────┐
│  FastAPI 后端（src/ipod_web/）                   │
│  路由 + 全局单线程作业队列 + 缓存                  │
│  ★ 所有碰外部世界的活走同一条队列                  │
└──────┬────────────────────────────┬─────────────┘
       │ 同进程直接调用（不是子进程）   │ HTTP localhost:4000
┌──────▼──────────────┐    ┌────────▼────────────┐
│ 同步内核             │    │ api-enhanced        │
│ （src/ipod_cli/）    │    │ （Node，网易云 API） │
└──────┬──────────────┘    └─────────────────────┘
       │
┌──────▼──────────────────────────────────────────┐
│  iOpenPod 内核（src/iopenpod/）                  │
│  iTunesDB / ArtworkDB 读写 + HASH58 签名         │
└──────┬──────────────────────────────────────────┘
       │ 文件系统（FAT32）
┌──────▼──────────────────────────────────────────┐
│  iPod Classic（挂载为盘符，如 D:\）             │
└─────────────────────────────────────────────────┘
```

**为什么后端要单线程**：两个作业同时重写 iTunesDB 是数据损坏级风险；而且
网易云的限速要求天然排斥并发。界面上连点十次下载，实际只有一个请求在飞。

---

## 为什么支持 Classic 就够了

iPod Classic 全代用 **HASH58** 签名，只需要设备 `SysInfo` 里的 FireWire GUID
即可自足完成签名——**不需要先用 iTunes 同步过一次**（那是 Nano 5G 的限制），
**也不需要** SQLite 库和 iTunesCDB 压缩（那是 Nano 5G/6G/7G 的机制）。
所以 iOpenPod 里最麻烦的几个分支全都不用走。

## 桌面端：iPod 音乐管理器

除了 CLI，还有一个 **Flutter 桌面应用**，把「网易云歌单 → 下载 → 同步进 iPod」
这条链路做成了点点点。

```
app\build\windows\x64\runner\Debug\ipod_manager.exe
```

双击即可。**后端由应用自动拉起**，不用先手动起 `ipod-web`。

四块界面：

| 页面 | 干什么 |
| --- | --- |
| **歌单** | 浏览网易云歌单（10 分钟缓存）→ 每首歌标出状态、可搜索 → 下载 / 同步 / 删本地那份 |
| **下载** | 只管进度：本次任务跑到哪了、逐首歌的结果（含失败原因）、还要多久、能不能取消 |
| **iPod 音乐管理** | **iPod 设备上**的曲目（搜索/排序/分页）、批量多选、从电脑导入、批量删除、健康检查 |
| **设置** | 扫码登录/切换账号、音质档位、请求间隔、缓存清理、设备信息、**后端调试面板** |

### 状态只有三个：未下载 / 已下载 / 已同步

| 状态 | 意思 | 能干什么 |
| --- | --- | --- |
| **未下载** | 电脑上没有这份文件 | 下载 / 直接同步 |
| **已下载** | 电脑上有，iPod 上还没有 | 同步到 iPod / 删本地那份 |
| **已同步** | iPod 上已经有了 | 删本地那份（"已同步 · 本地无"表示本地那份已经删了） |

**曾经这四个状态分散在两页说**：「歌单」页管在线曲目，「下载」页另有一个
「本地已下载」管理器，按**来源歌单**把同一批歌又分了一次组。同一件事两处
各说一遍、说法还不完全一样——删除本地之后两边状态不同步、来源分组要额外
维护一套 schema，几次 bug 都出在这儿。2026-09-20 合并掉了：**"这首歌本地
有没有、iPod 有没有"只在歌单页说一次**，删本地文件的能力挪到歌单页的右键
菜单和多选操作条。

### 几个刻意的设计

* **绝不并发**：所有碰外部世界的活走同一条队列、同一个工作线程。
  界面上连点十次下载，实际只有一个请求在飞——网易云高频请求会风控封号，
  而两个作业同时重写 iTunesDB 是数据损坏级风险。
* **破坏性操作要先预览**：删歌必须先预览（多少首 + 释放多少空间 + 前几首
  叫什么），而且后端要求带回**一次性预览令牌**——没有它直接拒绝。
  这条限制是有意留的：让"弹确认框"成为结构性保证，而不是界面自觉。
* **「全选本页（N 首）」和「全选筛选结果（M 首）」是两个按钮**，
  各自写明数量。合成一个"全选"的代价是真的删错歌。
* **界面里没有导出入口**（版权考虑）。导出能力保留在 CLI 的 `ipod export`。

### 右键菜单

两个列表（网易云歌单曲目 / iPod 音乐管理）的每一行都能**右键**，
弹出的操作跟当前上下文相关：

| 页面 | 菜单里有什么 |
| --- | --- |
| 歌单（网易云） | 下载这一首 · 同步到 iPod · 勾选 · **删除本地那份** · 只看未下载的 · 复制歌名 / 曲目信息 |
| iPod 音乐管理 | 从 iPod 删除 · 勾选 · 复制歌名 / 曲目信息 · 按这个艺人筛选 |

两条约定：

* 不可用的项**留在菜单里但置灰**，并把原因写在下面（"本地没有这份文件"）。
  直接不显示的话，用户会以为"这个功能没有"，而不是"现在还不能用"。
* 删除类的项标红，而且**仍然要弹确认框**——右键菜单最容易误点，
  "在菜单里点了一次"不等于"用户确认过要删"。

### 自动刷新

下载、同步、删除、导入全都走后台作业队列，提交完立刻返回。作业**真正干完**
的时候，页面上的数据已经过期了（歌单页还标着"未下载"、iPod 曲目列表还没算上
刚导进去的歌）。

以前是每个页面自己写个定时器去猜（歌单页等 5 秒、下载页等 4 秒，纯属拍脑袋）。
现在由 `AppState.refreshSignal` 统一管：轮询发现**完成集合里出现了新的作业 id**
就 ping 一次，所有页面一起重新拉数据。判据不用"队列从忙变闲"是因为后者会漏掉
两次轮询（3 秒）之间就干完的短作业——删几首本地文件就是这种。

### 删本地那份 ≠ 从 iPod 上删

歌单页的「删除本地那份」（右键单首 / 多选操作条）删的是**电脑上那份缓存**，
iPod 上已经同步进去的歌**不受影响**——对话框里专门写明了这句，不然用户会
以为"删了就没了"而不敢清。

想彻底清空本地缓存（包括那些已经不在任何歌单里的孤儿文件）走
**设置 → 本地缓存**，那里显示占用并能一键清掉。

反过来，从 iPod 上删歌在 **iPod 音乐管理**页，走的是另一套（先预览、再确认、
带回一次性令牌）。两边故意分开：一个动电脑，一个动设备，混在一起迟早出事。

### 从源码跑

```bash
# 后端（应用会自动拉，但也可以单独跑）
uv run ipod-web --port 8765

# 前端
cd app
export PUB_HOSTED_URL=https://pub.flutter-io.cn
export FLUTTER_STORAGE_BASE_URL=https://storage.flutter-io.cn
flutter run -d windows
```

国内必须走镜像：官方源约 0.2 MB/s，镜像是 17 MB/s。

---

## 安装

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)。

```bash
cd ipod-cli
uv sync
```

转码功能需要 **ffmpeg**（把 FLAC/APE/OGG 等转成 iPod 能播的格式）：

```bash
winget install Gyan.FFmpeg     # Windows
brew install ffmpeg            # macOS
sudo apt install ffmpeg        # Linux
```

## 命令一览

| 命令 | 作用 |
| --- | --- |
| `doctor` | 环境自检：Python / ffmpeg / 内核 / 设备 |
| `info` | 设备信息（型号/序列号/签名方案/容量/曲目统计） |
| `list` | 列出曲目，支持按艺人/专辑/流派/播放列表筛选和搜索 |
| `import` | 导入（PC → iPod），含判重、转码、封面 |
| `export` | 导出（iPod → PC），按艺人/专辑分目录，可导清单 |
| **`remove`** | 删除曲目（必须先指定筛选条件） |
| **`verify`** | 健康检查：签名 / 封面 / 文件 / 播放列表一致性 |
| **`backup`** | 备份整个 `iPod_Control`（可完整还原） |
| **`restore`** | 从备份还原（会先另存当前状态） |
| **`rename`** | 改 iPod 的名字 |

`--ipod` 参数省略时会自动扫描盘符/挂载点找 iPod。找不到或有多台时才需要手动指定，
例如 `--ipod D:\`。

本仓库有三个入口，各有分工：

| 入口 | 用途 |
| --- | --- |
| **`ipod`** | 设备本身的事：列表 / 导入 / 导出 / 删除 / 校验 / 备份 / 改名 |
| **`ipod-sync`** | 网易云的事：`plan` / `download` / `push` / `login` / `accounts` / `use` / `status` |
| **`ipod-web`** | 给桌面端用的本地后端（默认 127.0.0.1:8765），一般由 app 自动拉起 |

## 用法

### 先自检

```bash
uv run ipod doctor
```

### 看看设备上有什么

```bash
uv run ipod info                            # 型号/序列号/签名方案/容量
uv run ipod list                            # 列出曲目
uv run ipod list --by-album                 # 按专辑分组
uv run ipod list --artist 周杰伦            # 只看某艺人
uv run ipod list --playlist 通勤            # 只看某个播放列表
uv run ipod list --search 晴天              # 搜索
```

### 导入

```bash
uv run ipod import ~/Music/新专辑           # 导入文件夹（先显示计划并要求确认）
uv run ipod import 歌.mp3 -y                # 跳过确认
uv run ipod import ~/Music --dry-run        # 只预览，不做任何修改
uv run ipod import ~/Music --no-transcode   # 不转码（跳过 iPod 不支持的格式）
uv run ipod import ~/Music --no-artwork     # 不写封面
```

导入行为：

* **自动判重**：标题+艺人+专辑+文件大小都一致就跳过，想强行导入加 `--force`
* **自动转码**：FLAC/APE/WavPack 等无损格式 → ALAC（不二次损失）；
  OGG/Opus/WMA → AAC 256kbps
* **自动封面**：从音频文件内嵌封面或同目录的 `cover.jpg`/`folder.jpg` 提取
* **保全已有曲目和播放列表**：整库重写时会把已有曲目和播放列表一起重建回去
* **写前备份 + 写后校验**：自动生成 `iTunesDB.backup`，写完重读核对曲目数

### 导出

```bash
uv run ipod export ~/备份                   # 全部导出（按艺人分目录）
uv run ipod export ~/备份 --artist 周杰伦   # 只导出某艺人
uv run ipod export ~/备份 --playlist 通勤   # 只导出某个播放列表
uv run ipod export ~/备份 --flat            # 平铺不分目录
uv run ipod export ~/备份 --csv 清单.csv    # 同时导清单（Excel 直接打开不乱码）
```

### 删除

删除是破坏性操作，所以**必须指定筛选条件**（否则会匹配到全部曲目）：

```bash
uv run ipod remove --artist "ipod-cli 测试" --dry-run   # 先看会删什么
uv run ipod remove --artist "ipod-cli 测试" -y          # 真删
uv run ipod remove --album 某专辑 -y
uv run ipod remove --playlist 临时歌单 -y                # 删某个播放列表里的歌
```

设计要点：

* **先写库、后删文件**。反过来的话，写库失败会留下"库里有记录、磁盘没文件"的
  坏状态（iPod 上显示却播不出来）。先写库最坏只留孤儿文件（白占空间，无害）。
* **默认不动封面库**。剩余曲目的封面引用原样有效，零风险。
  被删曲目的封面条目会成为孤儿（不可见），要收掉加 `--compact-artwork`。
* **播放列表自动清理**。幽灵条目由重建机制处理，不需要单独操作。
* 拒绝清空整个曲库（太容易误用）。

### 备份与还原

```bash
uv run ipod backup                          # 备份到 ~/Documents/iPod备份-时间戳
uv run ipod backup ~/我的备份 --verify      # 指定目录并立即校验哈希
uv run ipod backup --with-music ~/全量      # 连音乐一起（几十 GB，很慢）

uv run ipod restore ~/我的备份 --dry-run    # 先看会做什么
uv run ipod restore ~/我的备份 -y
```

**为什么备份整个 `iPod_Control` 而不只是 `iTunesDB`**：写入会**同时重写**
`Artwork/ArtworkDB` 和 `.ithmb` 文件。只还原数据库的话，曲目里的 `artwork_id_ref`
会全部指向已不存在的封面条目——实测 **100% 悬空**，封面全丢。

备份目录里有 `manifest.json`，记录每个文件的 SHA256。还原前会自动校验，
校验不过就拒绝写入。还原前还会**先把设备当前状态另存一份**（还原本身可逆）。

> ⚠️ 不加 `--with-music` 的备份**只还原数据库和封面，不动音频文件**。
> 如果备份之后删过歌，还原后那些歌会回到曲库里但文件不在——
> 用 `ipod verify` 能看到这种缺失。

### 健康检查

```bash
uv run ipod verify                          # 8 项检查
uv run ipod verify -v                       # 显示全部详情
uv run ipod verify --json                   # 输出 JSON（给脚本用）
uv run ipod verify --no-files               # 跳过逐文件核对（大曲库快很多）
```

检查项：设备识别 / 数据库文件头 / 可读性 / 签名 / 曲目字段 / 文件对应 /
封面 / 播放列表 / 备份。

**写入之后一定要跑一次。** 写入函数返回成功不代表固件能读——本项目在真机上
踩到的两个静默数据丢失缺陷（播放列表被抹、`--playlist` 失效）都不是靠命令
返回码发现的，而是靠写入后逐项核对。

### 改名

```bash
uv run ipod rename "铁头的 iPod"
```

iPod 的名字存在**主播放列表的标题**里，所以改名本质上是重写一次数据库
（曲目内容不变，封面库不动）。

## 推荐工作流

```bash
# 1. 插上 iPod，先看状态
uv run ipod verify

# 2. 改动前备份
uv run ipod backup ~/iPod备份-$(date +%Y%m%d)

# 3. 先预览再动手
uv run ipod import ~/新专辑 --dry-run
uv run ipod import ~/新专辑 -y

# 4. 写入后核对
uv run ipod verify

# 5. 安全移除硬件，再拔线
```

万一出问题：`uv run ipod restore ~/iPod备份-xxx` 即可还原。

## 测试

```bash
uv run pytest tests/ -q
uv run ruff check src/ipod_cli tests tools
```

全部跑在 tmp 目录下的**虚拟 iPod** 上（由内核的 `create_virtual_ipod()` 生成，
带完整 SysInfo/HashInfo/序列号），绝不碰真实设备。
测试音频用 ffmpeg 现场生成，含中文标签；缺 ffmpeg 时相关测试自动 skip 而非假通过。

测试里刻意造了各种**坏状态**来验证健康检查真的能抓到：删掉音乐文件、
塞孤儿文件、把签名清零、截断数据库、构造悬空封面引用、塞垃圾文件头。

## 目录结构

```
src/iopenpod/     裁剪自 iOpenPod 的内核（104 文件 / 27,979 行，几乎原样搬运）
                   iTunesDB / ArtworkDB 读写 + HASH58 签名 + FAT32 安全写入
src/ipod_cli/     自写的设备侧代码（19 文件 / 7,329 行）
  discovery.py     设备识别（纯文件系统，不走 USB 硬件探测）
  library.py       读库
  dbwrite.py       写入核心：整库重写 + 重建播放列表 + 签名 + 读回校验
  importer.py      导入        exporter.py   导出
  remover.py       删除        verify.py     健康检查
  backup.py        备份/还原    transcode.py  ffmpeg 转码
  ncm/             网易云同步链路（client 接口 / sync 规划 / state 状态库）
  sync_cli.py      网易云命令行入口
  cli.py           设备命令行入口
src/ipod_web/     FastAPI 后端（15 文件 / 3,660 行）
                  路由 + 全局单线程作业队列 + 缓存，给桌面端用
app/              Flutter 桌面端（20 文件 / 7,598 行 Dart）
tests/            pytest 555 项（25 文件 / 9,042 行）
                  app/test/ 另有 Flutter 71 项
tools/            开发辅助脚本：真机彩排、依赖分析、端口清理
docs/             技术文档
```

自己写的部分：Python 约 11,000 行 + Dart 约 7,600 行 + 测试约 11,400 行。

## 引用的项目

| 项目 | 用在哪 | 许可 |
| --- | --- | --- |
| **[iOpenPod](https://github.com/TheRealSavi/iOpenPod)** | `src/iopenpod/` 内核：iTunesDB / ArtworkDB 读写、HASH58 签名、FAT32 安全写入 | MIT |
| **[api-enhanced](https://github.com/Binaryify/NeteaseCloudMusicApi)** | 本地网易云 API（`localhost:4000`）：歌单、曲目、取流地址、登录 | MIT |
| [mutagen](https://github.com/quodlibet/mutagen) | 读写 PC 端音频标签 | GPL-2.0 |
| [pycryptodome](https://github.com/Legrandin/pycryptodome) | HASH58 派生密钥的 AES | BSD-2 |
| [FastAPI](https://github.com/fastapi/fastapi) / [uvicorn](https://github.com/encode/uvicorn) | 桌面端后端 | MIT / BSD |
| [Flutter](https://github.com/flutter/flutter) | 桌面端界面 | BSD-3 |
| [ffmpeg](https://ffmpeg.org/) | 音频转码（外部命令，可选） | LGPL/GPL |

裁剪范围与上游 commit 见 `THIRD_PARTY_NOTICES.md`。

**没有直接用但参考过的**：[libgpod](https://github.com/fzug/libgpod)（播放次数增量文件的
合并语义）、[gtkpod](https://github.com/fzug/gtkpod) / [foobar2000 的 iPod 组件](https://www.foobar2000.org/components)
（同类工具的行为基准）。

## 文档

`docs/` 里是从开发过程沉淀下来的技术文档，不是事后补的说明：

| 文档 | 内容 |
| --- | --- |
| [网易云 → iPod 同步方案](docs/网易云%20→%20iPod%20同步方案.md) | 全链路实测：可下载率、音质档位陷阱、限速表现、真机验证记录、每个坑的现场 |
| [iPod 存储原理与 HASH58 签名](docs/iPod%20存储原理与%20HASH58%20签名.md) | iTunesDB/ArtworkDB 的二进制布局、校验签名怎么算、FAT32 写入要注意什么 |
| [ipod-cli 精简版 iPod 工具](docs/ipod-cli%20精简版%20iPod%20工具.md) | 命令行工具的设计与用法 |
| [iOpenPod 源码架构](docs/iOpenPod%20源码架构.md) | 上游项目结构与我裁掉了什么、为什么 |

读的过程中如果发现文档和代码对不上——**以代码为准**，文档记录的是当时的事实。

## 许可

MIT。内核部分版权归 iOpenPod 作者，详见 `THIRD_PARTY_NOTICES.md`。
