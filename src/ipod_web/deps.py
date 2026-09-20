"""路由共用的依赖。

单独放一个模块，免得每个路由文件都抄一遍 `request.app.state.ctx`——
抄多份的后果是改一处漏一处。
"""

from __future__ import annotations

from fastapi import Request

from ipod_web.context import WebContext


def get_ctx(request: Request) -> WebContext:
    """从 app.state 取上下文。

    用 app.state 而不是模块级全局：测试里能起多个互不干扰的实例，
    而模块级全局会让并行测试互相污染状态库。
    """
    return request.app.state.ctx


__all__ = ["get_ctx"]
