"""把 node 运行时 + 网易云 API（api-enhanced）打进来，供发行版自带。

## 为什么要这么做

「从网易云下载」依赖 `api-enhanced`，它是个 Node 项目。上一版发行包**不带 node**，
于是用户得自己装 node、自己 clone 部署那个 API 服务——一个"解压即用"的程序里
嵌着两步手动准备，说不通。

这一步把三样东西凑到 `app/build/node_api/`：

    node_api/
      node.exe         ← 官方 Windows 版（pin 版本，不是抓本机装的那个）
      launcher.js      ← 我方启动器：关掉版本检查 + 看门狗（父进程死则自退）
      api/             ← api-enhanced 本体（app.js / server.js / data / node_modules）

构建完由 `build_windows_release.py` 拷到 exe 同级；运行时由 Python 后端拉起。

## 关于 node 版本

pin 在 `NODE_VERSION`。**不要拿本机装的那个 node**——那台机器的版本会变，
不同人打出来的包不一样，出问题无法复现。只有下载失败时才回退到本机的（并打印警告）。

国内直连 nodejs.org 很慢，所以镜像优先、官网兜底。

## 关于 api-enhanced 的来源

它**不在本仓库里**（是独立项目，见 AGENTS.md）。这里按优先级找：

1. `IPOD_MANAGER_API_ENHANCED` 环境变量指定的目录
2. `../api-enhanced`（本机开发时的常见位置）

找到后**整体复制**（含 `node_modules`）。不做 esbuild 打包：那能省十几兆，
但多一个构建步骤和一类"打包后行为不一致"的风险，不划算。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
OUT = APP / "build" / "node_api"
LAUNCHER = APP / "node" / "launcher.js"

#: pin 住 node 版本——见模块开头
NODE_VERSION = "22.11.0"

#: 镜像优先（国内直连 nodejs.org 很慢）
NODE_URLS = (
    f"https://registry.npmmirror.com/-/binary/node/v{NODE_VERSION}/node-v{NODE_VERSION}-win-x64.zip",
    f"https://npmmirror.com/mirrors/node/v{NODE_VERSION}/node-v{NODE_VERSION}-win-x64.zip",
    f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-win-x64.zip",
)

#: api-enhanced 里运行时真正要用的东西。多带的东西（测试、docs、examples）白占体积
API_KEEP = ("app.js", "main.js", "index.js", "index.mjs", "server.js",
            "generateConfig.js", "interface.d.ts", "package.json",
            "LICENSE", "data", "node_modules", "module", "util", "plugins",
            "template", "scripts")

CACHE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ipod-node-cache"


def _download(url: str, dest: Path, timeout: int = 300) -> bool:
    print(f"  ↓ {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ipod-bundler/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp, \
                open(dest, "wb") as fh:
            shutil.copyfileobj(resp, fh, 1024 * 1024)
        return dest.is_file() and dest.stat().st_size > 10 * 1024 * 1024
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        print(f"    失败：{exc}")
        return False


def _extract_node(zip_path: Path, cached_dir: Path) -> None:
    """从官方 zip 里取出 node.exe 和它的许可，放进缓存目录。

    ★ 用**独立的临时目录**解压，别解到缓存目录本身再"移动"过去——第一版就是
    那么在写的：`parent` 和 `target_dir` 其实是同一个目录，文件原地移动之后
    紧跟的 `rmtree` 会把刚取出来的东西全删掉。症状很阴：node.exe 因为已经拷到
    目的地了所以看不出问题，只有 LICENSE 静默消失（=许可合规静默失效），
    而且缓存永远建不起来、每次构建都重新下载 34 MB。
    """
    tmp = cached_dir.parent / f".tmp-node-{os.getpid()}"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            member = next((n for n in names
                           if n.endswith("/node.exe") and n.count("/") == 1), None)
            if member is None:
                # 别让 next() 抛 StopIteration——调用方只认 RuntimeError，
                # 而且"这不是个 node 包"比"迭代器空了"能说明问题
                raise RuntimeError("zip 里没有 node.exe，不像是官方 node 包")
            top = Path(member).parent
            wanted = ["node.exe", "LICENSE", "README.md"]
            for name in wanted:
                if f"{top.as_posix()}/{name}" in names:
                    zf.extract(f"{top.as_posix()}/{name}", tmp)

        src_root = tmp / top
        if not (src_root / "node.exe").is_file():
            raise RuntimeError("zip 里没找到 node.exe")

        cached_dir.mkdir(parents=True, exist_ok=True)
        for name in wanted:
            src = src_root / name
            if src.is_file():
                shutil.copy2(src, cached_dir / name)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def fetch_node_exe(dest: Path) -> str:
    """拿到 node.exe，返回它是哪来的（写进清单，便于排查）。"""
    cached_dir = CACHE / f"node-v{NODE_VERSION}-win-x64"
    cached = cached_dir / "node.exe"

    # 缓存要**完整**才算命中：旧版本的本脚本只存了 node.exe，没有 LICENSE，
    # 命中那种缓存会让许可合规静默落空。
    if cached.is_file() and (cached_dir / "LICENSE").is_file():
        shutil.copy2(cached, dest)
        print(f"  用缓存的 node.exe（{cached.stat().st_size / 1024 / 1024:.1f} MB）")
        return f"cache:node-v{NODE_VERSION}"
    if cached_dir.exists():
        print("  缓存不完整（缺 LICENSE），重新准备")
        shutil.rmtree(cached_dir, ignore_errors=True)

    CACHE.mkdir(parents=True, exist_ok=True)
    zip_path = CACHE / f"node-v{NODE_VERSION}-win-x64.zip"

    # zip 已经下过就直接解压，别重复下载 34 MB
    if not zip_path.is_file():
        for url in NODE_URLS:
            if _download(url, zip_path):
                break
        else:
            return _fallback_local_node(dest)

    try:
        _extract_node(zip_path, cached_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"    解压失败：{exc}")
        zip_path.unlink(missing_ok=True)  # 可能是坏包，下次重下
        return _fallback_local_node(dest)

    if not cached.is_file():
        return _fallback_local_node(dest)

    shutil.copy2(cached, dest)
    print(f"  已就绪 node v{NODE_VERSION}（{dest.stat().st_size / 1024 / 1024:.1f} MB）")
    return f"download:node-v{NODE_VERSION}"


def _fallback_local_node(dest: Path) -> str:
    """下载/解压都不行时用本机的（并明确警告：这样打出来的包不可复现）。"""
    local = Path(r"C:\Program Files\nodejs\node.exe")
    if not local.is_file():
        which = shutil.which("node")
        local = Path(which) if which else local
    if local.is_file():
        shutil.copy2(local, dest)
        ver = subprocess.run([str(local), "--version"], capture_output=True,
                             text=True).stdout.strip()
        print(f"  ⚠ 下载失败，回退到本机的 node {ver}（{local}）")
        print("    ⚠ 这样打出来的包**不可复现**：别人重建会得到不同版本")
        return f"local:{ver}"
    raise SystemExit("拿不到 node.exe：下载失败，本机也没有 node")


def find_api_enhanced() -> Path:
    candidates: list[Path] = []
    env = os.environ.get("IPOD_MANAGER_API_ENHANCED")
    if env:
        candidates.append(Path(env))
    candidates.append(ROOT.parent / "api-enhanced")
    candidates.append(ROOT / "api-enhanced")

    for cand in candidates:
        if (cand / "app.js").is_file() and (cand / "node_modules").is_dir():
            return cand.resolve()
    raise SystemExit(
        "找不到 api-enhanced（要有 app.js 和 node_modules）。\n"
        "  用 IPOD_MANAGER_API_ENHANCED=<路径> 指定，或把它放在本仓库同级目录。\n"
        "  候选：" + "、".join(str(c) for c in candidates)
    )


def copy_api(src: Path, dest: Path) -> tuple[int, int]:
    dest.mkdir(parents=True, exist_ok=True)
    files = 0
    size = 0
    for name in API_KEEP:
        item = src / name
        if not item.exists():
            continue
        target = dest / name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(item, target)
    for f in dest.rglob("*"):
        if f.is_file():
            files += 1
            size += f.stat().st_size
    return files, size


def main() -> int:
    ap = argparse.ArgumentParser(description="打包 node + 网易云 API 进构建产物")
    ap.add_argument("--keep", action="store_true", help="保留已有的 node_api/（增量）")
    args = ap.parse_args()

    if not LAUNCHER.is_file():
        raise SystemExit(f"找不到启动器 {LAUNCHER}")

    if OUT.exists() and not args.keep:
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True, exist_ok=True)

    print("══ node 运行时 ══")
    source = fetch_node_exe(OUT / "node.exe")
    # 许可合规：node 是 MIT，随二进制分发要带上它的 LICENSE（见 fetch_node_exe）
    for name in ("LICENSE", "README.md"):
        src_lic = CACHE / f"node-v{NODE_VERSION}-win-x64" / name
        if src_lic.is_file():
            dst_lic = OUT / (f"NODE-{name}" if name == "LICENSE" else f"NODE-{name}")
            shutil.copy2(src_lic, dst_lic)
            print(f"  {dst_lic.name}（node 的 {name}）")

    print("══ 启动器 ══")
    shutil.copy2(LAUNCHER, OUT / "launcher.js")
    print(f"  launcher.js（{LAUNCHER.stat().st_size} 字节）")

    print("══ 网易云 API（api-enhanced）══")
    api_src = find_api_enhanced()
    print(f"  来源 {api_src}")
    files, size = copy_api(api_src, OUT / "api")
    print(f"  {files} 个文件，{size / 1024 / 1024:.1f} MB")

    version = subprocess.run([str(OUT / "node.exe"), "--version"],
                             capture_output=True, text=True).stdout.strip()
    manifest = {
        "node": version,
        "node_source": source,
        "api_source": str(api_src),
        "api_files": files,
    }
    (OUT / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    total = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print()
    print(f"✅ node_api/ 共 {total / 1024 / 1024:.1f} MB（node {version}）")
    print("   下一步：tools/build_windows_release.py 会把它拷到 exe 同级")
    return 0


if __name__ == "__main__":
    sys.exit(main())
