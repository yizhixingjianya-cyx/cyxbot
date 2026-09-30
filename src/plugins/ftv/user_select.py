"""ftv 用户标识回退：直接调用查不到用户时走搜索，由用户点按钮选择。

仅 goqbot 生效（按钮依赖 markdown+keyboard 富文本）：
- 搜索命中 1 条 → 直接用该条的用户名重跑原指令接口；
- 命中多条     → 发 markdown 列表 + 回调按钮（每页 4 条，[1][2][3] / [←][4][→]）；
- 命中 0 条     → 交给调用方按原逻辑提示。

回调按钮（action.type=1）需要 goqbot 打开 InteractionHandler：点击后除
notice/interaction 外还会推送一条文本为 button_data 的普通 message，
因此这里用 on_message 规则直接接管该文本。
"""
import secrets
import time
from typing import Any, Awaitable, Callable, Dict, List, Tuple

from nonebot import on_message
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.log import logger

from util import is_goqbot, markdown_message

from .api import furtv_api

__all__ = ["resolve_user_fallback"]

PAGE_SIZE = 4  # 每页 4 条：[1][2][3] / [←][4][→]
SESSION_TTL = 300  # 选择会话有效期（秒）
_PREFIX = "ftvsel"  # 按钮回调文本前缀：ftvsel:<token>:<动作>
_MAX_TITLE = 60

# token -> 会话
_SESSIONS: Dict[str, "_Session"] = {}

# 原指令的展示函数：show(cmd, bot, event, 用户标识) -> 是否成功展示（False=查不到）
Show = Callable[[Any, Bot, MessageEvent, str], Awaitable[bool]]

_NOT_FOUND_HINT = "没有该数据（{0}），请尝试输入全名或 ID"


class _Session:
    __slots__ = ("token", "scope", "user_id", "query", "items", "page", "show", "ts")

    def __init__(
        self,
        token: str,
        scope: str,
        user_id: str,
        query: str,
        items: List[dict],
        show: "Show",
    ) -> None:
        self.token = token
        self.scope = scope
        self.user_id = user_id
        self.query = query
        self.items = items
        self.page = 1
        self.show = show
        self.ts = time.time()


def _cap(text: Any, limit: int = _MAX_TITLE) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _purge() -> None:
    now = time.time()
    for token in [t for t, s in _SESSIONS.items() if now - s.ts > SESSION_TTL]:
        _SESSIONS.pop(token, None)


def _scope_of(event: MessageEvent) -> str:
    group_id = getattr(event, "group_id", None)
    return f"g{group_id}" if group_id is not None else f"u{event.user_id}"


async def search_user_items(query: str) -> List[dict]:
    """按关键词搜索用户，返回用户对象列表（兼容多种外层结构）。"""
    data = await furtv_api.search(query, type="all")

    items = None
    if isinstance(data, dict):
        if isinstance(data.get("users"), list):
            items = data["users"]
        elif isinstance(data.get("data"), list):
            items = data["data"]
        else:
            inner = data.get("data")
            if isinstance(inner, dict) and isinstance(inner.get("users"), list):
                items = inner["users"]
    return [it for it in (items or []) if isinstance(it, dict)]


def identifier_of(item: dict) -> str:
    """取可用于重跑接口的用户标识（优先 username，回落 id）。"""
    return str(item.get("username") or item.get("id") or "").strip()


def _title_of(item: dict) -> str:
    """条目只显示 名字 / 物种 / 地区。"""
    name = str(item.get("nickname") or item.get("username") or "未知")
    parts = [_cap(name, 24)]
    species = item.get("fursuit_species") or item.get("species")
    if species:
        parts.append(_cap(species, 16))
    location = item.get("location")
    if location:
        parts.append(_cap(location, 16))
    return " ｜ ".join(parts)


def _total_pages(items: List[dict]) -> int:
    return max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)


def _button(label: str, data: str, style: int) -> dict:
    return {
        "id": data,
        "render_data": {"label": label, "visited_label": label, "style": style},
        "action": {
            "type": 1,  # 回调按钮：点击上报 INTERACTION_CREATE，data 进 button_data
            "permission": {"type": 2},  # 所有人可点
            "data": data,
            "unsupport_tips": "当前QQ版本不支持按钮，请升级后重试",
        },
    }


