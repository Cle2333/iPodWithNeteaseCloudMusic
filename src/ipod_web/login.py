"""扫码登录。

登录跑成**一个作业**，不是一串无状态的 HTTP 请求。理由：

* 扫码是"等用户动作"的长流程（最长 3 分钟），中间要反复轮询网易云。
  挂在队列上，它和下载/写 iPod 就天然不会互相穿插——用户扫着码的同时
  不会有个同步作业在改 cookie。
* 取消是现成的：作业取消了，轮询循环在下一个检查点就停。

界面拿二维码的方式：路由提交作业后**等一小会儿**（``ready`` 事件），
拿到 PNG 就返回。等不到说明服务有问题，也不用干等 3 分钟。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ipod_web.context import WebContext
from ipod_web.jobs import JobCancelled, JobHandle

#: 二维码有效期。和 CLI 里的登录流程保持一致。
QR_TIMEOUT_SECONDS = 180.0

#: 轮询间隔。2 秒是"够及时"和"别太频繁"之间的平衡点。
POLL_INTERVAL = 2.0

#: 网易云扫码接口的状态码。
QR_CODE_EXPIRED = 800
QR_CODE_WAITING = 801
QR_CODE_SCANNED = 802
QR_CODE_CONFIRMED = 803

_CODE_MESSAGE: dict[int, str] = {
    QR_CODE_EXPIRED: "二维码已过期，请重新获取",
    QR_CODE_WAITING: "等待扫码…",
    QR_CODE_SCANNED: "已扫描，请在手机上点确认",
    QR_CODE_CONFIRMED: "授权成功",
}

_CODE_STATE: dict[int, str] = {
    QR_CODE_EXPIRED: "expired",
    QR_CODE_WAITING: "waiting",
    QR_CODE_SCANNED: "scanned",
    QR_CODE_CONFIRMED: "confirmed",
}


@dataclass
class LoginSession:
    """一次扫码登录的可观测状态。界面轮询它来更新二维码和文案。"""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: str = "starting"
    message: str = "正在获取二维码…"
    image: bytes = b""
    account: dict[str, Any] | None = None
    request_count: int = 0
    started_at: float = field(default_factory=time.time)

    #: 跑这次登录的作业 ID。取消登录就是取消这个作业。
    job_id: str = ""

    #: 二维码拿到后置位。路由靠它决定"返回图"还是"报错"。
    ready: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def finished(self) -> bool:
        return self.state in {"confirmed", "expired", "failed", "cancelled"}

    def as_dict(self, *, with_image: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "session_id": self.id,
            "state": self.state,
            "message": self.message,
            "finished": self.finished,
            "request_count": self.request_count,
            "account": self.account,
        }
        if with_image:
            import base64

            payload["image_base64"] = (
                base64.b64encode(self.image).decode("ascii") if self.image else ""
            )
        return payload


def run_login(ctx: WebContext, handle: JobHandle, session: LoginSession) -> dict[str, Any]:
    """作业体：取二维码 → 轮询到确认/过期/取消。

    这个函数只改 ``session``，不返回值驱动界面——界面所有信息都从
    session 读，避免"作业结束了但界面还在转圈"的错位。
    """
    client = ctx.client()

    # ① 取二维码
    try:
        unikey, png = client.qr_login_start()
    except Exception as exc:  # noqa: BLE001 - 任何失败都要变成界面能读懂的文案
        session.state = "failed"
        session.message = f"获取二维码失败：{exc}"
        session.ready.set()
        handle.log(f"获取二维码失败：{exc}", level="error")
        return {"ok": False, "error": str(exc)}

    session.image = png
    session.state = "waiting"
    session.message = _CODE_MESSAGE[QR_CODE_WAITING]
    session.request_count = client.request_count
    session.ready.set()
    handle.log("二维码已生成，等待用户扫码")

    # ② 轮询
    deadline = time.monotonic() + QR_TIMEOUT_SECONDS
    last_code: int | None = None
    confirmed_cookie = ""

    try:
        while time.monotonic() < deadline:
            handle.check_cancelled()
            try:
                code, cookie = client.qr_login_poll(unikey)
            except Exception as exc:  # noqa: BLE001
                # 单次轮询失败不该终结整个登录：网络抖一下很正常，
                # 记一笔继续轮询，用户那边二维码还开着。
                handle.log(f"轮询失败，稍后重试：{exc}", level="warn")
                _sleep_cancellable(handle, POLL_INTERVAL)
                continue

            session.request_count = client.request_count

            if code != last_code:
                session.state = _CODE_STATE.get(code, session.state)
                session.message = _CODE_MESSAGE.get(code, f"未知状态 {code}")
                handle.log(f"扫码状态：{session.message}")
                last_code = code

            if code == QR_CODE_CONFIRMED:
                confirmed_cookie = cookie
                break
            if code == QR_CODE_EXPIRED:
                session.ready.set()
                return {"ok": False, "error": "二维码已过期"}

            _sleep_cancellable(handle, POLL_INTERVAL)
        else:
            session.state = "expired"
            session.message = "等待扫码超时，请重新获取二维码"
            handle.log("等待扫码超时", level="warn")
            return {"ok": False, "error": "超时"}

        # ③ 拿 cookie 换账号信息并存档
        if not confirmed_cookie:
            session.state = "failed"
            session.message = "授权成功但没拿到凭据，请重试"
            handle.log("授权成功但 cookie 为空", level="error")
            return {"ok": False, "error": "cookie 为空"}

        handle.log("授权成功，正在读取账号信息…")
        from ipod_cli.ncm.client import NcmClient

        probe = NcmClient(
            ctx.base_url, cookie=confirmed_cookie, min_interval=ctx.min_interval()
        )
        account = probe.login_status()
        session.request_count += probe.request_count

        ctx.store.save_account(
            account.uid,
            account.cookie,
            nickname=account.nickname,
            vip_type=account.vip_type,
        )
        ctx.store.set_active_uid(account.uid)

        session.state = "confirmed"
        session.message = f"已登录：{account.nickname}"
        session.account = {
            "uid": account.uid,
            "nickname": account.nickname,
            "vip": bool(account.vip_type),
            "vip_type": account.vip_type,
        }
        handle.log(
            f"登录成功：{account.nickname}（uid={account.uid}，"
            f"{'VIP' if account.vip_type else '普通用户'}）"
        )
        return {"ok": True, "account": session.account}

    except JobCancelled:
        # 用户点了取消。**改了 session 再抛**，不放任异常直接穿过去——
        # 否则界面会一直停在"等待扫码…"，而作业其实已经结束了。
        session.state = "cancelled"
        session.message = "已取消登录"
        handle.log("登录已取消")
        raise


def _sleep_cancellable(handle: JobHandle, seconds: float) -> None:
    """可被取消打断的等待。

    直接 ``time.sleep(2)`` 的话，用户点取消后最坏要等 2 秒才停；
    切成 0.1 秒一片，取消基本是立刻生效。
    """
    remaining = seconds
    while remaining > 0:
        handle.check_cancelled()
        slice_ = min(0.1, remaining)
        time.sleep(slice_)
        remaining -= slice_


__all__ = [
    "POLL_INTERVAL",
    "QR_TIMEOUT_SECONDS",
    "LoginSession",
    "run_login",
]
