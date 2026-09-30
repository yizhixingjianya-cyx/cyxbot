"""物种/地区统计图分页：一次只发一页，附「上一页 / 剩下的全发 / 下一页」按钮。

- 仅 goqbot 分页（按钮依赖 markdown+keyboard 富文本），其他平台或单页时由调用方整发；
- 指令带 all 参数时由调用方直接全发，不进入本模块；
- 点「上一页 / 下一页」重绘对应页；点「剩下的全发」把当前页之后的所有页一次性发出。

回调按钮（action.type=1）需要 goqbot 打开 InteractionHandler：点击后会推送一条
文本为 button_data 的普通 message，因此这里用 on_message 规则直接接管该文本。
"""
import secrets
import time
from io import BytesIO
from typing import Any, Dict, List

from nonebot import on_message
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment

from util import finish_text, is_goqbot, markdown_message

from .image_generator import STATS_PER_PAGE, generate_species_stats_image
from .image_host import host_image, image_markdown, md_display_size
from .user_select import _button, _scope_of

__all__ = ["publish_stats", "ftv_stats_page"]

SESSION_TTL = 300  # 分页会话有效期（秒）
_PREFIX = "ftvstats"  # 按钮回调文本前缀：ftvstats:<token>:<动作>，动作为 p<页码> 或 rest

# 物种列表画布是 1000px 宽、条数一多就上千 px 高，按官方建议回填 720x1080 的框
# 之后仍然偏「细长」。这里给统计图单独留一个更矮的显示高度上限（等比缩放，不拉伸）。
# 它只影响 QQ 里的显示大小，图床上放的仍是原图；嫌矮/嫌高就改这一个数。
STATS_MD_MAX_HEIGHT = 900

# token -> 会话
_SESSIONS: Dict[str, "_StatsSession"] = {}


class _StatsSession:
    __slots__ = ("token", "scope", "user_id", "title", "items", "total", "page", "ts")

    def __init__(
        self,
        token: str,
        scope: str,
        user_id: str,
        title: str,
        items: List[dict],
        total: int,
    ) -> None:
        self.token = token
        self.scope = scope
        self.user_id = user_id
        self.title = title
        self.items = items
        self.total = total
        self.page = 1
        self.ts = time.time()


def _purge() -> None:
    now = time.time()
    for token in [t for t, s in _SESSIONS.items() if now - s.ts > SESSION_TTL]:
        _SESSIONS.pop(token, None)


