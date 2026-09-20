"""`tools/bundle_node_api.py` 的回归测试。

## 为什么值得测

打包器有两个"错了也看不出来"的失败模式，都是实际踩出来的：

1. **解压后自删**：第一版把 zip 解到缓存目录本身、再"移动到缓存目录"——
   两个路径其实是同一个，文件原地移动后紧跟的 `rmtree` 把刚取出来的东西全删了。
   `node.exe` 因为已经被拷到目的地所以**看不出问题**，只有 `LICENSE` 静默消失
   （=许可合规静默失效），而且缓存永远建不起来、每次构建白下 34 MB。
2. **缓存不完整也算命中**：老缓存里没有 LICENSE，命中它就等于跳过许可。

跑真网络来验太慢，所以这里造一个**假 node zip**，把两条路径都走一遍。
"""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load_module():
    """按路径加载 tools/bundle_node_api.py（tools/ 不是包）。"""
    spec = importlib.util.spec_from_file_location(
        "bundle_node_api_under_test", REPO / "tools" / "bundle_node_api.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def fake_zip(tmp_path: Path) -> Path:
    """造一个布局跟官方 node zip 一样的假包。"""
    zip_path = tmp_path / "node-v9.9.9-win-x64.zip"
    top = "node-v9.9.9-win-x64"
    payload = {
        "node.exe": b"MZ fake node binary" * 100,
        "LICENSE": b"Node.js is licensed for use as follows:\n\"\"\"\nMIT\n",
        "README.md": b"# Node.js\n",
        "npm.cmd": b"@echo off\n",  # 我们不要的，确保没被一起拿进来
    }
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name, data in payload.items():
            zf.writestr(f"{top}/{name}", data)
    return zip_path


def test_extract_keeps_licence_and_binary(tmp_path: Path, fake_zip: Path) -> None:
    """提取后缓存目录里必须**同时**有 node.exe 和 LICENSE。

    这是那个"自删"bug 的回归点：只看 node.exe 在不在是抓不到的——它当时确实在。
    """
    mod = _load_module()
    cached_dir = tmp_path / "cache" / "node-v9.9.9-win-x64"

    mod._extract_node(fake_zip, cached_dir)

    assert (cached_dir / "node.exe").is_file(), "node.exe 不在缓存里（被 rmtree 删了？）"
    assert (cached_dir / "LICENSE").is_file(), "LICENSE 不在缓存里 —— 许可合规会静默失效"
    assert (cached_dir / "README.md").is_file()
    # 缓存目录里不该出现解压残留的另一层目录
    assert not (cached_dir / "node-v9.9.9-win-x64").exists()


def test_extract_leaves_no_temp_dirs(tmp_path: Path, fake_zip: Path) -> None:
    """临时解压目录必须清干净（失败路径也要清）。"""
    mod = _load_module()
    cache_root = tmp_path / "cache"
    cached_dir = cache_root / "node-v9.9.9-win-x64"

    mod._extract_node(fake_zip, cached_dir)

    leftovers = [p.name for p in cache_root.iterdir() if p.name.startswith(".tmp-node")]
    assert leftovers == [], f"临时目录没清：{leftovers}"


def test_extract_raises_when_no_node_exe(tmp_path: Path) -> None:
    """zip 里没有 node.exe 时要报错，不能"成功"返回一个空缓存。"""
    mod = _load_module()
    bad_zip = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("node-v9.9.9-win-x64/README.md", b"nothing useful")

    with pytest.raises(RuntimeError, match="node\\.exe"):
        mod._extract_node(bad_zip, tmp_path / "cache" / "node-v9.9.9-win-x64")


def test_cache_hit_requires_licence(tmp_path: Path) -> None:
    """只有 node.exe、没有 LICENSE 的缓存**不算命中**。

    旧版本的脚本就是那种缓存；命中它会让许可合规静默落空。
    """
    mod = _load_module()
    cache_root = tmp_path / "cache"
    cached_dir = cache_root / f"node-v{mod.NODE_VERSION}-win-x64"
    cached_dir.mkdir(parents=True)
    (cached_dir / "node.exe").write_bytes(b"MZ old cache without licence")
    dest = tmp_path / "out" / "node.exe"
    dest.parent.mkdir(parents=True)

    with mock.patch.object(mod, "CACHE", cache_root), \
            mock.patch.object(mod, "_download", return_value=False):
        try:
            source = mod.fetch_node_exe(dest)
        except SystemExit:
            # 本机没装 node 时，下载又失败 → 干脆报错退出。那也是"没把不完整的
            # 缓存当命中"，同样合格。
            return

    # 缓存不完整 → 既不该当作命中，也不该糊弄成 download: 成功
    assert not source.startswith("cache:"), f"不完整的缓存被当成命中了：{source}"


def test_cache_hit_uses_complete_cache(tmp_path: Path) -> None:
    """完整的缓存要真的被用上（否则每次构建都白下 34 MB）。"""
    mod = _load_module()
    cache_root = tmp_path / "cache"
    cached_dir = cache_root / f"node-v{mod.NODE_VERSION}-win-x64"
    cached_dir.mkdir(parents=True)
    (cached_dir / "node.exe").write_bytes(b"MZ cached node" * 50)
    (cached_dir / "LICENSE").write_text("MIT", encoding="utf-8")
    dest = tmp_path / "out" / "node.exe"
    dest.parent.mkdir(parents=True)

    with mock.patch.object(mod, "CACHE", cache_root):
        source = mod.fetch_node_exe(dest)

    assert source == f"cache:node-v{mod.NODE_VERSION}"
    assert dest.read_bytes().startswith(b"MZ cached node")


def test_bundled_api_keep_list_covers_entries() -> None:
    """API_KEEP 必须带 app.js（没有它自带的 API 起不来）和 LICENSE（合规）。"""
    mod = _load_module()
    assert "app.js" in mod.API_KEEP
    assert "LICENSE" in mod.API_KEEP
    assert "node_modules" in mod.API_KEEP
