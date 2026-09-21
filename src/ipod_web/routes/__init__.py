"""ipod-web 的路由模块。

每个模块一个功能域，全部挂在 ``/api`` 下：

* ``status``   —— 就绪探测、状态条、设备信息
* ``account``  —— 扫码登录、切换/移除账号
* ``settings`` —— 音质、请求间隔、本地缓存
* ``library``  —— iPod 上的本地音乐（列表 / 导入 / 删除 / 健康检查）
* ``repair``   —— 设备修复（扫孤儿文件、断链记录、残留临时文件并清理）
* ``jobs``     —— 作业队列（列表/详情/取消）
* ``debug``    —— 后端日志、环境信息、环境自检

共用的依赖（取上下文）在 ``ipod_web.deps``。
"""

from __future__ import annotations

__all__: list[str] = []
