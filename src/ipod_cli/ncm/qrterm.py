"""终端二维码渲染。

登录要扫码，而命令行里没法显示图片。所以把二维码转成**半块字符**画出来，
直接用手机扫终端就行，不用再去开一个图片文件。

原理：终端字符格大致是"宽 1 高 2"，所以用 ``▀``(上) ``▄``(下) ``█``(双)
`` ``(空) 四种字符，一个字符格表示两个像素点，纵横比刚好对得上。

渲染不出来的话（图片格式意外、Pillow 缺失）就返回 None，
调用方退回"把 PNG 存文件并打印路径"——**扫码是必须能完成的**，
不能因为装饰性的东西失败就卡住主流程。
"""

from __future__ import annotations

import io

#: QR 的模块数一定是 21 + 4*k（版本 1 是 21×21，版本 40 是 177×177）
_MODULE_COUNTS = tuple(21 + 4 * k for k in range(40))

_BLOCKS = {
    (True, True): "█",
    (True, False): "▀",
    (False, True): "▄",
    (False, False): " ",
}


def _module_count(pixel_span: int) -> int:
    """猜二维码是多少模块宽。

    图片里的黑点区域宽度 = 模块数 × 每模块像素数。模块数只可能是
    21/25/29/…，所以挑"除得最整"的那个——这样能容忍图片被缩放过。

    **每模块至少要有 2 像素**：1 像素/模块的二维码是不可能存在的
    （那种图上手机根本识别不出来）。不排除的话，149 像素会被误判成
    "149 个模块 × 1 像素"——因为那个"除得最整"（误差 0）。
    """
    best, best_error = 0, None
    fallback = 0
    for count in _MODULE_COUNTS:
        per_module = pixel_span / count
        error = abs(per_module - round(per_module))
        if fallback == 0 and per_module >= 2:
            fallback = count
        if per_module < 2:
            continue
        if best_error is None or error < best_error:
            best, best_error = count, error
    return best or fallback or 21


def render_qr(png_bytes: bytes, *, quiet: int = 2) -> str | None:
    """把二维码 PNG 画成可直接扫描的字符画。

    一个模块正好占一个字符格：字符格是"宽 1 像素列、高 2 像素行"，
    所以模块纵向复制成 2 个像素行，得到的字符画是 N 行 × N 列的方格，
    纵横比正确，而且每行正好对应一行模块。

    （早先的写法是直接把模块当 1 像素行，遇到奇数模块数时纵向劈不整齐，
    留白会和第一行数据挤进同一个字符格——往返测试就是这么发现问题。）

    ``quiet`` 是四周留白（模块数），默认 2：一个字符行的上下留白。
    完全没有留白手机识别率会明显下降。
    """
    try:
        from PIL import Image
    except ImportError:
        return None

    try:
        image = Image.open(io.BytesIO(png_bytes)).convert("L")
    except Exception:
        return None

    width, height = image.size
    if width < 8 or height < 8:
        return None

    pixels = image.load()

    # 1. 找出黑点区域（去掉二维码自带的留白）
    left, top, right, bottom = width, height, -1, -1
    for y in range(height):
        for x in range(width):
            if pixels[x, y] < 128:
                left, right = min(left, x), max(right, x)
                top, bottom = min(top, y), max(bottom, y)
    if right < left or bottom < top:
        return None

    # 2. 推断模块数，再按模块中心采样（比直接缩放稳，不怕留白不均）
    modules = _module_count(max(right - left + 1, bottom - top + 1))
    span_x = (right - left + 1) / modules
    span_y = (bottom - top + 1) / modules

    grid: list[list[bool]] = []
    for row in range(modules):
        line: list[bool] = []
        for col in range(modules):
            cx = int(left + (col + 0.5) * span_x)
            cy = int(top + (row + 0.5) * span_y)
            cx = min(max(cx, 0), width - 1)
            cy = min(max(cy, 0), height - 1)
            line.append(pixels[cx, cy] < 128)
        grid.append(line)

    # 3. 纵向复制：每个模块占两个像素行 = 一个字符格
    doubled: list[list[bool]] = []
    for row in grid:
        doubled.append(row)
        doubled.append(row)

    # 4. 加留白。横向 quiet 列，纵向 quiet*2 个像素行（= quiet 个字符行）
    total_cols = modules + quiet * 2
    blank = [False] * total_cols
    padded = (
        [blank] * (quiet * 2)
        + [[False] * quiet + row + [False] * quiet for row in doubled]
        + [blank] * (quiet * 2)
    )

    lines: list[str] = []
    for index in range(0, len(padded), 2):
        upper, lower = padded[index], padded[index + 1]
        lines.append(
            "".join(_BLOCKS[(upper[c], lower[c])] for c in range(total_cols))
        )
    return "\n".join(lines)
