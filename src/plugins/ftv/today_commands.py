"""Today 公开内容(兽频道动态) 与 搜索建议/点赞状态/平台状态 指令。

对应 developer.vds.pub 文档:
- Today 公开内容: furtv.today.explore/feed/detail/topics/gatherings/users.timeline/users.current
- 搜索建议: furtv.discovery.search.suggestions
- 用户点赞状态: furtv.fursuit.likestatus
- 健康检查: furtv.health
"""
from functools import partial
from typing import Any

from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import MessageEvent, Message
from nonebot.params import CommandArg
from nonebot.exception import FinishedException

from util import cmdbuild, finish_text, is_goqbot
from .api import furtv_api
from .feed_pager import publish_feed
from .user_select import resolve_user_fallback

# 列表类响应里的常见卡片字段
_CARD_LIST_KEYS = ("explore_items", "feed_items", "todays")
# 响应里可忽略的包裹字段
_SKIP_KEYS = {"requestId", "success", "ok", "code", "message", "traceId", "error", "data"}

_MAX_LIST = 10
FEED_PAGE_SIZE = 6  # 动态流每页条数（与 feed_pager 的分页按钮一致）


def _cap(s, n: int = 200) -> str:
    """截断过长的字符串"""
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _inner(data) -> dict:
    """兼容 `{...}` 与 `{data: {...}}` 两种返回外层"""
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return data["data"]
    return data if isinstance(data, dict) else {}


def _pick_card_list(data) -> list:
    """从响应里挑出卡片列表（探索/信息流/时间线等）。

    同时兼容原始响应（``{"success":true,"data":{...}}``）与已拆包的 inner 字典。
    """
    if isinstance(data, list):
        return data
    inner = _inner(data)
    for key in _CARD_LIST_KEYS:
        value = inner.get(key)
        if isinstance(value, list) and value:
            return value
    # 兜底: data 字段本身是列表
    if isinstance(inner.get("data"), list):
        return inner["data"]
    return []


def _meta_name(inner: dict, key: str) -> str:
    """从 user/topic/gathering 对象里取展示名"""
    obj = inner.get(key)
    if not isinstance(obj, dict):
        return ""
    return str(
        obj.get("nickname") or obj.get("name") or obj.get("username")
        or obj.get("title") or obj.get("slug") or ""
    )


