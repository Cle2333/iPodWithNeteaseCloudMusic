# iPodWithNeteaseCloudMusic

把**网易云音乐**的歌单和「我喜欢的音乐」下载到电脑，再同步进 **iPod Classic**。

命令行 + 中文桌面端（Flutter），扫码登录即可用，不依赖 iTunes。

> 为什么需要它：iPod Classic 在 macOS 10.15 之后被移除了支持，Windows 上的 iTunes 也早停了；
> 而网易云下载下来的歌（FLAC 无标签、m4a 是加密容器、VIP 曲目只给 30 秒试听）也不能直接
> 丢进 iPod。中间这一段——**格式转换、标签与封面补全、写进 iTunesDB、算对校验签名**——
> 是这个项目在做的事。

---

## ⚠️ 免责声明

**仅供学习与个人技术研究使用；禁止商业用途；严禁用于盗版传播。**

* 本工具**不提供、不内置、不分发任何音乐内容**，也不绕过任何付费机制。所有音乐都来自使用者
  **自己的账号**（需自行登录，且仅限该账号本就有权访问的内容）。
* 下载的内容**仅供个人在自有设备上收听**，请勿分享、上传或以任何形式传播。
* 请自行确认使用行为符合所在地区法律及平台用户协议。因使用本软件产生的一切后果由使用者承担。

只想「把本地已有的音乐放进 iPod」的话，用 `ipod import` 即可，完全不需要接入任何音乐平台。

---

## 能做什么

* 把网易云歌单 / 「我喜欢的音乐」下载到电脑
* 同步时**按在线歌单在 iPod 上建同名播放列表**并填好曲子
* 在 iPod 上管理播放列表：新建、改名、删除、加歌进来、移出
* 把电脑上的本地音乐导进 iPod
* 设备信息、备份 / 还原、健康检查、批量删除

**支持机型**：iPod Classic 6th / 7th Gen（HASH58 签名）。其它机型没在真机上验过。

## 下载即用（推荐）

