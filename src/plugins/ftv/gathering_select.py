"""本月聚会多结果选择：多个聚会时只发简要列表，点数字按钮查看完整详情。

仅 goqbot 生效（按钮依赖 markdown+keyboard 富文本）；单聚会或非 goqbot 时
由调用方按原文本列表发送。

列表条目只显示 名字 / 地点 / 日期（不含简介等详情），完整详情点按钮后展示。
同一会话（群）内任何人都可点按钮查看，无需自己重新发起指令。

回调按钮（action.type=1）需要 goqbot 打开 InteractionHandler：点击后会推送一条
文本为 button_data 的普通 message，因此这里用 on_message 规则直接接管该文本。
"""
import secrets
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from nonebot import on_message
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import MessageEvent

from util import finish_text, is_goqbot, markdown_message

from .api import furtv_api
from .user_select import _button, _scope_of

__all__ = ["publish_gatherings", "format_gathering_detail"]

MAX_BUTTONS = 10  # 最多展示前 10 个聚会（与原文本列表一致）
SESSION_TTL = 300  # 选择会话有效期（秒）
_PREFIX = "ftvgath"  # 按钮回调文本前缀：ftvgath:<token>:<序号>
_PER_ROW = 5  # 每行按钮数

# token -> 会话
_SESSIONS: Dict[str, "_Session"] = {}


class _Session:
    __slots__ = ("token", "scope", "items", "month", "ts")

    def __init__(self, token: str, scope: str, items: List[dict], month: int) -> None:
        self.token = token
        self.scope = scope
        self.items = items
        self.month = month
        self.ts = time.time()


def _cap(text: Any, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _purge() -> None:
    now = time.time()
    for token in [t for t, s in _SESSIONS.items() if now - s.ts > SESSION_TTL]:
        _SESSIONS.pop(token, None)


# 展示的字段：(中文标签, 候选键名)，取第一个存在的键；键都不存在时不显示该行
# 只输出这些关键字段，接口返回的其余内部字段（抓取来源、同步状态、图片地址等）一律不展示
_DETAIL_FIELDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("ID", ("id", "gathering_id", "gatheringId", "_id")),
    ("名称", ("title", "name")),
    ("日期", ("event_date", "date", "start_date", "day")),
    ("结束", ("end_date", "end_time")),
    ("地点", ("locationPublic", "location_public", "location", "location_city", "address", "venue")),
    ("类型", ("type_display", "type")),
    ("状态", ("status", "state")),
    ("参与人数", ("current_participants", "participants")),
    ("人数上限", ("max_participants", "capacity", "limit")),
    ("描述", ("description", "intro", "content")),
    ("创建时间", ("created_at",)),
    ("更新时间", ("updated_at",)),
)


def _field_value(value) -> str:
    """字段值转成可读单行文本"""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (dict, list, tuple)):
        return f"（{len(value)} 项）"
    return str(value)


_MAX_VALUE = 120  # 单个字段值最大字符数，超出截断（描述例外）


def _clip(text: str, limit: int = _MAX_VALUE) -> str:
    """过长的字段值截断显示（如内嵌 JSON 串）"""
    if limit <= 0 or len(text) <= limit:
        return text
    return f"{text[:limit]}…（共 {len(text)} 字）"


def _format_event_date(value) -> str:
    """日期去掉 T 后面的时间部分"""
    text = _field_value(value)
    return text.split("T")[0] if "T" in text else text


