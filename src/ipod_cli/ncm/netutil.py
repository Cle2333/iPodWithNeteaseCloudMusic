"""网络读取的公共工具：**掐总时长**的读响应。

## 为什么需要它

`urllib.request.urlopen(url, timeout=N)` 里的 `timeout` 是**每次 recv 的超时**，
不是"这次请求总共最多花多久"。服务器只要慢慢吐数据（比如每 50 秒一个字节，
或者响应头分几次发），每次 recv 都算"没超时"，于是这个 `timeout` **永远不会触发**。

实测踩到的后果：

    同步 147 首时应用卡死，Windows 判定"未响应"然后被杀。
    抓到的调用栈是：

        ssl.py     in read          ←
        socket.py  in readinto      ←  一直在等网络
        http/client.py in _read_status
        ncm/client.py  in cover_bytes
        ncm/downloader.py in download_song

    也就是说：它在**等一个永远不来的响应**，而 socket 超时没能救它。
    应用卡在那个等待上，界面的每个请求跟着一起超时 —— 用户看到的就是
    "点了同步，然后整个应用没反应了"。

所以凡是读响应体/响应头的地方，都要自己拿 `time.monotonic()` 掐一个**墙钟
上限**：不管 socket 层怎么想，超过这个总时长就放弃。
"""

from __future__ import annotations

import time

#: 读一个响应体时，默认的总时长上限（秒）。
DEFAULT_READ_SECONDS = 120.0


class ReadTimeoutError(TimeoutError):
    """读取响应超过了墙钟上限。"""


def read_with_deadline(
    response,
    *,
    seconds: float = DEFAULT_READ_SECONDS,
    chunk_size: int = 1 << 16,
    max_bytes: int | None = None,
) -> bytes:
    """把响应体读完，但**总时长**超过 ``seconds`` 就放弃。

    ``max_bytes`` 是体积上限（防止对端一直吐直到把内存吃光）——传了的话
    超过就抛 :class:`ValueError`，交给调用方决定怎么报错。
    """
    deadline = time.monotonic() + max(float(seconds), 0.001)
    chunks: list[bytes] = []
    total = 0
    while True:
        if time.monotonic() > deadline:
            raise ReadTimeoutError(
                f"读取响应超过 {seconds:.0f} 秒仍未结束，已放弃"
                f"（已收到 {total / 1048576:.1f} MB）"
            )
        chunk = response.read(chunk_size)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if max_bytes is not None and total > max_bytes:
            raise ValueError(
                f"响应体超过上限 {max_bytes // 1048576} MB，已中止"
            )
        chunks.append(chunk)
