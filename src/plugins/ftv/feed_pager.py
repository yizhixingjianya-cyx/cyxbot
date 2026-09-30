"""动态流发送：每页 6 条，每条一个 text 代码块 + 数字/翻页按钮。

- 仅 goqbot 分页（按钮依赖 markdown+keyboard 富文本），其他平台由调用方按原文本发送；
- 每页 6 条，markdown 内容为标题 + 6 个独立 ``text`` 代码块（序号按页内 1-6）；
- 按钮：``[1][2][3]`` / ``[上一页][4][5][6][下一页]``，不满 6 条时不显示多余数字键，
  单页时不显示翻页键；
- 点数字 → 该条动态的完整详情（``动态详情 <ID>``）；点翻页 → 重绘该页。

回调按钮（action.type=1）需要 goqbot 打开 InteractionHandler：点击后会推送一条
文本为 button_data 的普通 message，因此这里用 on_message 规则直接接管该文本。
"""
import secrets
import time
from typing import Any, Dict, List, Optional

from nonebot import on_message
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.exception import FinishedException
from nonebot.log import logger

from util import finish_text, is_goqbot, markdown_message

from .user_select import _button, _scope_of

__all__ = ["publish_feed", "ftv_feed_page"]

PAGE_SIZE = 6  # 与 today_commands.FEED_PAGE_SIZE 保持一致
SESSION_TTL = 300  # 分页会话有效期（秒）
_PREFIX = "ftvfeed"  # 按钮回调文本前缀：ftvfeed:<token>:<动作>，动作为 i<全局序号> 或 p<页码>

# token -> 会话
_SESSIONS: Dict[str, "_FeedSession"] = {}


class _FeedSession:
    __slots__ = ("token", "scope", "title", "items", "page", "ts")

    def __init__(self, token: str, scope: str, title: str, items: List[dict]) -> None:
        self.token = token
        self.scope = scope
        self.title = title
        self.items = items
        self.page = 1
        self.ts = time.time()


def _purge() -> None:
    now = time.time()
    for token in [t for t, s in _SESSIONS.items() if now - s.ts > SESSION_TTL]:
        _SESSIONS.pop(token, None)


def _total_pages(items: List[dict]) -> int:
    return max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)


def _build_keyboard(session: "_FeedSession", count: int) -> Optional[dict]:
    """第一行 1-3，第二行「上一页 / 4-6 / 下一页」，单页时不显示翻页键。

    数字键的 data 编码全局序号（``i<index>``），翻页后点上一页残留的数字键仍指向原条目。
    """
    token = session.token
    total = _total_pages(session.items)
    page = session.page
    start = (page - 1) * PAGE_SIZE

    def num_button(label: int) -> dict:
        return _button(str(label), f"{_PREFIX}:{token}:i{start + label - 1}", 1)

    row1 = [num_button(i) for i in range(1, min(count, 3) + 1)]
    row2: List[dict] = []
    if total > 1:
        row2.append(_button("上一页", f"{_PREFIX}:{token}:p{page - 1}", 0))
    row2 += [num_button(i) for i in range(4, count + 1)]
    if total > 1:
        row2.append(_button("下一页", f"{_PREFIX}:{token}:p{page + 1}", 0))

    rows = [{"buttons": row} for row in (row1, row2) if row]
    return {"content": {"rows": rows}} if rows else None


async def _send_page(cmd: Any, bot: Bot, event: MessageEvent, session: "_FeedSession") -> None:
    """发送当前页（标题 + 若干 text 代码块）+ 数字/翻页按钮（结束本次处理）。"""
    from .today_commands import format_feed_blocks  # 延迟导入，避免循环依赖

    total = _total_pages(session.items)
    start = (session.page - 1) * PAGE_SIZE
    count = len(session.items[start : start + PAGE_SIZE])

    blocks = format_feed_blocks(session.items, session.page)
    caption = (
        f"**{session.title}**\n"
        f"第 {session.page}/{total} 页 · 共 {len(session.items)} 条\n"
        f"点数字查看动态详情，或使用翻页按钮"
    )
    content = f"{caption}\n\n{blocks}" if blocks else caption

    keyboard = _build_keyboard(session, count)
    if keyboard is None:
        return await finish_text(cmd, bot, content.replace("**", ""))
    await cmd.finish(markdown_message(content, keyboard))


async def publish_feed(
    cmd: Any, bot: Bot, event: MessageEvent, items: List[dict], title: str = "动态流"
) -> bool:
    """多结果统一出口：goqbot 下每页 6 条分页发送。

    返回 False 表示未接管（非 goqbot 或结果不足 2 条），调用方按原文本逻辑处理；
    接管时已 finish，不会正常返回。
    """
    if not items or len(items) < 2 or not await is_goqbot(bot):
        return False

    _purge()
    token = secrets.token_hex(4)
    session = _FeedSession(token=token, scope=_scope_of(event), title=title, items=items)
    _SESSIONS[token] = session
    await _send_page(cmd, bot, event, session)
    return True


def _payload_rule(event: MessageEvent) -> bool:
    return event.get_plaintext().strip().startswith(f"{_PREFIX}:")


ftv_feed_page = on_message(rule=_payload_rule, priority=1, block=True)


@ftv_feed_page.handle()
async def _(bot: Bot, event: MessageEvent):
    parts = event.get_plaintext().strip().split(":")
    if len(parts) < 3:
        return await ftv_feed_page.finish()

    token, action = parts[1], parts[2]
    session = _SESSIONS.get(token)
    if session is None or time.time() - session.ts > SESSION_TTL:
        _SESSIONS.pop(token, None)
        return await ftv_feed_page.finish("列表已过期，请重新发起指令")
    # 同会话内任何人可点，不再校验发起人
    if session.scope != _scope_of(event):
        return await ftv_feed_page.finish()

    session.ts = time.time()

    # 翻页
    if action.startswith("p"):
        try:
            page = int(action[1:])
        except ValueError:
            return await ftv_feed_page.finish()
        session.page = max(1, min(page, _total_pages(session.items)))
        return await _send_page(ftv_feed_page, bot, event, session)

    # 查看第 n 条：i<全局序号> 优先，纯数字按当前页换算（兼容旧按钮）
    if action.startswith("i") and action[1:].isdigit():
        index = int(action[1:])
    elif action.isdigit():
        index = (session.page - 1) * PAGE_SIZE + int(action) - 1
    else:
        return await ftv_feed_page.finish()
    if not 0 <= index < len(session.items):
        return await ftv_feed_page.finish("该选项已失效，请重新发起指令")

    item = session.items[index]
    today_id = ""
    if isinstance(item, dict):
        from .today_commands import _card_id  # 延迟导入，避免循环依赖

        today_id = str(_card_id(item) or "").strip()
    if not today_id:
        return await ftv_feed_page.finish("该动态没有可用的详情 ID")

    try:
        from .today_commands import show_today_detail  # 延迟导入，避免循环依赖

        if await show_today_detail(ftv_feed_page, bot, event, today_id):
            return
    except FinishedException:
        raise
    except Exception as e:
        logger.warning(f"获取动态详情失败（{today_id}）：{e}")
    return await ftv_feed_page.finish(f"没有找到该动态（{today_id}）")
