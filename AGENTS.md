# AGENTS.md

给 AI 助手的项目上下文（DeepSeek Harness / Claude Code / Codex / Hermes / Cursor 等）。
人看的说明在 `README.md`。

> 本文件是**唯一真相源**。别的 harness 认的文件名（`CLAUDE.md` / `.cursor/rules/` 等）
> 如需存在，只放一行指向本文件的引用，**不要复制内容**——复制出来的必然漂移。

## 这是什么

把网易云音乐的歌单和「喜欢的歌」下载到电脑，再同步进 iPod Classic。
Python 后端 + 中文 Flutter 桌面端。

**仅供学习使用，禁止商业用途，严禁盗版传播**（见 README 免责声明）。改动时不要
引入任何绕过平台付费机制、批量分发音乐内容的能力。

## 结构

```
src/iopenpod/   裁剪自 iOpenPod（MIT）的内核 —— iTunesDB/ArtworkDB 读写 + HASH58 签名
                ★ 几乎原样搬运，**不要改这里**；要改请先读 THIRD_PARTY_NOTICES.md
src/ipod_cli/   自写设备侧代码 + 网易云同步链路（ncm/）
src/ipod_web/   FastAPI 后端，给桌面端用
  paths.py      数据目录推导与迁移
app/            Flutter 桌面端（Dart）
  python/main.py    嵌入模式入口（serious_python 在独立线程里跑它）
  lib/services/backend.dart  后端生命周期：嵌入（默认）/ 外挂（uv 子进程）
tests/          pytest（真实网络一律不碰，靠 mock 传输层）
app/test/       Flutter 测试
tools/          开发脚本（构建 / 打包 / 自检 / 插件软链绕行 / 真机彩排 / 端口清理）
docs/           技术文档
```

## 后端形态：**嵌入**，不是子进程

Release 版把 CPython 运行时 + 依赖 + 后端源码全打进 `ipod_manager.exe` 那个目录，
Python 跑在**同一进程的一条线程**里（靠 `serious_python`）。后果：

* 用户**不需要装 uv**，发行包解压即用、不需要联网初始化
* **没有子进程** → 没有孤儿进程、没有端口残留、没有"杀整棵树"
* 代价：包体大（zip 约 58 MB），且构建链变复杂（见 `release/打包说明.md`）

开发时想让 Python 跑在单独进程（改后端代码免重编译）：

```bash
export IPOD_MANAGER_EXTERNAL_BACKEND=1
uv run ipod-web --port 8765
```

`backend.dart` 两条路都留着，**改宿主层时两条都要照顾到**。

## 外部依赖与运行环境

这个项目**在源码形态下不是自包含的**，跑起来要这几样（本机都已就位）：

| 依赖 | 用途 | 位置 |
| --- | --- | --- |
| **uv** | 跑后端与测试。**发行版不需要它**（Python 已嵌入 exe） | 本机已装 |
| **Flutter** | 编译桌面端 | `C:\flutter` |
| **Node.js** | 只为「从网易云下载」服务；**发行版自带**，源码形态才需要本机装 | 本机已装 |
| **iPod 真机** | 同步目标 | 挂载为 `D:\`，`iPod_Control/` 在根目录 |

### ★ api-enhanced（网易云 API）—— 在仓库外，但**发行版自带**

「从网易云下载」的整条链路都依赖它，而**它不在本仓库里**：

```
位置：跟本仓库同级的 api-enhanced/   （用 IPOD_MANAGER_API_ENHANCED 可指定别处）
启动：node app.js          （它自己的 AGENTS.md 里也叫 pnpm start）
端口：4000
```

**发行版会把 node 运行时 + 它一起打进 `node_api/`**（见 `tools/bundle_node_api.py`），
所以最终用户不需要装 node、也不需要部署这个服务。区别在于：

* **源码形态**跑（`uv run ipod-web`）—— 需要你自己起它，否则界面显示
  「网易云服务不可用」，**下载 / 登录 / 歌单列表全部失效**。
  这是**外部依赖没起**，不是代码坏了：先
  `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:4000` 确认。
* **发行版**跑 —— 后端启动时自动拉起自带的那个（`src/ipod_cli/ncm/server.py`），
  并带**看门狗**：应用一退，node 2 秒内自退（否则孤儿占着 4000 端口，
  下次启动撞端口，报错长得像"代码坏了"）。
* 本地音乐文件导进 iPod 的路径**不需要**它，没网也能用。
* 它自己是个 Node/TypeScript 项目（pnpm），有自己的 `AGENTS.md`。
  **不要把它搬进本仓库**；打包时按路径取，不进版本库。


## 动手前必读

### 1. 命令

```bash
uv run pytest tests/ -q          # 后端测试
uv run ruff check src tests tools

