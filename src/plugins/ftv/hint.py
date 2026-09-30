"""「查询中，请稍后」提示：只在真正发起 API 请求时发送一次。

指令匹配后由 preprocessor 调用 :func:`arm_hint` 登记本次会话；等到确实要访问
API（缓存未命中、即将发起 HTTP 请求）时才由 :func:`send_hint_once` 发出提示，
因此菜单这类不访问接口的指令、以及参数缺失提前返回的情况都不会发提示。
"""
from contextvars import ContextVar
from typing import Any, Optional

_HINT_CTX: ContextVar[Optional[dict]] = ContextVar("ftv_hint", default=None)


def arm_hint(bot: Any, event: Any) -> None:
    """登记本次会话的 bot/event（此时不发送提示）"""
    _HINT_CTX.set({"bot": bot, "event": event, "sent": False})


async def send_hint_once() -> None:
    """真正要访问 API 时调用：本次会话首次调用才发出提示"""
    ctx = _HINT_CTX.get()
    if not ctx or ctx.get("sent"):
        return
    ctx["sent"] = True
    try:
        from util import send_text

        await send_text(ctx["bot"], ctx["event"], "查询中，请稍后…")
    except Exception as e:
        from nonebot.log import logger

        logger.warning(f"发送「查询中」提示失败：{e}")