def _card_merge(item: dict) -> dict:
    """把嵌套在 today / page 里的字段并入顶层（setdefault，可重复调用）"""
    for key in ("today", "page"):
        nested = item.get(key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                item.setdefault(k, v)
    return item


def _card_author(item: dict) -> str:
    """卡片作者展示名（author 可能是含 nickname/display_name 的对象）"""
    author = item.get("author") or item.get("user")
    if isinstance(author, dict):
        author = author.get("nickname") or author.get("display_name") or author.get("username")
    if not author:
        author = item.get("username") or item.get("nickname")
    return str(author) if author else ""


def _card_body(item: dict) -> str:
    """卡片正文（caption 已含 caption_suffix 时不重复追加）"""
    body = item.get("caption")
    suffix = item.get("caption_suffix")
    if suffix and suffix not in (body or ""):
        body = f"{body} {suffix}" if body else suffix
    if not body:
        nested = item.get("page")
        if isinstance(nested, dict):
            body = nested.get("caption")
    return str(body) if body else ""


def _card_id(item: dict):
    """卡片可用于「动态详情」的 ID"""
    return item.get("today_id") or item.get("today_uuid") or item.get("id")


def _today_card_text(item) -> str:
    """把一条 Today 卡片整理成一段文本（含可用于详情的 ID）"""
    if not isinstance(item, dict):
        return _cap(item, 160)

    _card_merge(item)
    card_id = _card_id(item)
    author = _card_author(item)
    body = _card_body(item)

    controls = item.get("content_controls")
    allow = None
    if isinstance(controls, dict):
        allow = controls.get("allow_screenshot")

    parts = []
    if body:
        parts.append(f"「{_cap(body, 150)}」")
    if author:
        parts.append(f"作者:{_cap(author, 40)}")
    card_type = item.get("card_type") or item.get("type")
    if card_type:
        parts.append(f"类型:{_cap(card_type, 24)}")
    if allow is not None:
        parts.append("可截图" if allow else "禁止截图")
    line = " ".join(parts) if parts else _cap(item, 160)

    if card_id:
        line += f"\n  · 详情ID: {_cap(card_id, 48)}（发「动态详情 {_cap(card_id, 48)}」查看）"
    return line


def format_today_list(data) -> str:
    """Today 列表类响应 -> 文本。返回空串表示没有可展示的卡片。"""
    root = _inner(data)
    items = _pick_card_list(data)
    if not items:
        return ""

    lines = []
    meta_parts = []
    for key, label in (("user", "用户"), ("topic", "话题"), ("gathering", "聚会")):
        name = _meta_name(root, key)
        if name:
            meta_parts.append(f"{label}：{_cap(name, 80)}")
    if meta_parts:
        lines.append("｜".join(meta_parts))
    if not meta_parts and isinstance(root.get("today"), dict):
        lines.append("【动态内容】")

    for i, item in enumerate(items[:_MAX_LIST], 1):
        lines.append(f"{i}. {_today_card_text(item)}")
    total = len(items)
    if total > _MAX_LIST:
        lines.append(f"\n… 共 {total} 条，仅显示前 {_MAX_LIST} 条")
    return "\n".join(lines).strip()


def format_today_single(data) -> str:
    """Today 详情/单条 -> 文本"""
    root = _inner(data)
    today = root.get("today")
    if not isinstance(today, dict):
        # 有些实现把单条卡片直接放顶层
        today = root
    card = _today_card_text(today)
    return card or ""


def format_feed_blocks(items: list, page: int = 1) -> str:
    """把动态流某一页渲染成 markdown：每条一个 text 代码块，序号按页内 1-6 编。

    形如::

        ```text
        1：作者：xxx
        内容：今天，xxxxx
        ```
    """
    start = (max(1, page) - 1) * FEED_PAGE_SIZE
    chunk = items[start : start + FEED_PAGE_SIZE]
    blocks = []
    for index, item in enumerate(chunk, 1):
        if isinstance(item, dict):
            _card_merge(item)
            author = _card_author(item) or "未知"
            body = _card_body(item) or "（无正文）"
        else:
            author, body = "未知", _cap(item, 150)
        blocks.append(f"```text\n{index}：作者：{_cap(author, 40)}\n内容：{_cap(body, 150)}\n```")
    return "\n\n".join(blocks)


def format_like_status(data) -> str:
    """点赞状态 -> 文本（兼容根级与 data 包裹）"""
    root = _inner(data) or {}
    lines = []
    count = root.get("like_count", root.get("likes_count"))
    if count is not None:
        lines.append(f"当前点赞数：{count}")
    liked = root.get("is_liked")
    if liked is not None:
        lines.append("TA 已被本账号点赞" if liked else "本账号尚未给 TA 点赞")
    can = root.get("can_like")
    if can is not None:
        lines.append("现在可以点赞" if can else "当前不可点赞")
    days = root.get("days_until_can_like")
    if days is not None and not root.get("can_like"):
        lines.append(f"距可再次点赞还需 {days} 天")
    # 兜底: 至少展示可读键
    if not lines:
        for k, v in root.items():
            if k in _SKIP_KEYS or isinstance(v, (dict, list)):
                continue
            lines.append(f"{k}: {_cap(v)}")
    return "\n".join(lines) or "（接口返回空内容）"


def format_health(data) -> str:
    """平台健康 -> 文本"""
    root = _inner(data) or {}
    msg = root.get("message") or root.get("msg") or root.get("status") or "运行中"
    return f"FTV/FurryWill 平台状态：{_cap(msg, 200)}"


def format_suggestions(data) -> str:
    """搜索建议 -> 文本（顶层可能是数组或 suggestions 键）"""
    items = data if isinstance(data, list) else None
    if items is None and isinstance(data, dict):
        items = data.get("suggestions") or data.get("data")
        if isinstance(items, dict):
            items = items.get("suggestions")
    if not items:
        return "暂无相关搜索建议"
    lines = [f"共 {len(items)} 条建议："]
    for i, s in enumerate(items[:_MAX_LIST], 1):
        lines.append(f"{i}. {_cap(s, 120)}")
    if len(items) > _MAX_LIST:
        lines.append(f"… 仅显示前 {_MAX_LIST} 条")
    return "\n".join(lines)


# ------------------------------------------------------------------ 指令
# 频道动态 = Today 探索流
today_explore_cmd = cmdbuild("ftv.today_explore", "频道动态").alias("动态探索").priority(5).block().build()


@today_explore_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    limit = 10
    if arg.extract_plain_text().strip():
        try:
            limit = int(arg.extract_plain_text().strip())
            limit = max(1, min(limit, 24))
        except ValueError:
            pass
    try:
        data = await furtv_api.get_today_explore(limit)
        text = format_today_list(data)
        if not text:
            await finish_text(today_explore_cmd, bot, "暂无频道动态")
        await finish_text(today_explore_cmd, bot, text)
    except FinishedException:
        raise
    except Exception as e:
        await finish_text(today_explore_cmd, bot, f"获取失败：{str(e)}")


# 动态流 = Today 时间线信息流
today_feed_cmd = cmdbuild("ftv.today_feed", "动态流").alias("今日动态").priority(5).block().build()


@today_feed_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    args = arg.extract_plain_text().strip().split()
    date = None
    limit = 10
    for token in args:
        if token.isdigit():
            limit = max(1, min(int(token), 20))
        elif "-" in token:
            date = token
    try:
        data = await furtv_api.get_today_feed(limit=limit, date=date)
        title = f"动态流{('（日期 ' + date + '）') if date else ''}"
        # goqbot 下多结果每页 6 条（text 代码块）+ 数字/翻页按钮
        items = _pick_card_list(data)
        if len(items) > 1 and await publish_feed(today_feed_cmd, bot, event, items, title):
            return
        text = format_today_list(data)
        if not text:
            await finish_text(today_feed_cmd, bot, "暂无动态")
        header = f"{title}\n\n"
        await finish_text(today_feed_cmd, bot, header + text, f"**{title}**\n\n{text}")
    except FinishedException:
        raise
    except Exception as e:
        await finish_text(today_feed_cmd, bot, f"获取失败：{str(e)}")


# 动态详情
today_detail_cmd = cmdbuild("ftv.today_detail", "动态详情").priority(5).block().build()


async def show_today_detail(
    cmd: Any, bot: Bot, event: MessageEvent, today_id: str
) -> bool:
    """展示单条动态详情；返回 False 表示没找到该动态（由调用方决定提示语）。"""
    try:
        data = await furtv_api.get_today_detail(today_id)
    except FileNotFoundError:
        return False
    text = format_today_single(data)
    if not text:
        await finish_text(cmd, bot, "该动态不存在或已被下架（可能开启了禁止截图保护）")
        return True
    await finish_text(cmd, bot, text)
    return True


@today_detail_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    today_id = arg.extract_plain_text().strip()
    if not today_id:
        await finish_text(today_detail_cmd, bot, "请提供动态 ID，例如：.动态详情 xxx")
    try:
        if not await show_today_detail(today_detail_cmd, bot, event, today_id):
            await finish_text(today_detail_cmd, bot, f"没有找到该动态（{today_id}）")
    except FinishedException:
        raise
    except Exception as e:
        await finish_text(today_detail_cmd, bot, f"获取失败：{str(e)}")


# 话题动态
today_topics_cmd = cmdbuild("ftv.today_topics", "话题动态").priority(5).block().build()


@today_topics_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    args = arg.extract_plain_text().strip().split()
    if not args:
        await finish_text(today_topics_cmd, bot, "请提供话题 ID 或话题名，例如：.话题动态 #兽聚")
    topic = args[0].lstrip("#")
    limit = 10
    if len(args) > 1 and args[1].isdigit():
        limit = max(1, min(int(args[1]), 30))
    try:
        data = await furtv_api.get_today_topics(topic, limit)
        text = format_today_list(data)
        if not text:
            await finish_text(today_topics_cmd, bot, f"话题 '{topic}' 暂无动态")
        await finish_text(today_topics_cmd, bot, text)
    except FinishedException:
        raise
    except FileNotFoundError:
        await finish_text(today_topics_cmd, bot, f"没有找到话题 '{topic}'")
    except Exception as e:
        await finish_text(today_topics_cmd, bot, f"获取失败：{str(e)}")


# 聚会动态
today_gatherings_cmd = cmdbuild("ftv.today_gatherings", "聚会动态").priority(5).block().build()


@today_gatherings_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    args = arg.extract_plain_text().strip().split()
    if not args:
        await finish_text(today_gatherings_cmd, bot, "请提供聚会 ID，例如：.聚会动态 12345")
    gathering_id = args[0]
    limit = 10
    if len(args) > 1 and args[1].isdigit():
        limit = max(1, min(int(args[1]), 30))
    try:
        data = await furtv_api.get_today_gatherings(gathering_id, limit)
        text = format_today_list(data)
        if not text:
            await finish_text(today_gatherings_cmd, bot, f"聚会 '{gathering_id}' 暂无动态")
        await finish_text(today_gatherings_cmd, bot, text)
    except FinishedException:
        raise
    except FileNotFoundError:
        await finish_text(today_gatherings_cmd, bot, f"没有找到聚会 '{gathering_id}'")
    except Exception as e:
        await finish_text(today_gatherings_cmd, bot, f"获取失败：{str(e)}")


# 用户动态（时间线）
today_user_cmd = cmdbuild("ftv.today_user", "用户动态").alias("动态主页").priority(5).block().build()


async def show_today_user(
    cmd: Any, bot: Bot, event: MessageEvent, user: str, limit: int = 10
) -> bool:
    """按用户名展示 Today 时间线；返回 False 表示查不到该用户。"""
    try:
        data = await furtv_api.get_today_user_timeline(user, limit)
    except FileNotFoundError:
        return False
    text = format_today_list(data)
    if not text:
        await finish_text(cmd, bot, f"用户 '{user}' 暂无动态")
        return True
    await finish_text(cmd, bot, text)
    return True


@today_user_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    args = arg.extract_plain_text().strip().split()
    if not args:
        await finish_text(today_user_cmd, bot, "请提供用户名，例如：.用户动态 xj123")
    user = args[0]
    limit = 10
    if len(args) > 1 and args[1].isdigit():
        limit = max(1, min(int(args[1]), 24))
    try:
        if await show_today_user(today_user_cmd, bot, event, user, limit):
            return
        if not await resolve_user_fallback(
            today_user_cmd, bot, event, user, partial(show_today_user, limit=limit)
        ):
            await finish_text(today_user_cmd, bot, f"没有找到用户 '{user}'")
    except FinishedException:
        raise
    except Exception as e:
        await finish_text(today_user_cmd, bot, f"获取失败：{str(e)}")


# 今日发布（某用户当前 Today）
today_current_cmd = cmdbuild("ftv.today_current", "今日发布").priority(5).block().build()


async def show_today_current(
    cmd: Any, bot: Bot, event: MessageEvent, user: str
) -> bool:
    """展示某用户今天的 Today；返回 False 表示查不到该用户。"""
    try:
        data = await furtv_api.get_today_user_current(user)
    except FileNotFoundError:
        return False
    text = format_today_single(data)
    if not text:
        await finish_text(cmd, bot, f"用户 '{user}' 今天还没有发布动态")
        return True
    await finish_text(
        cmd, bot,
        f"用户 {user} 今天的动态：\n{text}",
        f"**用户 {user} 今天的动态**\n\n{text}",
    )
    return True


@today_current_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    user = arg.extract_plain_text().strip()
    if not user:
        await finish_text(today_current_cmd, bot, "请提供用户名，例如：.今日发布 xj123")
    try:
        if await show_today_current(today_current_cmd, bot, event, user):
            return
        if not await resolve_user_fallback(
            today_current_cmd, bot, event, user, show_today_current
        ):
            await finish_text(today_current_cmd, bot, f"没有找到用户 '{user}'")
    except FinishedException:
        raise
    except Exception as e:
        await finish_text(today_current_cmd, bot, f"获取失败：{str(e)}")


# 搜索建议
search_suggest_cmd = cmdbuild("ftv.search_suggest", "搜索建议").priority(5).block().build()


@search_suggest_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    q = arg.extract_plain_text().strip()
    if not q or len(q) < 2:
        await finish_text(search_suggest_cmd, bot, "请提供至少 2 个字符，例如：.搜索建议 狐")
    try:
        data = await furtv_api.get_search_suggestions(q)
        await finish_text(search_suggest_cmd, bot, f"「{q}」{format_suggestions(data)}")
    except FinishedException:
        raise
    except Exception as e:
        await finish_text(search_suggest_cmd, bot, f"获取失败：{str(e)}")


# 点赞状态
like_status_cmd = cmdbuild("ftv.like_status", "点赞状态").priority(5).block().build()


async def show_like_status(
    cmd: Any, bot: Bot, event: MessageEvent, username: str
) -> bool:
    """展示某用户点赞状态；返回 False 表示查不到该用户。"""
    try:
        data = await furtv_api.get_user_like_status(username)
    except FileNotFoundError:
        return False
    await finish_text(
        cmd, bot,
        f"用户 {username} 点赞状态：\n{format_like_status(data)}",
        f"**用户 {username} 点赞状态**\n\n{format_like_status(data)}",
    )
    return True


@like_status_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    username = arg.extract_plain_text().strip()
    if not username:
        await finish_text(like_status_cmd, bot, "请提供用户名，例如：.点赞状态 xj123")
    try:
        if await show_like_status(like_status_cmd, bot, event, username):
            return
        if not await resolve_user_fallback(
            like_status_cmd, bot, event, username, show_like_status
        ):
            await finish_text(like_status_cmd, bot, f"没有找到用户 '{username}'")
    except FinishedException:
        raise
    except Exception as e:
        await finish_text(like_status_cmd, bot, f"获取失败：{str(e)}")


# 平台状态
health_cmd = cmdbuild("ftv.health", "平台状态").alias("兽频道状态").priority(5).block().build()


@health_cmd.handle()
async def _(bot: Bot, event: MessageEvent):
    try:
        data = await furtv_api.get_platform_health()
        await finish_text(health_cmd, bot, format_health(data))
    except FinishedException:
        raise
    except Exception as e:
        await finish_text(health_cmd, bot, f"平台状态检查失败：{str(e)}")
