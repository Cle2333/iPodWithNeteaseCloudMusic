# AGENTS.md

给 AI 助手（Claude Code / Hermes / Cursor 等）的项目上下文。人看的说明在 `README.md`。

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

## 动手前必读

### 1. 命令

```bash
uv run pytest tests/ -q          # 后端测试（555 项）
uv run ruff check src tests tools

cd app
export PUB_HOSTED_URL=https://pub.flutter-io.cn
export FLUTTER_STORAGE_BASE_URL=https://storage.flutter-io.cn
flutter test                     # 前端测试（71 项）
flutter analyze
```

**用 uv，不要 pip。** 本机 `python` 可能是别的 venv，`PYTHONPATH` 常被设成别的
site-packages 导致依赖错位——跑脚本一律 `env -u PYTHONPATH`。

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
| **行尾符** | 仓库统一 LF（`.gitattributes` 管着）。别手工改行尾，也别删这个文件——它治的正是"改两行、diff 报上千行"的老毛病 |
| **`patch` vs `write_file`** | 两个工具写的行尾不同；改文件前**先读一遍**，别直接整文件覆盖 |
| **设备要注册给内核** | 路由层拿到设备后必须 `device.activate()`（写 ArtworkDB 需要格式定义），`WebContext.device()` 里统一做了 |
| **同一份数据两条读取路径** | 改一处必然漏另一处。改动后**两条路都要验**，且要覆盖用户实际看的那条 |
| **测试别依赖跑测机器的硬件** | 曾经有测试靠"这台机器恰好没插 iPod"过关，真机一插就红 |
| **`.ncm/` 绝不能进 git** | 里面是登录 cookie。`iPod_Control` 的备份也别提交 |
| **界面文案全中文** | 包括报错、进度、表头。不要写英文再"汉化" |
| **破坏性操作要确认** | 删歌/删文件必须先预览或确认；界面上已经这么做了，别绕过去 |

## 认证

**不要往仓库里写任何令牌、cookie、账号信息。** 需要 GitHub 凭据时从环境变量取
（`GITHUB_TOKEN`，存在 Hermes 的 `.env` 里，不在本仓库）。`~/.git-credentials`
已配好，`git push` 直接走，remote URL 里不带 token。

## 发行版

见 `release/打包说明.md`（含目录结构、四个坑、验证清单）。
构建产物不进版本库。
