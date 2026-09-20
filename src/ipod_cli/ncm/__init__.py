"""网易云 → iPod 同步。

四个模块，职责单向依赖，便于单独测试：

    state.py       状态库（SQLite）：多账号 cookie、歌曲映射、下载缓存
    client.py      网易云 API 客户端：串行限速、退避重试、扫码登录
    downloader.py  下载 + 写标签 + 内嵌封面，产出"能直接喂给 ipod import"的文件
    sync.py        同步引擎：diff → 下载 → 导入 → 建 iPod 播放列表

``sync.py`` 只依赖前三个和 ipod_cli 现有内核，**不依赖任何 Web 框架**——
这样同步逻辑能用虚拟 iPod 单测，前端只是它的壳。
"""
