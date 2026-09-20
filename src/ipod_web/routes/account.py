"""账号：扫码登录、切换、移除。

登录是个长流程（等用户扫码，最长 3 分钟），所以它跑成**一个作业**，
接口只负责"起作业 / 读状态 / 取消"。界面拿到的二维码是 PNG 的 base64，
直接塞进 ``Image.memory`` 就能显示——不用落盘、不用外部查看器。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ipod_web.context import WebContext
from ipod_web.deps import get_ctx
from ipod_web.login import LoginSession, run_login

router = APIRouter(prefix="/api/account", tags=["账号"])

#: 路由等二维码的最长时间（秒）。
#:
#: 作业是异步跑的，路由得等它把图取回来才能返回。所以这里"等一小会儿"
#: 而不是立刻返回空图——但也别死等：取二维码失败的话，界面应该马上
#: 拿到错误信息，而不是转 3 分钟圈。
QR_READY_WAIT = 20.0

_IDLE: dict[str, Any] = {
    "session_id": "",
    "state": "idle",
    "message": "未在登录",
    "finished": True,
    "request_count": 0,
    "account": None,
    "image_base64": "",
}


class UseRequest(BaseModel):
    uid: int


class RemoveRequest(BaseModel):
    uid: int


@router.get("/login")
def login_state(
    with_image: bool = False, ctx: WebContext = Depends(get_ctx)
) -> dict[str, Any]:
    """当前登录会话的状态。界面轮询这个来换二维码下方的提示文案。"""
    if ctx.login is None:
        return dict(_IDLE)
    return ctx.login.as_dict(with_image=with_image)


@router.post("/login")
def login_start(ctx: WebContext = Depends(get_ctx)) -> dict[str, Any]:
    """开始扫码登录，返回二维码。

    **幂等**：已经在登录中就直接返回现状，不会再起一个作业——
    连点两次"登录"不该拿到两张互相顶掉的二维码。
    """
    if ctx.login is not None and not ctx.login.finished:
        ctx.login.ready.wait(timeout=QR_READY_WAIT)
        return ctx.login.as_dict(with_image=True)

    session = LoginSession()
    ctx.login = session
    job = ctx.jobs.submit("login", "扫码登录", lambda handle: run_login(ctx, handle, session))
    session.job_id = job.id

    if not session.ready.wait(timeout=QR_READY_WAIT):
        # 作业还在跑但二维码没出来——多半是连不上本地 API 服务
        raise HTTPException(
            status_code=503,
            detail=(
                "取二维码超时。请确认本地网易云 API 服务已启动"
                f"（{ctx.base_url}），详见设置里的后端日志。"
            ),
        )

    if session.state == "failed":
        # 取二维码失败最常见的原因就是本地 API 服务没起来。把排查方向
        # 直接写出来——让用户对着一句 "connection refused" 自己猜，等于没提示。
        raise HTTPException(
            status_code=502,
            detail=(
                f"{session.message}。"
                f"请确认本地网易云 API 服务已启动（{ctx.base_url}）；"
                "设置页的「后端日志」里有完整输出。"
            ),
        )

    return session.as_dict(with_image=True)


@router.post("/login/cancel")
def login_cancel(ctx: WebContext = Depends(get_ctx)) -> dict[str, Any]:
    if ctx.login is None or ctx.login.finished:
        return {"ok": False, "message": "当前没有进行中的登录"}

    cancelled = ctx.jobs.cancel(ctx.login.job_id)
    return {
        "ok": cancelled,
        "message": "已取消登录" if cancelled else "登录已经结束了",
    }


@router.get("/list")
def list_accounts(ctx: WebContext = Depends(get_ctx)) -> dict[str, Any]:
    """所有登录过的账号。**只读状态库，不碰网络。**"""
    active = ctx.store.active_account()
    active_uid = active.uid if active else 0
    return {
        "active_uid": active_uid,
        "accounts": [
            {
                "uid": account.uid,
                "nickname": account.nickname,
                "vip": bool(account.vip_type),
                "active": account.uid == active_uid,
            }
            for account in ctx.store.list_accounts()
        ],
    }


@router.post("/use")
def use_account(
    payload: UseRequest, ctx: WebContext = Depends(get_ctx)
) -> dict[str, Any]:
    """切换当前账号。cookie 是分开存的，切换只是换个"当前"。"""
    account = ctx.store.get_account(payload.uid)
    if account is None:
        raise HTTPException(status_code=404, detail=f"没有 uid={payload.uid} 这个账号")

    ctx.store.set_active_uid(account.uid)
    return {
        "ok": True,
        "message": f"已切换到 {account.nickname}",
        "uid": account.uid,
        "nickname": account.nickname,
    }


@router.post("/remove")
def remove_account(
    payload: RemoveRequest, ctx: WebContext = Depends(get_ctx)
) -> dict[str, Any]:
    """移除一个账号的登录信息。

    如果删的是当前账号，干脆把"当前"也清空——留一个指向不存在账号的
    当前 uid，后面所有请求都会带着空 cookie 跑，症状是"莫名拿不到歌"。
    """
    account = ctx.store.get_account(payload.uid)
    if account is None:
        raise HTTPException(status_code=404, detail=f"没有 uid={payload.uid} 这个账号")

    active = ctx.store.active_account()
    was_active = active is not None and active.uid == payload.uid

    ctx.store.remove_account(payload.uid)
    if was_active:
        ctx.store.set_active_uid(None)

    return {
        "ok": True,
        "message": f"已移除 {account.nickname}",
        "was_active": was_active,
    }