def _build_keyboard(session: "_Session", count: int) -> dict:
    """第一行放第 1-3 条，第二行按「上一页 / 第 4 条 / 下一页」排。

    不满 6 个键（单页）时不显示换页按钮。
    """
    token = session.token
    total = _total_pages(session.items)
    page = session.page

    row1 = [
        _button(str(i), f"{_PREFIX}:{token}:{i}", 1)
        for i in range(1, min(count, 3) + 1)
    ]
    row2: List[dict] = []
    if total > 1:
        row2.append(_button("上一页", f"{_PREFIX}:{token}:p{page - 1}", 0))
    if count >= 4:
        row2.append(_button("4", f"{_PREFIX}:{token}:4", 1))
    if total > 1:
        row2.append(_button("下一页", f"{_PREFIX}:{token}:p{page + 1}", 0))

    rows = [{"buttons": row} for row in (row1, row2) if row]
    return {"content": {"rows": rows}}


def _render_select(session: "_Session") -> Tuple[str, dict]:
    """生成当前页的 markdown 列表与键盘。"""
    total = _total_pages(session.items)
    start = (session.page - 1) * PAGE_SIZE
    chunk = session.items[start : start + PAGE_SIZE]

    lines = [f"**「{_cap(session.query, 30)}」匹配到 {len(session.items)} 个用户，请选择：**", ""]
    for i, item in enumerate(chunk, 1):
        lines.append(f"{i}. {_title_of(item)}")
    lines.append("")
    lines.append(f"第 {session.page}/{total} 页")
    return "\n".join(lines), _build_keyboard(session, len(chunk))


async def resolve_user_fallback(
    cmd: Any, bot: Bot, event: MessageEvent, query: str, show: "Show"
) -> bool:
    """直接调用查不到用户后的回退入口。

    返回 False 表示未接管（非 goqbot 或搜索无结果），调用方按原逻辑提示。
    接管时内部已 finish，不会正常返回。
    """
    if not await is_goqbot(bot):
        return False
    try:
        items = await search_user_items(query)
    except Exception as e:
        logger.warning(f"ftv 用户搜索失败（{query}）：{e}")
        return False
    if not items:
        return False

    if len(items) == 1:
        identifier = identifier_of(items[0])
        if not identifier:
            return False
        return await show(cmd, bot, event, identifier)

    _purge()
    token = secrets.token_hex(4)
    session = _Session(
        token=token,
        scope=_scope_of(event),
        user_id=str(event.user_id),
        query=query,
        items=items,
        show=show,
    )
    _SESSIONS[token] = session
    markdown, keyboard = _render_select(session)
    await cmd.finish(markdown_message(markdown, keyboard))
    return True


async def _select_payload_rule(event: MessageEvent) -> bool:
    return event.get_plaintext().strip().startswith(f"{_PREFIX}:")


ftv_select = on_message(rule=_select_payload_rule, priority=1, block=True)


@ftv_select.handle()
async def _(bot: Bot, event: MessageEvent):
    parts = event.get_plaintext().strip().split(":")
    if len(parts) < 3:
        return await ftv_select.finish()

    token, action = parts[1], parts[2]
    session = _SESSIONS.get(token)
    if session is None or time.time() - session.ts > SESSION_TTL:
        _SESSIONS.pop(token, None)
        return await ftv_select.finish("选择已过期，请重新发起指令")
    # 同会话内任何人可点，不再校验发起人
    if session.scope != _scope_of(event):
        return await ftv_select.finish()

    session.ts = time.time()

    # 翻页
    if action.startswith("p"):
        try:
            page = int(action[1:])
        except ValueError:
            return await ftv_select.finish()
        session.page = max(1, min(page, _total_pages(session.items)))
        markdown, keyboard = _render_select(session)
        return await ftv_select.finish(markdown_message(markdown, keyboard))

    # 选择第 n 条
    if not action.isdigit():
        return await ftv_select.finish()
    index = (session.page - 1) * PAGE_SIZE + int(action) - 1
    if not 0 <= index < len(session.items):
        return await ftv_select.finish("该选项已失效，请重新发起指令")

    identifier = identifier_of(session.items[index])
    if not identifier:
        return await ftv_select.finish("该用户缺少可用的标识，请改用其他方式查询")
    if not await session.show(ftv_select, bot, event, identifier):
        return await ftv_select.finish(_NOT_FOUND_HINT.format(identifier))