def _total_pages(items: List[dict]) -> int:
    return max(1, (len(items) + STATS_PER_PAGE - 1) // STATS_PER_PAGE)


def _build_keyboard(session: "_StatsSession") -> dict:
    token = session.token
    page = session.page
    row = [
        _button("上一页", f"{_PREFIX}:{token}:p{page - 1}", 0),
        _button("剩下的全发", f"{_PREFIX}:{token}:rest", 1),
        _button("下一页", f"{_PREFIX}:{token}:p{page + 1}", 0),
    ]
    return {"content": {"rows": [{"buttons": row}]}}


def _image_message(img) -> Message:
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Message([MessageSegment.image(buf)])


def _multi_image_message(imgs: List) -> Message:
    """一条普通消息里挂多张图（多个 image 段）"""
    segs = []
    for img in imgs:
        buf = BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        segs.append(MessageSegment.image(buf))
    return Message(segs)


async def _send_pages_together(bot: Bot, event: MessageEvent, imgs: List) -> None:
    """把多页统计图塞进**同一个消息框**

    goqbot + 有公网图床时用 markdown 连写多个 ``![]()``，一条消息贴完；
    否则回落「一条普通消息挂多个图片段」。两条路都只发一条消息，
    不会再一页一条地刷屏。
    """
    if await is_goqbot(bot):
        urls = [host_image(img) for img in imgs]
        if all(urls):
            blocks = [
                image_markdown(
                    url,
                    "统计图",
                    *md_display_size(
                        img.width, img.height, max_height=STATS_MD_MAX_HEIGHT
                    ),
                )
                for url, img in zip(urls, imgs)
            ]
            await bot.send(event, markdown_message("\n\n".join(blocks)))
            return
    await bot.send(event, _multi_image_message(imgs))


async def _render_page(session: "_StatsSession", page: int):
    return await generate_species_stats_image(
        session.items, session.total, title=session.title, page=page
    )


async def _send_page(cmd: Any, bot: Bot, event: MessageEvent, session: "_StatsSession") -> None:
    """发送当前页统计图 + 带按钮的说明（结束本次处理）。

    「图 + 文字 + 按钮」落在同一条消息里：图片以公网 URL 嵌进 markdown，
    和 keyboard 同段发出；没有可用公网图床时回退为「图片一条 + 文字按钮一条」。
    """
    total = _total_pages(session.items)
    imgs = await _render_page(session, session.page)
    if not imgs:
        return await finish_text(cmd, bot, f"{session.title}：图片生成失败")

    page_img = imgs[0]
    caption = (
        f"**{session.title}**\n"
        f"第 {session.page}/{total} 页 · 共 {len(session.items)} 条\n"
        f"点「剩下的全发」发完后续页；重新发送指令时带 all 参数可一次发完整列表"
    )
    keyboard = _build_keyboard(session)

    url = host_image(page_img)
    if url:
        # 图床上是原图；只把回填给 QQ 的显示尺寸压小（统计图的高上限更矮一点）
        disp = md_display_size(
            page_img.width, page_img.height, max_height=STATS_MD_MAX_HEIGHT
        )
        content = f"{image_markdown(url, '统计图', *disp)}\n\n{caption}"
        return await cmd.finish(markdown_message(content, keyboard))

    await bot.send(event, _image_message(page_img))
    await cmd.finish(markdown_message(caption, keyboard))


async def publish_stats(
    cmd: Any,
    bot: Bot,
    event: MessageEvent,
    items: List[dict],
    total: int = 0,
    title: str = "CYX-BOT 物种统计",
) -> bool:
    """统计结果统一出口：goqbot 且超过一页时一次只发一页。

    返回 False 表示未接管（非 goqbot 或不超过一页），调用方按原整发逻辑处理；
    接管时已 finish，不会正常返回。
    """
    if len(items) <= STATS_PER_PAGE or not await is_goqbot(bot):
        return False

    _purge()
    token = secrets.token_hex(4)
    session = _StatsSession(
        token=token,
        scope=_scope_of(event),
        user_id=str(event.user_id),
        title=title,
        items=items,
        total=total,
    )
    _SESSIONS[token] = session
    await _send_page(cmd, bot, event, session)
    return True


def _stats_payload_rule(event: MessageEvent) -> bool:
    return event.get_plaintext().strip().startswith(f"{_PREFIX}:")


ftv_stats_page = on_message(rule=_stats_payload_rule, priority=1, block=True)


@ftv_stats_page.handle()
async def _(bot: Bot, event: MessageEvent):
    parts = event.get_plaintext().strip().split(":")
    if len(parts) < 3:
        return await ftv_stats_page.finish()

    token, action = parts[1], parts[2]
    session = _SESSIONS.get(token)
    if session is None or time.time() - session.ts > SESSION_TTL:
        _SESSIONS.pop(token, None)
        return await ftv_stats_page.finish("列表已过期，请重新发起指令")
    # 同会话内任何人可点，不再校验发起人
    if session.scope != _scope_of(event):
        return await ftv_stats_page.finish()

    session.ts = time.time()
    total = _total_pages(session.items)

    # 剩下的全发：当前页之后的所有页塞进**同一个消息框**一次发完
    if action == "rest":
        remaining = total - session.page
        if remaining <= 0:
            return await ftv_stats_page.finish("已经是最后一页了")
        pages = []
        for page in range(session.page + 1, total + 1):
            imgs = await _render_page(session, page)
            if imgs:
                pages.append(imgs[0])
        session.page = total  # 已全部发出，页码推进到末页，避免重复点「剩下的全发」再发一遍
        if not pages:
            return await ftv_stats_page.finish("后续页生成失败，请重试")
        await _send_pages_together(bot, event, pages)
        return await ftv_stats_page.finish(f"已发送剩余 {len(pages)} 页")

    # 翻页
    if action.startswith("p"):
        try:
            page = int(action[1:])
        except ValueError:
            return await ftv_stats_page.finish()
        session.page = max(1, min(page, total))
        return await _send_page(ftv_stats_page, bot, event, session)

    return await ftv_stats_page.finish()
