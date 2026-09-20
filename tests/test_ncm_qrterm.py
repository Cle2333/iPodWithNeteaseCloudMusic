"""终端二维码渲染的测试。

渲染错了的表现很坑：字符画看起来"有个二维码的样子"，但扫不出来，
而且不会报任何错。所以不能靠肉眼，要做**往返验证**：
自己造一张已知图案的二维码图片，渲染出来，再解析回图案，
逐格比对是不是原样。

同时验证兜底路径：渲染失败要返回 None，让调用方退回"存 PNG 给路径"，
**扫码这件事必须能完成**，不能因为装饰性功能失败就卡住登录。
"""

from __future__ import annotations

import io

import pytest

from ipod_cli.ncm.qrterm import _module_count, render_qr

PIL = pytest.importorskip("PIL", reason="渲染二维码需要 Pillow")

DEFAULT_QUIET = 2


def make_qr_png(grid: list[list[bool]], *, per_module: int = 4, quiet: int = 4) -> bytes:
    """按给定图案造一张"二维码图片"。"""
    from PIL import Image

    size = len(grid)
    px = (size + quiet * 2) * per_module
    image = Image.new("L", (px, px), 255)      # 白底
    pixels = image.load()
    for row in range(size):
        for col in range(size):
            if not grid[row][col]:
                continue
            for dy in range(per_module):
                for dx in range(per_module):
                    x = (quiet + col) * per_module + dx
                    y = (quiet + row) * per_module + dy
                    pixels[x, y] = 0           # 黑点
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def parse_rendered(art: str) -> list[list[bool]]:
    """把字符画解析回模块格。

    一个字符格 = 一个模块（上半像素/下半像素），所以每行字符就是一行模块。
    """
    rows: list[list[bool]] = []
    for line in art.split("\n"):
        row: list[bool] = []
        for char in line:
            if char == "█":
                row.append(True)          # 上下都是黑 = 整格黑
            elif char == "▀":
                row.append(True)          # 上半黑
            elif char == "▄":
                row.append(True)          # 下半黑
            else:
                row.append(False)
        rows.append(row)
    return rows


def checkerboard(size: int) -> list[list[bool]]:
    return [[(r + c) % 2 == 0 for c in range(size)] for r in range(size)]


class TestModuleCountInference:
    """模块数只可能是 21+4k，靠"除得最整"来推断。"""

    @pytest.mark.parametrize("modules", [21, 25, 29, 33, 37, 41, 45])
    def test_exact_span_is_detected(self, modules: int) -> None:
        for per_module in (3, 4, 6, 8):
            assert _module_count(modules * per_module) == modules

    def test_real_netease_qr_is_37_modules(self) -> None:
        """实测网易云二维码是 37×37 模块（148px / 4px），版本 5。"""
        assert _module_count(148) == 37

    def test_tolerates_rounding(self) -> None:
        """真实图片的黑点区域是 148px（37 模块 × 4px），±1px 取整误差要能容忍。"""
        assert _module_count(148) == 37
        assert _module_count(149) == 37

    def test_ambiguous_span_resolves_to_the_best_fit(self) -> None:
        """有些跨度有多个合法解释，挑误差最小的——把行为固定下来。

        150px 既是 25×6（误差 0）也是 37×4.05（误差 0.054），所以判成 25。
        这**不是 bug**：我们拿到的图片是精确缩放过的（网易云是 148 = 37×4），
        不存在这种歧义。但行为要固定住，免得以后改坏了没人发现。
        """
        assert _module_count(150) == 25
        # 147 = 21×7 也是完全整除
        assert _module_count(147) == 21

    def test_degenerate_one_pixel_modules_are_rejected(self) -> None:
        """1 像素/模块的二维码不可能存在（那种图上识别不出来），不能当成候选。"""
        # 149 若允许 1px/模块就会被判成"149 个模块"，实际上它是 37×4.03
        assert _module_count(149) != 149

    def test_tiny_span_does_not_crash(self) -> None:
        assert _module_count(0) >= 21
        assert _module_count(1) >= 21
        assert _module_count(5) >= 21