def _parse_iso(value) -> Optional[datetime]:
    """解析 ISO 8601 时间，失败返回 None"""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_datetime(value) -> str:
    """ISO 时间 -> 本地可读时间（2026-08-01 08:00）；解析失败原样返回"""
    dt = _parse_iso(value)
    if dt is None:
        return _field_value(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%d %H:%M")


def format_gathering_detail(gathering: dict) -> Tuple[str, str]:
    """把单个聚会 dict 格式化为 (纯文本, markdown)，供「聚会详情」与按钮选择共用。

    markdown 用 `text` 代码块承载关键字段；时间字段转成本地可读格式；
    接口返回的其余内部字段不展示。
    """
    lines: list = []

    for label, keys in _DETAIL_FIELDS:
        key = next((k for k in keys if gathering.get(k) not in (None, "", [], {})), None)
        if key is None:
            continue
        value = gathering[key]
        if label in ("日期", "结束"):
            value = _format_event_date(value)
        elif label in ("创建时间", "更新时间"):
            value = _format_datetime(value)
        text = _field_value(value)
        lines.append(f"{label}：{text if label == '描述' else _clip(text)}")

    body = "\n".join(lines) if lines else "（接口未返回任何字段）"
    plain = f"聚会详情\n\n{body}"
    md = f"**聚会详情**\n\n```text\n{body}\n```"
    return plain, md


def _render_select(session: "_Session") -> str:
    """简要列表：只显示 名字 / 地点 / 日期。"""
    lines = [f"**本月共 {len(session.items)} 个聚会，点数字查看完整详情：**", ""]
    for i, g in enumerate(session.items, 1):
        title = _cap(g.get("title", "未知"), 20)
        location = _cap(g.get("locationPublic", g.get("location", "未知地点")), 20)
        day = g.get("day", "?")
        lines.append(f"{i}. {title} ｜ {location} ｜ {session.month}月{day}日")
    return "\n".join(lines)


def _build_keyboard(token: str, count: int) -> dict:
    rows = []
    for start in range(0, count, _PER_ROW):
        rows.append(
            {
                "buttons": [
                    _button(str(i), f"{_PREFIX}:{token}:{i}", 1)
                    for i in range(start + 1, min(start + _PER_ROW, count) + 1)
                ]
            }
        )
    return {"content": {"rows": rows}}


async def publish_gatherings(
    cmd: Any, bot: Bot, event: MessageEvent, gatherings: List[dict], month: int
) -> bool:
    """本月聚会多结果统一出口：goqbot 下只发简要列表 + 数字按钮。

    返回 False 表示未接管（非 goqbot 或聚会不足 2 个），调用方按原文本列表发送；
    接管时已 finish，不会正常返回。
    """
    if len(gatherings) < 2 or not await is_goqbot(bot):
        return False

    _purge()
    token = secrets.token_hex(4)
    items = gatherings[:MAX_BUTTONS]
    session = _Session(token=token, scope=_scope_of(event), items=items, month=month)
    _SESSIONS[token] = session
    await cmd.finish(markdown_message(_render_select(session), _build_keyboard(token, len(items))))
    return True


def _gath_payload_rule(event: MessageEvent) -> bool:
    return event.get_plaintext().strip().startswith(f"{_PREFIX}:")


ftv_gath_pick = on_message(rule=_gath_payload_rule, priority=1, block=True)


@ftv_gath_pick.handle()
async def _(bot: Bot, event: MessageEvent):
    parts = event.get_plaintext().strip().split(":")
    if len(parts) < 3 or not parts[2].isdigit():
        return await ftv_gath_pick.finish()

    token, action = parts[1], parts[2]
    session = _SESSIONS.get(token)
    if session is None or time.time() - session.ts > SESSION_TTL:
        _SESSIONS.pop(token, None)
        return await ftv_gath_pick.finish("列表已过期，请重新发送「本月聚会」")
    # 同会话内任何人可点，不再校验发起人
    if session.scope != _scope_of(event):
        return await ftv_gath_pick.finish()

    session.ts = time.time()
    index = int(action) - 1
    if not 0 <= index < len(session.items):
        return await ftv_gath_pick.finish("该选项已失效，请重新发送「本月聚会」")

    gathering = session.items[index]
    gid = str(
        gathering.get("id")
        or gathering.get("gathering_id")
        or gathering.get("gatheringId")
        or gathering.get("_id")
        or ""
    ).strip()
    if not gid:
        return await ftv_gath_pick.finish("该聚会缺少 ID，无法查看详情")

    try:
        data = await furtv_api.get_gathering_detail(gid)
    except FileNotFoundError:
        return await ftv_gath_pick.finish(f"没有找到该聚会（{gid}）")
    except Exception as e:
        return await ftv_gath_pick.finish(f"获取失败：{str(e)}")

    # 支持多种返回结构
    detail = None
    if isinstance(data, dict):
        if "gathering" in data:
            detail = data.get("gathering")
        elif "data" in data:
            detail = data.get("data")
    if not detail:
        return await ftv_gath_pick.finish(f"未找到聚会 ID '{gid}'")

    result, md = format_gathering_detail(detail)
    return await finish_text(ftv_gath_pick, bot, result, md)
