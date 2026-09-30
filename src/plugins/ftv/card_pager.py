"""档案卡片发送：多结果每页 6 张拼图 + 数字/翻页按钮，单条也给「查看详细档案」按钮。

- 仅 goqbot 分页（按钮依赖 markdown+keyboard 富文本），其他平台由调用方按整图发送；
- 每页一张拼图（3 列 2 行），下方按钮：``[1][2][3]`` / ``[上一页][4][5][6][下一页]``，
  不满 6 个时不显示多余数字键，单页时不显示翻页键；
- 只有 1 条时直接发该条卡片图，按钮为单个 ``[查看详细档案]``；
- 点数字 → 档案列表（detail=True）直接进入该条的完整档案详情（兽档案大图）；
  崽崽等角色列表（detail=False）仍发送单张卡片图，单条时不加按钮；点翻页 → 重绘该页。

回调按钮（action.type=1）需要 goqbot 打开 InteractionHandler：点击后会推送一条
文本为 button_data 的普通 message，因此这里用 on_message 规则直接接管该文本。
"""
import secrets
import time
from io import BytesIO
from typing import Any, Dict, List, Optional

from nonebot import on_message
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment
from nonebot.exception import FinishedException
from nonebot.log import logger

from util import finish_text, is_goqbot, markdown_message

from .image_generator import generate_user_info_image, generate_users_collage
from .image_host import host_image, image_markdown, md_display_size
from .user_select import _button, _scope_of

__all__ = ["publish_cards", "ftv_card_page"]

PAGE_SIZE = 6  # 每页 6 张卡片：3 列 x 2 行
SESSION_TTL = 300  # 分页会话有效期（秒）
_PREFIX = "ftvpage"  # 按钮回调文本前缀：ftvpage:<token>:<动作>，动作为 i<全局序号> 或 p<页码>

# token -> 会话
_SESSIONS: Dict[str, "_PageSession"] = {}


class _PageSession:
    __slots__ = ("token", "scope", "user_id", "title", "items", "page", "ts", "detail")

    def __init__(
        self,
        token: str,
        scope: str,
        user_id: str,
        title: str,
        items: List[dict],
        detail: bool = True,
    ) -> None:
        self.token = token
        self.scope = scope
        self.user_id = user_id
        self.title = title
        self.items = items
        self.page = 1
        self.ts = time.time()
        self.detail = detail  # True=档案列表：点数字进入档案详情；False=崽崽等：点数字发单张卡片图


def _purge() -> None:
    now = time.time()
    for token in [t for t, s in _SESSIONS.items() if now - s.ts > SESSION_TTL]:
        _SESSIONS.pop(token, None)


def _total_pages(items: List[dict]) -> int:
    return max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)


def _build_keyboard(session: "_PageSession", count: int) -> Optional[dict]:
    """第一行 1-3，第二行「上一页 / 4-6 / 下一页」，单页时不显示翻页键。

    数字键的 data 直接编码全局序号（``i<index>``），因此翻页后点击上一页残留的
    数字键仍指向原来那张卡片，不会错位。

    只有 1 条时给单个「查看详细档案」按钮；返回 None 表示不带按钮（单个崽崽卡片）。
    """
    token = session.token
    total = _total_pages(session.items)
    page = session.page
    start = (page - 1) * PAGE_SIZE

    if len(session.items) == 1:
        if not session.detail:
            return None
        return {
            "content": {
                "rows": [
                    {"buttons": [_button("查看详细档案", f"{_PREFIX}:{token}:i0", 1)]}
                ]
            }
        }

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
    return {"content": {"rows": rows}}


def _image_message(img) -> Message:
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Message([MessageSegment.image(buf)])


def _text_image_message(text: str, img) -> Message:
    """纯文本 + 图片：goqbot「单图快路径」把两者并成同一个气泡（文字在上、图在下）。

    注意这条路径**不能带 markdown 段**，否则会退化成两条消息，因此 text 用纯文本。
    """
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    segs = []
    if text:
        segs.append(MessageSegment.text(text))
    segs.append(MessageSegment.image(buf))
    return Message(segs)