class TestRoundTrip:
    """★ 核心验证：造一张已知图案 → 渲染 → 解析回来 → 逐格比对。"""

    @pytest.mark.parametrize("modules", [21, 25, 33, 37, 45])
    def test_pattern_is_preserved(self, modules: int) -> None:
        grid = checkerboard(modules)

        art = render_qr(make_qr_png(grid))
        assert art is not None

        rows = parse_rendered(art)
        q = DEFAULT_QUIET
        inner = [row[q:-q] for row in rows[q:-q]]

        assert len(inner) == modules, f"行数不对：{len(inner)} != {modules}"
        assert all(len(r) == modules for r in inner)

        for row in range(modules):
            for col in range(modules):
                assert inner[row][col] == grid[row][col], f"({row},{col}) 渲染错了"

    def test_asymmetric_pattern(self) -> None:
        """棋盘格是对称的，可能掩盖方向错误——再测一个不对称的。

        图案里刻意让"行"和"列"的贡献不同，转置了就会被抓出来。
        """
        modules = 25
        grid = [[(row * 3 + col * 7) % 11 < 5 for col in range(modules)]
                for row in range(modules)]

        art = render_qr(make_qr_png(grid))
        assert art is not None

        q = DEFAULT_QUIET
        inner = [row[q:-q] for row in parse_rendered(art)[q:-q]]

        assert inner == grid

    def test_works_with_different_module_pixel_sizes(self) -> None:
        grid = checkerboard(29)

        for per_module in (3, 5, 8):
            art = render_qr(make_qr_png(grid, per_module=per_module))
            assert art is not None
            q = DEFAULT_QUIET
            inner = [r[q:-q] for r in parse_rendered(art)[q:-q]]
            assert inner == grid, f"每模块 {per_module}px 时渲染错了"

    def test_odd_module_count_aligns_to_char_rows(self) -> None:
        """★ 奇数模块数是最容易出错的（纵向劈不整齐）。

        必须保证每个字符行对应一行模块——早先的实现会让留白和第一行数据
        挤进同一个字符格，图案整体错位一格，扫不出来。
        """
        modules = 37                    # 网易云实际用的就是 37
        grid = checkerboard(modules)

        art = render_qr(make_qr_png(grid))
        assert art is not None

        rows = parse_rendered(art)
        # 37 + 2*2 = 41 个字符行；每行 41 个字符
        assert len(rows) == modules + DEFAULT_QUIET * 2
        assert all(len(row) == modules + DEFAULT_QUIET * 2 for row in rows)

        q = DEFAULT_QUIET
        assert [row[q:-q] for row in rows[q:-q]] == grid


class TestOutputShape:
    def test_square_modules(self) -> None:
        """字符格宽 1 高 2，所以一个字符格正好是一个模块——行列数该相等。"""
        art = render_qr(make_qr_png(checkerboard(21)))
        assert art is not None
        lines = art.split("\n")
        assert len(lines) == 21 + DEFAULT_QUIET * 2
        assert all(len(line) == 21 + DEFAULT_QUIET * 2 for line in lines)

    def test_quiet_zone_is_present(self) -> None:
        """四周必须有留白，否则手机识别率明显下降。"""
        art = render_qr(make_qr_png(checkerboard(21)))
        assert art is not None
        lines = art.split("\n")
        q = DEFAULT_QUIET
        for line in lines[:q] + lines[-q:]:
            assert set(line) == {" "}, "上下留白不干净"
        for line in lines:
            assert line[:q].strip() == "" and line[-q:].strip() == "", "左右留白不干净"

    def test_larger_quiet_zone(self) -> None:
        art = render_qr(make_qr_png(checkerboard(21)), quiet=4)
        assert art is not None
        lines = art.split("\n")
        assert len(lines) == 21 + 8
        for line in lines[:4] + lines[-4:]:
            assert set(line) == {" "}


class TestFailureFallback:
    """渲染失败必须返回 None，而不是抛异常或返回半张图。"""

    def test_garbage_bytes(self) -> None:
        assert render_qr(b"not an image at all") is None

    def test_empty(self) -> None:
        assert render_qr(b"") is None

    def test_all_white_image_is_rejected(self) -> None:
        """全白图里没有二维码——不能返回一张空白字符画骗调用方。"""
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("L", (100, 100), 255).save(buffer, format="PNG")
        assert render_qr(buffer.getvalue()) is None

    def test_tiny_image_is_rejected(self) -> None:
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("L", (4, 4), 0).save(buffer, format="PNG")
        assert render_qr(buffer.getvalue()) is None
