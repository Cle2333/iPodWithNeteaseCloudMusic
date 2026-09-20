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
app/            Flutter 桌面端（Dart）
tests/          pytest（真实网络一律不碰，靠 mock 传输层）
app/test/       Flutter 测试
docs/           技术文档
```

## 外部依赖与运行环境

这个项目**不是自包含的**，跑起来要这几样（本机都已就位）：

| 依赖 | 用途 | 位置 |
| --- | --- | --- |
| **uv** | 跑后端与测试 | 本机已装 |
| **Flutter** | 编译桌面端 | `C:\flutter` |
| **Node.js** | 只为「从网易云下载」服务（见下） | 本机已装 |
| **iPod 真机** | 同步目标 | 挂载为 `D:\`，`iPod_Control/` 在根目录 |

### ★ api-enhanced（网易云 API）—— 在仓库外！

「从网易云下载」的整条链路都依赖它，但**它不在本仓库里**：

```
位置：C:\Users\ROG\source\repos\api-enhanced
启动：node app.js          （它自己的 AGENTS.md 里也叫 pnpm start）
端口：4000
```

后端通过 `--base-url` 连它（默认 `localhost:4000`，见 `src/ipod_web/app.py:105`）。

* 没起它 → 界面显示「网易云服务不可用」，**下载 / 登录 / 歌单列表全部失效**。
  这是**外部依赖没起**，不是代码坏了——先 `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:4000` 确认。
* 本地音乐文件导进 iPod 的路径**不需要**它，没网也能用。
* 它自己是个 Node/TypeScript 项目（pnpm），有自己的 `AGENTS.md`。
  **不要把它搬进本仓库**，也不要假设它随本仓库一起 clone。


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

## 认证与本地状态

**不要往仓库里写任何令牌、cookie、账号信息。**

### GitHub

凭据从环境变量取（`GITHUB_TOKEN`）。`~/.git-credentials` 已配好，`git push`
直接走，remote URL 里不带 token。**换 harness 不受影响**——凭据在系统层。

### 网易云登录态

cookie 存在 **`.ncm/ncm.db`**，已 gitignore，因此：

* **它不随仓库走。** 换机器 / 重新 clone 后**必须重新扫码登录**，否则下载失败、
  歌单拉不到。这**不是代码坏了**，别去改代码。
* `.ncm/` 里还有下载记录与作业日程（`.ncm/logs/jobs.jsonl`），删掉会丢历史。
* 多账号的 cookie 分开存（设置页支持切换账号）。

## 发行版

见 `release/打包说明.md`（含目录结构、四个坑、验证清单）。
构建产物不进版本库。

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
```

全绿 = 环境接上了。任何时候觉得「功能坏了」，**先确认 api-enhanced 在跑、
`.ncm/` 里还有登录态**，再怀疑代码。