cd app
export PUB_HOSTED_URL=https://pub.flutter-io.cn
export FLUTTER_STORAGE_BASE_URL=https://storage.flutter-io.cn
flutter test                     # 前端测试
flutter analyze
```

发行相关（改完后端或宿主层都要跑）：

```bash
uv run python tools/build_windows_release.py      # 构建（含嵌入 Python）
uv run python tools/verify_embedded_release.py    # 自检：起进程 + 打接口
uv run python tools/package_release.py            # 打 zip
```

**用 uv，不要 pip。** 本机 `python` 可能是别的 venv，`PYTHONPATH` 常被设成别的
site-packages 导致依赖错位——跑脚本一律 `env -u PYTHONPATH`。

> 测试条数**不要写进文档**（以前写过「555 项」「71 项」，很快就对不上了）。
> 以实际运行结果为准。

### 2. 两条硬约束（不是建议）

* **下载/请求必须低频、绝不并发。** 默认串行 + 每首间隔 ≥0.35s（后端有硬下限
  `MIN_INTERVAL_FLOOR`）+ 退避重试。网易云对高频请求会风控**封号**。
  **测试不许碰真网络**，用 mock 传输层；真机验证一次最多 3–5 首。
* **写 iPod 前先彩排。** 流程：备份 → `tools/rehearsal_from_device.py` 照抄真机
  当前状态搭彩排 → 在彩排上跑通逐项核对 → 才上真机。
  写库会整库重写，算错签名 iPod 会判定"数据库损坏"并重建。

### 3. 容易踩的坑（都踩过了）

| 坑 | 说明 |
| --- | --- |
| **行尾符分两组** | `.gitattributes` 管着，且**故意分两组**：`*.py *.dart *.md *.toml *.yaml *.json *.sh *.txt` 等是 **LF**；`*.cpp *.h *.rc *.manifest *.vcxproj *.sln *.bat *.ps1` 这些 Windows 原生文件是 **CRLF**。别一刀切、别手工改、别删这个文件——它治的正是"改两行、diff 报上千行"的老毛病 |
| **改文件前先读一遍** | 别直接整文件覆盖（不同编辑器/工具写的行尾可能不同）。改完用 `git diff --numstat` 看删除行数是否合理——「append 任务却出现大量 deletions」= 被截断了 |
| **设备要注册给内核** | 路由层拿到设备后必须 `device.activate()`（写 ArtworkDB 需要格式定义），`WebContext.device()` 里统一做了 |
| **同一份数据两条读取路径** | 改一处必然漏另一处。改动后**两条路都要验**，且要覆盖用户实际看的那条 |
| **测试别依赖跑测机器的硬件** | 曾经有测试靠"这台机器恰好没插 iPod"过关，真机一插就红 |
| **`.ncm/` 绝不能进 git** | 里面是登录 cookie。`iPod_Control` 的备份也别提交 |
| **界面文案全中文** | 包括报错、进度、表头。不要写英文再"汉化" |
| **破坏性操作要确认** | 删歌/删文件必须先预览或确认；界面上已经这么做了，别绕过去 |
| **★ 重启/重建应用前先查作业队列** | 重建要杀掉 `ipod_manager.exe`。正在同步时杀掉，文件已经拷进设备、数据库却没写成 → 设备上留下孤儿文件（iPod 看不见、白占空间，下次同步还会再拷一遍）。**动手前先 `GET /api/jobs` 看有没有 `running`/`queued`，有就问用户，别自己决定。** 查一下要几秒，杀掉一次要清理半天 |

### 4. 嵌入 Python 相关的坑（阶段 2 实测踩出来的）

| 坑 | 说明 |
| --- | --- |
| **嵌入环境里没有 `__file__`** | `app/python/main.py` 第一行若用 `Path(__file__)` 会直接 `NameError`，而表现只是"后端一直没起来"。定位自身目录改用 `sys.path` 反查 |
| **失败时不要 `sys.exit()`** | 嵌入解释器退出会把**整个 Flutter 应用**带下去（点开就闪退，连日志都看不到）。打印中文原因后停住线程，让宿主超时 |
| **`SeriousPython.terminate()` 在 Windows 上是空实现** | 所以嵌入模式下"停止/重启后端"做不到——`backend.dart` 会**如实说**，别改成假装成功 |
| **数据目录不能靠 cwd** | 宿主那句 `Directory.current = ...` 跟 Python 看到的 `os.getcwd()` 对不上。一律由 Dart 侧解析 `getApplicationSupportDirectory()` 后经环境变量显式传给 Python |
| **三个 `SERIOUS_PYTHON_*` 环境变量要覆盖两步** | `package` 和 `flutter build` 必须在**同一 shell**里都看得到，否则 site-packages / app 根本不进 bundle，而构建**照样"成功"** |
| **换 Python 版本必须 `flutter clean`** | 不然 `Lib/` 里混着别的版本的 `.pyc`，症状是 `ImportError: bad magic number in 'string'`：应用能起、Python 全无反应 |
| **Windows 没开"开发者模式"就构建不了** | Flutter 要给插件建**符号链接**，需要管理员或开发者模式。`tools/fix_plugin_symlinks.py` 用 junction（不需要权限）绕过，`build_windows_release.py` 已自动调用。注意 `flutter pub get` 本身就会因此返回非零 |
| **MSYS 路径会毁掉 pip 安装** | `/c/Users/...` 交给 Windows 原生程序会被当成 `C:\c\...`，依赖装到不存在的路径而构建"成功"。一律用 `C:/` 形式（`pwd -W`）。**同一个 bug 类还会让 `node --check /c/...` 报"模块找不到"**（Hermes 的 lint 钩子踩过）——症状是"代码语法错了"，其实路径形式错了 |
| **★ 拿应用做测试时 `Popen(stdout=PIPE)` 必须一边读** | 不读 → 管道填满 → 嵌入的 Python **阻塞在写日志上**，连作业线程一起卡住。症状是"作业一直排队、接口全超时"，极像应用坏了，实际是测试脚手架把它锁死了。`tools/verify_device_flow.py` 里的 `drain()` 就是干这个的 |
| **node 的 LICENSE 要随包分发** | node 是 MIT，官方 zip 里带 LICENSE。只解压 `node.exe` 会漏掉它（最常见的分发合规疏漏）。`bundle_node_api.py` 一起提取成 `node_api/NODE-LICENSE`，且缓存命中时会校验它在不在——不校验的话加了补丁也会静默失效 |

## 认证与本地状态

**不要往仓库里写任何令牌、cookie、账号信息。**

### GitHub

凭据从环境变量取（`GITHUB_TOKEN`）。`~/.git-credentials` 已配好，`git push`
直接走，remote URL 里不带 token。**换 harness 不受影响**——凭据在系统层。

### 网易云登录态

cookie 存在 **`ncm.db`**，已 gitignore。**位置有两处，取决于怎么跑**：

| 怎么跑 | 数据在哪 |
| --- | --- |
| **发行版 / 嵌入模式**（默认） | `%APPDATA%\com.example\ipod_manager\data\.ncm\ncm.db` |
| **命令行 / 外挂模式** | 你启动命令那个目录下的 `.ncm/ncm.db` |

* **两处是独立的库。** 在一边下载的歌，另一边不会知道——排查"为什么它说没登录 /
  说没下载"时**先确认问的是哪一个**。嵌入模式那份才是用户日常用的。
* **它不随仓库走。** 换机器 / 重新 clone 后必须重新扫码登录，否则下载失败、
  歌单拉不到。这**不是代码坏了**，别去改代码。
* `.ncm/` 里还有下载记录、歌曲缓存与作业日程（`.ncm/logs/jobs.jsonl`），
  删掉会丢历史。
* 多账号的 cookie 分开存（设置页支持切换账号）。
* 嵌入模式的迁移逻辑（`src/ipod_web/paths.py`）只在**目标不存在**时从候选位置
  复制一份过来，**不移动、不覆盖**——改那段代码时保住这两条语义，它们是
  "用户数据不会凭空消失"的判据。

## 发行版

一条命令出包，细节见 `release/打包说明.md`（含构建链、8 个坑、验证清单）。
构建产物不进版本库。

**发行包 = 构建产物 + 几个说明文件**——Python 运行时和源码都在里面了，
不再有"组装源码"这一步，也不需要 `pyproject.toml` 与 exe 同级。

## 用 DeepSeek Harness (DSH) 维护

本仓库的上下文文件**就是这份 `AGENTS.md`**——DSH 原生读它（官方 README：*"For agents,
follow AGENTS.md"*），**不需要额外建配置文件**。

```sh
npx @deepseek-ai/dsh web        # Web UI: http://127.0.0.1:3080
```

1. **Settings → Models**：填 DeepSeek API key，保存即生效，不用重启
2. **Choose workspace**：选本仓库目录（`dsh` 把启动目录当默认文件系统位置）
3. 开会话，第一句建议：「读 AGENTS.md，跑一遍测试确认环境」

> ⚠️ DSH 目前是 **developer preview**，官方明说会有破坏性变更（README 原文
> "THERE WILL BE COMPATIBILITY-BREAKING CHANGES"）。

### 交接自检（换任何 harness 都先跑这个）

```bash
uv run pytest tests/ -q                 # 后端
uv run ruff check src tests tools
cd app && flutter test && flutter analyze
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4000   # api-enhanced 在不在（期望 200）
uv run python tools/verify_embedded_release.py                   # 嵌入后端还能不能起（要已有构建产物）
```

全绿 = 环境接上了。任何时候觉得「功能坏了」，**先确认 api-enhanced 在跑、登录态
在你以为的那个 `.ncm/` 里**（见上表，有**两处**），再怀疑代码。

## 更新日志（CHANGELOG.md）

**每个版本必须留一条**，发版前写。只写用户能感知的变化——修了什么、加了什么、
行为怎么变了；重构、测试调整、文档错别字不占篇幅（要看得细有 `git log`）。

写在最上面（新的在上），版本号用语义化版本。**没发布但已经动过的改动放
`## [x.y.z] — 未发布`**，发版时把"未发布"改成日期。

写的时候说人话，别写成 commit 摘要的翻译：
"修复了 podcast_playlists 参数的默认值语义" 是坏例子；
"同一个歌单在界面上显示两条，删了像没删掉" 才是好例子。