去 [Releases](https://github.com/Cle2333/iPodWithNeteaseCloudMusic/releases) 下
`iPodWithNeteaseCloudMusic-v*-windows-x64.zip`，解压，双击 `ipod_manager.exe`。

**不需要装任何东西**——Python 运行时、node 运行时、网易云 API 都在包里，也不需要联网初始化。
（包约 99 MB：里面带着 CPython 3.13 和 Node 22 两个运行时，以及网易云 API 那一整套。）

数据都在 `%APPDATA%\com.example\ipod_manager\data\.ncm\`：

```
.ncm/
  ncm.db          登录态、下载/同步记录
  cache/          下载的歌曲缓存
  logs/           后端日志（backend-<日期>.log）
```

备份这个目录就等于备份全部状态。**出问题先看 `logs/`**——嵌入模式下界面看不到控制台。

**从 v0.1.0 升级**：旧版数据在"你运行命令的那个目录"下的 `.ncm\`，新版不认那个位置，把这个
目录拷到上面那个路径即可，否则要重新扫码登录。

## 快速开始

1. 双击 `ipod_manager.exe`
2. 「设置」→ 登录网易云（扫码）
3. 插上 iPod，等它出现在「此电脑」里
4. 「歌单」页挑歌 → 下载 → 同步；或在「iPod 音乐管理」页整理设备上的歌与播放列表
5. 传的过程中看「**进度**」页：它显示当前阶段（读取歌单 → 下载到本地 → 写入 iPod →
   把 N MB 刷到设备 → 重建数据库并签名）和进度条。**只要进度条还在动，就说明在干活**
   ——无损档要把每首 FLAC 转成 ALAC，几百首要几分钟，那几分钟里进度条会一直在走。

---

## 它解决什么问题

| 环节 | 难点 | 这个项目怎么处理 |
| --- | --- | --- |
| 拿到歌 | 网易云没有官方批量下载；直连 API 要处理登录态、音质档位、风控 | 本地起 [api-enhanced](https://github.com/Binaryify/NeteaseCloudMusicApi) 取数据，下载器**严格串行 + 限速** |
| 文件变成能播的 | FLAC 无标签、m4a 是加密容器、部分曲目只给 30 秒试听 | 补标签+内嵌封面、必要时 ffmpeg 转码、**下载后比对时长揪出试听片段** |
| 写进 iPod | iTunesDB 是私有二进制格式；HASH58 签名算错，iPod 会判定"数据库损坏"并重建 | 裁剪复用 [iOpenPod](https://github.com/TheRealSavi/iOpenPod) 内核，写入前后各一道校验 |
| 好用 | 命令行记不住参数，同步状况看不见 | 中文桌面端，每首歌标出「未下载 / 已下载 / 已同步」 |

桌面端是 Flutter，后端是 FastAPI，**两者嵌在同一个进程里**——用户不用装 Python、不用装 uv、
不用管子进程有没有杀干净。桌面端「iPod 音乐管理」页的多选、删除确认、歌单管理都有对应的
后端接口与测试；写入一律"先预览、后执行"，删除歌单还会带一次性确认令牌。

## 从源码跑

```bash
# 后端（命令行用法 / 开发）
uv sync
uv run ipod doctor              # 环境自检
uv run ipod-web                 # 只起后端（默认 127.0.0.1:8765）

# 桌面端
cd app
cp ../tools/setup_flutter_env.ps1 .   # 或自行设置国内镜像
flutter pub get
flutter run -d windows
```

后端默认**嵌入**在桌面端里。开发时想退回"外挂子进程"模式（改后端代码不用重编译）：

```bash
IPOD_MANAGER_EXTERNAL_BACKEND=1 flutter run -d windows
```

打 Windows 发行版：

```bash
uv run python tools/build_windows_release.py      # 组装 → 装依赖 → 打 node → flutter build
uv run python tools/package_release.py --version 0.3.0
```

## 命令行

| 命令 | 作用 |
| --- | --- |
| `doctor` | 环境自检：Python / ffmpeg / 内核 / 设备 |
| `info` | 设备信息（型号 / 序列号 / 签名方案 / 容量 / 曲目统计） |
| `list` | 列出曲目，可按艺人 / 专辑 / 流派 / 播放列表筛选和搜索 |
| `import` | 导入（PC → iPod），含判重、转码、封面 |
| `export` | 导出（iPod → PC），按艺人 / 专辑分目录 |
| `remove` | 删除曲目（**必须先指定筛选条件**） |
| `verify` | 健康检查：签名 / 封面 / 文件 / 播放列表一致性 |
| `backup` / `restore` | 备份整个 `iPod_Control` / 从备份还原 |
| `rename` | 改 iPod 的名字 |

`--ipod` 省略时会自动扫描盘符找 iPod；找不到或有多台时才需手动指定（如 `--ipod D:\`）。

三个入口：**`ipod`** 管设备本身，**`ipod-sync`** 管网易云（`plan`/`download`/`push`/`login`/`status`），
**`ipod-web`** 是桌面端的后端（一般由 app 自动拉起）。

## 测试

```bash
uv run pytest                 # 后端：跑在 tmp 目录的虚拟 iPod 上，不碰真机也不碰真网络
cd app && flutter test        # 桌面端
```

这里**故意不写测试条数**——那种数字隔几天就过期，然后没人改，反而变成误导。

涉及真机写入的验证流程（备份 → 彩排 → 上真机）见 `release/打包说明.md`；
`tools/` 下有一整套脚本：`verify_embedded_release.py`（发行版自检）、
`verify_device_flow.py`（对一台 iPod 做完整读写往返，支持彩排目录）、
`rehearsal_from_backup.py`（用真机备份搭一台彩排 iPod）。

## 目录结构

```
src/iopenpod/     裁剪自 iOpenPod 的内核（iTunesDB / ArtworkDB / HASH58 / FAT32 安全写入）
src/ipod_cli/     自写部分：设备操作、导入导出、网易云同步
  ncm/server.py   拉起并收掉自带的网易云 API 子进程（含孤儿防护）
src/ipod_web/     FastAPI 后端：路由 + 单线程作业队列
  paths.py        数据目录推导与迁移
app/              Flutter 桌面端
  python/main.py  嵌入模式入口（serious_python 在独立线程里跑它）
  node/launcher.js 自带网易云 API 的启动器（父进程看门狗）
tests/            后端测试    app/test/  桌面端测试
tools/            构建与验证脚本
docs/             技术文档（见下）
```

## 文档

`docs/` 里是从开发过程沉淀下来的，不是事后补的说明：

| 文档 | 内容 |
| --- | --- |
| [网易云 → iPod 同步方案](docs/网易云%20→%20iPod%20同步方案.md) | 全链路实测：可下载率、音质档位陷阱、限速表现、真机验证记录 |
| [iPod 存储原理与 HASH58 签名](docs/iPod%20存储原理与%20HASH58%20签名.md) | iTunesDB/ArtworkDB 二进制布局、签名算法、FAT32 写入注意事项 |
| [ipod-cli 精简版 iPod 工具](docs/ipod-cli%20精简版%20iPod%20工具.md) | 命令行工具的设计与用法 |
| [iOpenPod 源码架构](docs/iOpenPod%20源码架构.md) | 上游结构，以及裁掉了什么、为什么 |
| [架构选型调研](docs/架构选型调研-单语言还是一体化.md) | "要不要改成一体的"——查了哪些方案、实测数据、为什么选嵌入 Python |

文档和代码对不上时**以代码为准**——文档记录的是当时的事实。

## 常见问题

**音质有哪些档位？** 三档：标准 (128k) / 极高 (320k) / 无损 (FLAC)，默认 320k。
网易云的 `hires` / `jymaster` / `jyeffect` **故意不提供**——那是 24bit+ 或空间音频，
iPod Classic 播不了，下下来也是废文件。请求无损拿不到时会**往下**退到 320k，
绝不往上够。按无损全库算约 144 GB，80GB 的机器装不下；320k 约 40 GB，装得下。

**低音失真 / 听着破？** 先确认不是音源本身——这是最常见的原因，尤其华语流行。
用 ffmpeg 量一下真峰值（True Peak）就知道：

```bash
ffmpeg -i "某首歌.flac" -af loudnorm=print_format=json -f null - 2>&1 | grep input_tp
```

`input_tp` **超过 0 dBTP 就是音源已经削波**，波形在混音阶段被推过了上限，低音
那一段最先被削平——这个失真从下载的那一刻就存在了，跟 iPod 和本工具都无关。
实测 8 首随机抽的网易云曲目里 **6 首超过 0**，响度普遍在 -7 ~ -13 LUFS（压得
很狠的现代母带；流媒体平台一般以 -14 为标准）。

无损会比 320k "好一点"，因为**两者是同一个已经削了的母带**：无损是原样，
320k 是在它上面再压一道有损编码，解码时的峰值过冲会让本来顶在 0 的采样点更高
——**二次削波**。

能做的：① 关掉 iPod 的 EQ（往低频加增益会削得更狠）和 Sound Check；② 换个
低频量少的耳机试试；③ 同一首歌在网易云上常有多个版本（原版/重制/不同专辑），
母带不一样，换个版本可能就干净了。

**这个工具会不会动我的音频？** 不会。`mp3` / `m4a` / `wav` 这些 iPod 原生格式
**原样拷贝**（一个字节都不改）；`flac` 等转换目标是**无损 ALAC**（强制 16bit，
44.1kHz 不重采样）。两条路都没有二次压缩。

## 更新日志

每个版本改了什么见 **[CHANGELOG.md](CHANGELOG.md)**——只记用户能感知的变化，
重构和文档错别字那种不占篇幅（要看得细就 `git log`）。

## 引用的项目

| 项目 | 用在哪 | 许可 |
| --- | --- | --- |
| [iOpenPod](https://github.com/TheRealSavi/iOpenPod) | iTunesDB / ArtworkDB 读写、HASH58 签名、FAT32 安全写入 | MIT |
| [api-enhanced](https://github.com/Binaryify/NeteaseCloudMusicApi) | 本地网易云 API（随发行版打包） | MIT |
| [serious-python](https://github.com/flet-dev/serious-python) | 把 CPython 嵌进桌面应用 | Apache-2.0 |
| [mutagen](https://github.com/quodlibet/mutagen) | 读写音频标签 | GPL-2.0 |
| [pycryptodome](https://github.com/Legrandin/pycryptodome) | HASH58 派生密钥的 AES | BSD-2 |
| [FastAPI](https://github.com/fastapi/fastapi) / [uvicorn](https://github.com/encode/uvicorn) | 后端 | MIT / BSD |
| [Flutter](https://github.com/flutter/flutter) | 桌面端界面 | BSD-3 |
| [ffmpeg](https://ffmpeg.org/) | 音频转码（外部命令，可选） | LGPL/GPL |

Node.js 与 CPython 运行时也随 Windows 发行版分发。裁剪范围、上游 commit 与全部许可文本见
`THIRD_PARTY_NOTICES.md`。

## 许可

MIT。内核部分版权归 iOpenPod 作者，详见 `THIRD_PARTY_NOTICES.md`。