async def _send_page(cmd: Any, bot: Bot, event: MessageEvent, session: "_PageSession") -> None:
    """发送当前页图片 + 带按钮的说明（结束本次处理）。

    单条时不拼图，直接发该条的卡片图，并给「查看详细档案」按钮。

    「图 + 文字 + 按钮」尽量落在同一条消息里：
    - 需要按钮：把图片以公网 URL 嵌进 markdown，和 keyboard 同段发出（一条）；
    - 不需要按钮（单个崽崽卡片）：纯文本 + 图片走「单图快路径」，一个气泡；
    - 没有可用公网图床时回退为「图片一条 + 文字按钮一条」，至少不丢按钮。
    """
    total = _total_pages(session.items)
    start = (session.page - 1) * PAGE_SIZE
    chunk = session.items[start : start + PAGE_SIZE]
    single = len(session.items) == 1

    if single:
        try:
            img = await generate_user_info_image(chunk[0])
        except Exception as e:
            logger.error(f"生成单条档案图失败：{e}", exc_info=True)
            img = None
    else:
        img = await generate_users_collage(chunk, columns=3)
    if img is None:
        return await finish_text(cmd, bot, f"{session.title}：图片生成失败")

    if single:
        caption = f"**{session.title}**\n点下方按钮查看完整档案详情"
    else:
        action_hint = "点数字查看档案详情" if session.detail else "点数字查看崽崽"
        caption = (
            f"**{session.title}**\n"
            f"第 {session.page}/{total} 页 · 共 {len(session.items)} 条\n"
            f"{action_hint}，或使用翻页按钮"
        )
    plain = caption.replace("**", "")
    keyboard = _build_keyboard(session, len(chunk))

    # 不需要按钮：单图快路径，文字 + 图片合成一个气泡（不依赖公网图床）
    if keyboard is None:
        return await cmd.finish(_text_image_message(plain, img))

    # 需要按钮：图片必须走公网 URL 才能和按钮同一条消息
    url = host_image(img)
    if url:
        # 图床上是原图；只把回填给 QQ 的显示尺寸按官方建议压小（不重编码）
        content = (
            f"{image_markdown(url, '档案预览', *md_display_size(img.width, img.height))}"
            f"\n\n{caption}"
        )
        return await cmd.finish(markdown_message(content, keyboard))

    await bot.send(event, _image_message(img))
    return await cmd.finish(markdown_message(caption, keyboard))


async def publish_cards(
    cmd: Any, bot: Bot, event: MessageEvent, items: List[dict], title: str = "", detail: bool = True
) -> bool:
    """多结果统一出口：goqbot 下按每页 6 个分页发送；单条也给「查看详细档案」按钮。

    detail=True（档案列表）时点数字/按钮直接进入档案详情；
    崽崽等角色列表传 detail=False，点数字仍发送单张卡片图（单条时不加按钮）。
    返回 False 表示未接管（非 goqbot、无结果，或单条崽崽卡片），
    调用方按原整图逻辑处理；接管时已 finish，不会正常返回。
    """
    if not items or not await is_goqbot(bot):
        return False
    if len(items) == 1 and not detail:
        return False

    _purge()
    token = secrets.token_hex(4)
    session = _PageSession(
        token=token,
        scope=_scope_of(event),
        user_id=str(event.user_id),
        title=title or "档案列表",
        items=items,
        detail=detail,
    )
    _SESSIONS[token] = session
    await _send_page(cmd, bot, event, session)
    return True


def _page_payload_rule(event: MessageEvent) -> bool:
    return event.get_plaintext().strip().startswith(f"{_PREFIX}:")


ftv_card_page = on_message(rule=_page_payload_rule, priority=1, block=True)


@ftv_card_page.handle()
async def _(bot: Bot, event: MessageEvent):
    parts = event.get_plaintext().strip().split(":")
    if len(parts) < 3:
        return await ftv_card_page.finish()

    token, action = parts[1], parts[2]
    session = _SESSIONS.get(token)
    if session is None or time.time() - session.ts > SESSION_TTL:
        _SESSIONS.pop(token, None)
        return await ftv_card_page.finish("列表已过期，请重新发起指令")
    # 同会话内任何人可点，不再校验发起人
    if session.scope != _scope_of(event):
        return await ftv_card_page.finish()

    session.ts = time.time()

    # 翻页
    if action.startswith("p"):
        try:
            page = int(action[1:])
        except ValueError:
            return await ftv_card_page.finish()
        session.page = max(1, min(page, _total_pages(session.items)))
        return await _send_page(ftv_card_page, bot, event, session)

    # 查看第 n 条：i<全局序号> 优先，纯数字按当前页换算（兼容旧按钮）
    if action.startswith("i") and action[1:].isdigit():
        index = int(action[1:])
    elif action.isdigit():
        index = (session.page - 1) * PAGE_SIZE + int(action) - 1
    else:
        return await ftv_card_page.finish()
    if not 0 <= index < len(session.items):
        return await ftv_card_page.finish("该选项已失效，请重新发起指令")
    item = session.items[index]

    # 档案列表：点数字直接进入完整档案详情（兽档案）；崽崽等角色列表不触发
    if session.detail:
        username = str(item.get("username") or item.get("id") or "").strip()
        if username:
            try:
                from .commands import show_user_profile  # 延迟导入，避免循环依赖

                if await show_user_profile(ftv_card_page, bot, event, username):
                    return
            except FinishedException:
                raise
            except FileNotFoundError:
                pass  # 查不到档案，回退卡片图
            except Exception as e:
                logger.warning(f"获取档案详情失败（{username}），回退卡片图：{e}")
        else:
            logger.warning("档案条目缺少可用标识，回退卡片图")

    try:
        img = await generate_user_info_image(item)
    except Exception as e:
        logger.error(f"生成单条档案图失败：{e}", exc_info=True)
        img = None
    if img is None:
        return await ftv_card_page.finish("档案图生成失败，请稍后重试")
    return await ftv_card_page.finish(_image_message(img))
