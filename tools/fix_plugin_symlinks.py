"""给 Windows 插件预建 junction，绕过 Flutter 的软链权限要求。

## 为什么需要这个

`flutter build` 给每个插件在 `windows/flutter/ephemeral/.plugin_symlinks/` 下建一个
**符号链接**。Windows 上创建符号链接要么需要管理员权限，要么需要打开"开发者模式"
（设置 → 隐私和安全性 → 开发者选项，需要管理员）。

没开的话构建直接失败：

    Building with plugins requires symlink support.
    Please enable Developer Mode in your system settings.

## 绕过原理

Flutter 建链接前会检查目标是否已存在（`flutter_plugins.dart` 里
`if (link.existsSync()) { continue; }`）。而 **junction**（目录联接）在 Windows 上
**不需要任何特殊权限**就能创建。所以预先建好 junction，Flutter 就会跳过，构建通过。

PowerShell 的 `New-Item -ItemType Junction` 即可，不弹 UAC。

## 什么时候要跑

`flutter pub get` 之后、`flutter build` 之前。插件列表变了（加减依赖）就要重跑。
`flutter clean` 也会清掉 `.plugin_symlinks`，之后同样要重跑。

## 用法

    uv run python tools/fix_plugin_symlinks.py
    # 或指定项目根
    uv run python tools/fix_plugin_symlinks.py --project app
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def _plugins(project: Path, platform: str) -> list[tuple[str, str]]:
    """从 .flutter-plugins-dependencies 里读出该平台的插件 (名字, 路径)。"""
    manifest = project / ".flutter-plugins-dependencies"
    if not manifest.is_file():
        raise SystemExit(
            f"找不到 {manifest.name}——先跑一次 `flutter pub get` 生成它。"
        )
    data = json.loads(manifest.read_text(encoding="utf-8"))
    entries = data.get("plugins", {}).get(platform, [])
    return [(e["name"], e["path"]) for e in entries if "name" in e and "path" in e]


def _link_type(path: Path) -> str:
    """返回 'Junction' / 'SymbolicLink' / 'Directory' / ''（不存在）。"""
    if not path.exists() and not path.is_symlink():
        # 断链的链接在 Windows 上 exists() 可能为 False，再探一次
        if not path.parent.is_dir() or path.name not in {
            p.name for p in path.parent.iterdir()
        }:
            return ""
    ps = (
        "$i = Get-Item -LiteralPath '"
        + str(path).replace("'", "''")
        + "' -Force -ErrorAction SilentlyContinue; "
        "if ($i) { if ($i.LinkType) { $i.LinkType } else { 'Directory' } }"
    )
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    ).stdout.strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="给 Flutter 插件预建 junction")
    ap.add_argument(
        "--project",
        default=".",
        help="Flutter 项目目录（含 pubspec.yaml），默认当前目录",
    )
    ap.add_argument("--platform", default="windows", help="目标平台，默认 windows")
    args = ap.parse_args()

    project = Path(args.project).resolve()
    if not (project / "pubspec.yaml").is_file():
        raise SystemExit(f"{project} 里没有 pubspec.yaml，不是 Flutter 项目目录")

    entries = _plugins(project, args.platform)
    if not entries:
        print(f"  这个项目没有 {args.platform} 插件，不用建链接。")
        return 0

    link_dir = project / "windows" / "flutter" / "ephemeral" / ".plugin_symlinks"
    link_dir.mkdir(parents=True, exist_ok=True)

    made = skipped = failed = 0
    for name, path in entries:
        link = link_dir / name
        kind = _link_type(link)

        if kind in ("Junction", "SymbolicLink"):
            print(f"  = {name:28} 已有 {kind}，跳过")
            skipped += 1
            continue

        if kind == "Directory":
            # 真目录（不是链接）——Flutter 自己解的，别动它
            print(f"  = {name:28} 是真实目录，跳过")
            skipped += 1
            continue

        ps = (
            "New-Item -ItemType Junction -Path '"
            + str(link).replace("'", "''")
            + "' -Target '"
            + path.rstrip("\\/").replace("'", "''")
            + "' | Out-Null"
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        if r.returncode != 0 or not link.exists():
            print(f"  ✗ {name:28} 建链接失败：{(r.stderr or '').strip()[:120]}")
            failed += 1
        else:
            print(f"  + {name:28} 已建 junction")
            made += 1

    print(f"\n  新建 {made} · 跳过 {skipped} · 失败 {failed}")
    if failed:
        print(
            "  失败的话请打开开发者模式（需要管理员）：\n"
            "    start ms-settings:developers"
        )
        return 1
    print("  现在可以 `flutter build windows` 了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
