"""guanli 管理面板 · QQ 侧指令

与 ``configs/commands.json`` 中的 guanli 指令一一对应。

**平台差异统一走 ``util.gobot_api``**：同一套指令在 goqbot（QQ 官方桥接）与标准
OneBot（go-cqhttp / llbot 等）下都能跑，因为抽象层会把两端 action 名、参数差异
抹平；确实只有一端支持的能力（例如全员禁言）会抛 ``PlatformNotSupportedError``，
这里翻译成一句人话提示，而不是静默失败。

===========================  ==============  ==========================================
指令                         权限            说明
===========================  ==============  ==========================================
禁言 / 解除禁言               admin           单成员禁言、解禁（两端同 action）
全体禁言 / 解除全体禁言        admin           全员禁言（仅标准 OneBot；官方群 v2 无此能力）
撤回                         admin           回复某条消息撤回，或直接给 message_id
入群申请                     admin           同步 / 列表 / 同意 / 拒绝（两端都支持）
发言统计                     member          本群发言排行、查自己的排名
黑白名单                     admin           黑名单拦截、白名单豁免
自动回复                     admin           关键词自动回复（精确 / 前缀 / 包含）
入群欢迎 / 退群提醒            admin           自定义欢迎语与退群播报
操作日志                     admin           最近的群管操作审计
更新违禁词                   super           维护全局违禁词
开启 / 关闭图文监控           admin           本群是否启用违禁词监测
群关键词设置                 admin           本群关键词
群插件开关                   admin           本群按插件粒度启停
获取配置秘钥                 super           返回管理面板地址、可用登录方式与应急口令
管理面板                     super           关键词回复面板地址（不占命令前缀）
===========================  ==============  ==========================================

本模块位于 ``util/guanli_panel``，由 ``src/plugins/guanli`` 引导加载（指令归属仍记在
guanli 插件名下，与 ``configs/commands.json`` 保持一致）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from nonebot import on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupDecreaseNoticeEvent,
    GroupIncreaseNoticeEvent,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.log import logger
from nonebot.params import CommandArg
from nonebot.rule import Rule
from nonebot.typing import T_State

from util import (
    PlatformNotSupportedError,
    cmdbuild,
    delete_msg,
    finish_text,
    get_group_system_msg,
    on_group_decrease,
    on_group_increase,
    send_group_msg,
    set_group_add_request,
    set_group_ban,
    set_group_whole_ban,
)

from . import store
from .config import get_config, save_config
from .web import auth_config, bind_loop, panel_base_url, panel_token, parse_join_requests


# ------------------------------------------------------------------ 小工具
def _plain(arg: Message) -> str:
    return (arg.extract_plain_text() or "").strip()


def _split_words(raw: str) -> List[str]:
    for sep in ("，", ",", "、", "\n", " "):
        raw = raw.replace(sep, "\x00")
    return [w.strip() for w in raw.split("\x00") if w.strip()]


def _parse_duration(raw: str, default: int) -> int:
    """把 ``10`` / ``10m`` / ``2h`` / ``1d`` 解析成秒"""
    text = (raw or "").strip().lower()
    if not text:
        return default
    unit = text[-1]
    try:
        value = int(text[:-1]) if unit in "smhd" else int(text)
    except ValueError:
        return default
    factor = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit, 1)
    return max(60, value * factor)


def _target_of(event: MessageEvent, arg: Message) -> Optional[int]:
    """从消息里取 @某人 或纯数字 QQ 号"""
    for seg in event.get_message():
        if seg.type == "at" and str(seg.data.get("qq", "")).isdigit():
            qq = int(seg.data["qq"])
            if qq != int(event.self_id):
                return qq
    text = _plain(arg)
    if text.isdigit():
        return int(text)
    return None


def _reply_id(event: MessageEvent) -> Optional[int]:
    """取被回复消息的 message_id（撤回指令用）"""
    for seg in event.get_message():
        if seg.type == "reply":
            raw = seg.data.get("id") or seg.data.get("message_id")
            if raw is not None and str(raw).isdigit():
                return int(raw)
    return None


def _is_super(qq: Any) -> bool:
    try:
        from util.config_manager import get_config_manager

        return str(qq) in get_config_manager().get("bot.superusers", [])
    except Exception:
        return False


def _panel_link(with_token: bool = False) -> str:
    url = panel_base_url()
    if with_token:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}token={panel_token()}"
    return url


def _at_seg(user_id: Any) -> MessageSegment:
    """@某人；官方端拿不到数字 QQ 时退化成文本占位"""
    raw = str(user_id)
    if raw.isdigit():
        return MessageSegment.at(int(raw))
    return MessageSegment.text(f"@{raw}")


def _render(template: str, *, user_id: Any = "", group_id: Any = "", self_id: Any = "") -> Message:
    """渲染欢迎语 / 退群提醒文案中的占位符

    支持 ``{at}``（@成员）、``{user}`` / ``{qq}``（QQ 号）、``{group}`` / ``{群}``（群号）。
    """
    text = str(template or "")
    for token in ("{user}", "{qq}"):
        text = text.replace(token, str(user_id))
    for token in ("{group}", "{群}"):
        text = text.replace(token, str(group_id))
    parts = text.split("{at}")
    msg = Message()
    for index, part in enumerate(parts):
        if index:
            msg += _at_seg(user_id)
        if part:
            msg += Message(part)
    return msg


def _name_sets(group_id: Any) -> Tuple[Set[str], Set[str]]:
    """一次查询取出本群黑名单 / 白名单（消息热路径上少一次 IO）"""
    black: Set[str] = set()
    white: Set[str] = set()
    try:
        for entry in store.list_entries(group_id):
            target = black if entry.get("kind") == "black" else white
            target.add(str(entry.get("user_id")))
    except Exception as e:
        logger.warning(f"guanli 读取群名单失败：{e!r}")
    return black, white


def _platform_hint(exc: Exception) -> str:
    """把 gobot_api 的平台不支持异常翻译成用户能看懂的一句话"""
    if isinstance(exc, PlatformNotSupportedError):
        return f"当前协议端不支持该操作：{exc}"
    return f"操作失败：{exc}"


async def _say(cmd: Any, bot: Bot, plain: str, markdown: str = "") -> None:
    """统一出口：默认走 Markdown 富文本，协议端不支持时自动回落纯文本"""
    await finish_text(cmd, bot, plain, markdown)


# ------------------------------------------------------------------ 禁言 / 解除禁言
mute_cmd = cmdbuild("guanli.mute", "禁言").alias("口球", "mute").level("admin").priority(6).block().build()
unmute_cmd = cmdbuild("guanli.unmute", "解除禁言").alias("解禁", "unmute").level("admin").priority(6).block().build()


@mute_cmd.handle()
async def _handle_mute(bot: Bot, event: MessageEvent, state: T_State, arg: Message = CommandArg()) -> None:
    cfg = get_config()
    if not isinstance(event, GroupMessageEvent):
        await _say(mute_cmd, bot, "禁言只能在群里使用。")
        return

    qq = _target_of(event, arg)
    if qq is None:
        await _say(mute_cmd, bot, "用法：禁言 @某人 [时长]，例如「禁言 @某某 10m」")
        return

    text = _plain(arg)
    parts = [p for p in text.split() if not p.isdigit() or p != str(qq)]
    duration = _parse_duration(parts[-1] if parts else "", cfg.anti_mute_duration)

    try:
        await set_group_ban(bot, event.group_id, qq, duration)
    except Exception as e:
        await _say(mute_cmd, bot, _platform_hint(e))
        return
    warns = store.add_warn(event.group_id, qq)
    store.add_audit(
        "禁言", actor=str(event.user_id), group_id=event.group_id, target=qq,
        detail=f"{duration} 秒", source="qq",
    )
    await _say(mute_cmd, bot, f"已禁言 {qq} {duration} 秒（当前违规 {warns} 次）。")


@unmute_cmd.handle()
async def _handle_unmute(bot: Bot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    if not isinstance(event, GroupMessageEvent):
        await _say(unmute_cmd, bot, "解除禁言只能在群里使用。")
        return
    qq = _target_of(event, arg)
    if qq is None:
        await _say(unmute_cmd, bot, "用法：解除禁言 @某人")
        return
    try:
        await set_group_ban(bot, event.group_id, qq, 0)
    except Exception as e:
        await _say(unmute_cmd, bot, _platform_hint(e))
        return
    store.clear_warn(event.group_id, qq)
    store.add_audit(
        "解除禁言", actor=str(event.user_id), group_id=event.group_id, target=qq, source="qq"
    )
    await _say(unmute_cmd, bot, f"已解除 {qq} 的禁言，违规计数已清零。")


# ------------------------------------------------------------------ 全员禁言
wholeban_cmd = (
    cmdbuild("guanli.wholeban", "全体禁言").alias("全员禁言", "wholeban").level("admin").priority(6).block().build()
)


@wholeban_cmd.handle()
async def _handle_wholeban(bot: Bot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    if not isinstance(event, GroupMessageEvent):
        await _say(wholeban_cmd, bot, "全体禁言只能在群里使用。")
        return
    text = _plain(arg).lower()
    enable = text not in ("关", "关闭", "解除", "off", "false", "0")
    try:
        await set_group_whole_ban(bot, event.group_id, enable)
    except PlatformNotSupportedError as e:
        await _say(
            wholeban_cmd,
            bot,
            f"{e}\n提示：官方群（v2）只提供成员级禁言，全员禁言仅在标准 OneBot 协议端可用。",
        )
        return
    except Exception as e:
        await _say(wholeban_cmd, bot, _platform_hint(e))
        return
    store.add_audit(
        "全员禁言" if enable else "解除全员禁言",
        actor=str(event.user_id),
        group_id=event.group_id,
        source="qq",
    )
    await _say(wholeban_cmd, bot, "已开启全员禁言。" if enable else "已解除全员禁言。")


# ------------------------------------------------------------------ 撤回消息
recall_cmd = cmdbuild("guanli.recall", "撤回").alias("recall", "撤消息").level("admin").priority(6).block().build()


@recall_cmd.handle()
async def _handle_recall(bot: Bot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    raw = _plain(arg)
    message_id = int(raw) if raw.isdigit() else _reply_id(event)
    if message_id is None:
        await _say(recall_cmd, bot, "用法：回复要撤回的消息并发送「撤回」，或「撤回 <message_id>」")
        return
    try:
        await delete_msg(bot, message_id)
    except Exception as e:
        await _say(recall_cmd, bot, _platform_hint(e))
        return
    store.add_audit(
        "撤回消息",
        actor=str(event.user_id),
        group_id=getattr(event, "group_id", ""),
        detail=f"message_id={message_id}",
        source="qq",
    )
    await _say(recall_cmd, bot, f"已撤回消息 {message_id}。")


# ------------------------------------------------------------------ 违禁词
anti_cmd = cmdbuild("guanli.anti", "更新违禁词").alias("违禁词", "anti").level("super").priority(6).block().build()


@anti_cmd.handle()
async def _handle_anti(bot: Bot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    text = _plain(arg)
    cfg = get_config()
    words = list(cfg.anti_keywords)
    action, _, rest = text.partition(" ")
    action = action or "列表"

    if action in ("列表", "list", ""):
        body = "、".join(words) if words else "（空）"
        await _say(anti_cmd, bot, f"当前违禁词 {len(words)} 个：\n{body}")
        return

    if action in ("清空", "clear"):
        cfg.anti_keywords = []
        save_config(cfg)
        await _say(anti_cmd, bot, "已清空违禁词。")
        return

    if action in ("添加", "add", "+"):
        added = [w for w in _split_words(rest) if w not in words]
        cfg.anti_keywords = words + added
        save_config(cfg)
        await _say(anti_cmd, bot, f"已添加 {len(added)} 个：{'、'.join(added) or '（无新增）'}")
        return

    if action in ("删除", "del", "remove", "-"):
        removed = [w for w in _split_words(rest) if w in words]
        cfg.anti_keywords = [w for w in words if w not in removed]
        save_config(cfg)
        await _say(anti_cmd, bot, f"已删除 {len(removed)} 个：{'、'.join(removed) or '（无匹配）'}")
        return

    await _say(anti_cmd, bot, "用法：更新违禁词 列表 / 添加 a,b / 删除 a / 清空")


# ------------------------------------------------------------------ 图文监控
watch_on = cmdbuild("guanli.watch.on", "开启图文监控").alias("开启监控").level("admin").priority(6).block().build()
watch_off = cmdbuild("guanli.watch.off", "关闭图文监控").alias("关闭监控").level("admin").priority(6).block().build()


async def _set_watch(bot: Bot, event: MessageEvent, cmd: Any, on: bool) -> None:
    if not isinstance(event, GroupMessageEvent):
        await _say(cmd, bot, "该设置仅对群生效，请在群里使用。")
        return
    data = store.save_group(event.group_id, watch_image=on)
    await _say(cmd, bot, "本群图文监控已开启。" if data["watch_image"] else "本群图文监控已关闭。")


@watch_on.handle()
async def _(bot: Bot, event: MessageEvent) -> None:
    await _set_watch(bot, event, watch_on, True)


@watch_off.handle()
async def _(bot: Bot, event: MessageEvent) -> None:
    await _set_watch(bot, event, watch_off, False)


# ------------------------------------------------------------------ 群关键词
kw_cmd = cmdbuild("guanli.keywords", "群关键词设置").alias("群关键词", "keywords").level("admin").priority(6).block().build()


@kw_cmd.handle()
async def _handle_keywords(bot: Bot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    if not isinstance(event, GroupMessageEvent):
        await _say(kw_cmd, bot, "群关键词仅在群里可设置。")
        return
    data = store.get_group(event.group_id)
    words: List[str] = list(data.get("keywords") or [])
    text = _plain(arg)
    action, _, rest = text.partition(" ")
    action = action or "列表"

    if action in ("列表", "list", ""):
        body = "、".join(words) if words else "（空）"
        await _say(kw_cmd, bot, f"本群关键词 {len(words)} 个：\n{body}")
        return
    if action in ("清空", "clear"):
        store.save_group(event.group_id, keywords=[])
        await _say(kw_cmd, bot, "已清空本群关键词。")
        return
    if action in ("添加", "add", "+"):
        added = [w for w in _split_words(rest) if w not in words]
        store.save_group(event.group_id, keywords=words + added)
        await _say(kw_cmd, bot, f"已添加 {len(added)} 个：{'、'.join(added) or '（无新增）'}")
        return
    if action in ("删除", "del", "remove", "-"):
        removed = [w for w in _split_words(rest) if w in words]
        store.save_group(event.group_id, keywords=[w for w in words if w not in removed])
        await _say(kw_cmd, bot, f"已删除 {len(removed)} 个：{'、'.join(removed) or '（无匹配）'}")
        return
    await _say(kw_cmd, bot, "用法：群关键词设置 列表 / 添加 a,b / 删除 a / 清空")


# ------------------------------------------------------------------ 群插件开关
plug_cmd = cmdbuild("guanli.plugins", "群插件开关").alias("群插件", "plugswitch").level("admin").priority(6).block().build()


@plug_cmd.handle()
async def _handle_plugins(bot: Bot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    if not isinstance(event, GroupMessageEvent):
        await _say(plug_cmd, bot, "群插件开关仅在群里可设置。")
        return
    data = store.get_group(event.group_id)
    switches = dict(data.get("plugins") or {})
    text = _plain(arg)
    action, _, rest = text.partition(" ")
    action = action or "列表"

    if action in ("列表", "list", ""):
        body = "、".join(f"{k}={'开' if v else '关'}" for k, v in switches.items()) or "（全部默认开启）"
        await _say(plug_cmd, bot, f"本群插件开关：\n{body}")
        return
    if action in ("清空", "clear", "重置"):
        store.save_group(event.group_id, plugins={})
        await _say(plug_cmd, bot, "已清空本群插件开关，全部恢复默认。")
        return

    # 支持「ftv 开 / ftv 关」「ftv=1 / ftv=0」「开启 ftv / 关闭 ftv」
    updates: Dict[str, bool] = {}
    tokens = _split_words(rest or text)
    if action in ("开启", "打开", "on", "enable"):
        updates = {k: True for k in _split_words(rest)}
    elif action in ("关闭", "关掉", "off", "disable"):
        updates = {k: False for k in _split_words(rest)}
    else:
        tokens = [action] + tokens
        for i, token in enumerate(tokens):
            if "=" in token:
                name, _, value = token.partition("=")
                updates[name.strip()] = value.strip().lower() not in ("0", "false", "off", "关")
            elif token in ("开", "打开", "开启", "on") and i > 0:
                updates[tokens[i - 1]] = True
            elif token in ("关", "关闭", "off") and i > 0:
                updates[tokens[i - 1]] = False

    if not updates:
        await _say(
            plug_cmd,
            bot,
            "用法：群插件开关 ftv=1 / 群插件开关 开启 ftv / 群插件开关 关闭 ftv / 群插件开关 列表 / 清空",
        )
        return
    store.save_group(event.group_id, plugins=updates)
    body = "、".join(f"{k}={'开' if v else '关'}" for k, v in updates.items())
    await _say(plug_cmd, bot, f"已更新：{body}")


# ------------------------------------------------------------------ 入群申请
approval_cmd = (
    cmdbuild("guanli.approval", "入群申请").alias("加群申请", "审批").level("admin").priority(6).block().build()
)


def _resolve_approval(group_id: Any, key: str) -> Optional[Dict[str, Any]]:
    """按「序号」或「flag」定位一条待处理申请"""
    key = (key or "").strip()
    pending = [r for r in store.list_approvals(group_id, "pending")]
    if key.isdigit():
        index = int(key)
        if 1 <= index <= len(pending):
            return pending[index - 1]
    if key:
        return store.get_approval(key) or {"flag": key, "group_id": str(group_id), "user_id": ""}
    return pending[0] if pending else None


@approval_cmd.handle()
async def _handle_approval(bot: Bot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    if not isinstance(event, GroupMessageEvent):
        await _say(approval_cmd, bot, "入群申请只能在群里处理。")
        return
    text = _plain(arg)
    action, _, rest = text.partition(" ")
    action = (action or "列表").strip()
    group_id = event.group_id

    if action in ("同步", "刷新", "refresh", "pull"):
        try:
            data = await get_group_system_msg(bot, group_id)
        except Exception as e:
            await _say(approval_cmd, bot, _platform_hint(e))
            return
        items = parse_join_requests(group_id, data)
        for item in items:
            store.save_approval(
                item["flag"], item["group_id"], item["user_id"], item["comment"], item["sub_type"]
            )
        await _say(approval_cmd, bot, f"已从协议端同步 {len(items)} 条入群申请。")
        return

    if action in ("同意", "通过", "approve", "拒绝", "驳回", "reject"):
        approve = action in ("同意", "通过", "approve")
        record = _resolve_approval(group_id, rest)
        if not record:
            await _say(approval_cmd, bot, "当前没有待处理的入群申请。")
            return
        flag = str(record.get("flag") or "")
        try:
            await set_group_add_request(
                bot,
                flag,
                approve,
                None,
                group_id=int(group_id),
                user_id=int(record["user_id"]) if str(record.get("user_id") or "").isdigit() else None,
                sub_type=str(record.get("sub_type") or "add"),
            )
        except Exception as e:
            await _say(approval_cmd, bot, _platform_hint(e))
            return
        store.set_approval_status(flag, "approved" if approve else "rejected")
        store.add_audit(
            "入群审批",
            actor=str(event.user_id),
            group_id=group_id,
            target=record.get("user_id") or "",
            detail="同意" if approve else "拒绝",
            source="qq",
        )
        await _say(approval_cmd, bot, f"已{'同意' if approve else '拒绝'} {record.get('user_id') or flag} 的入群申请。")
        return

    # 默认：列表
    pending = store.list_approvals(group_id, "pending")
    if not pending:
        await _say(
            approval_cmd,
            bot,
            "当前没有待处理的入群申请。\n用法：入群申请 同步 / 列表 / 同意 1 / 拒绝 1",
        )
        return
    lines = [f"{i}. {r.get('user_id') or '（未知）'}　{r.get('comment') or '（无附言）'}" for i, r in enumerate(pending[:20], 1)]
    await _say(
        approval_cmd,
        bot,
        "待处理入群申请共 {} 条：\n{}\n\n同意用「入群申请 同意 序号」，拒绝用「入群申请 拒绝 序号」".format(
            len(pending), "\n".join(lines)
        ),
        markdown="**待处理入群申请 · {} 条**\n{}".format(len(pending), "\n".join(lines)),
    )


# ------------------------------------------------------------------ 发言统计
stat_cmd = cmdbuild("guanli.stat", "发言统计").alias("发言排行", "水群榜", "stat").level("member").priority(6).block().build()


@stat_cmd.handle()
async def _handle_stat(bot: Bot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    if not isinstance(event, GroupMessageEvent):
        await _say(stat_cmd, bot, "发言统计只能在群里查看。")
        return
    text = _plain(arg)
    if text in ("我的", "me", "自己"):
        count = 0
        for row in store.list_stats(event.group_id, 500):
            if str(row["user_id"]) == str(event.user_id):
                count = int(row["count"])
                break
        rank = store.rank_of(event.group_id, event.user_id, 500)
        await _say(
            stat_cmd,
            bot,
            f"你在本群发言 {count} 条，当前排名第 {rank} 名。" if rank else f"你还没有发言记录（{count} 条）。",
        )
        return

    rows = store.list_stats(event.group_id, 10)
    if not rows:
        await _say(stat_cmd, bot, "本群还没有发言统计。")
        return
    lines = [f"{i}. {r['user_id']}　{int(r['count'])} 条" for i, r in enumerate(rows, 1)]
    await _say(
        stat_cmd,
        bot,
        "本群发言排行：\n" + "\n".join(lines),
        markdown="**本群发言排行 · Top {}**\n{}".format(len(lines), "\n".join(lines)),
    )


# ------------------------------------------------------------------ 黑白名单
list_cmd = cmdbuild("guanli.lists", "黑白名单").alias("名单", "黑名单", "白名单").level("admin").priority(6).block().build()


@list_cmd.handle()
async def _handle_lists(bot: Bot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    if not isinstance(event, GroupMessageEvent):
        await _say(list_cmd, bot, "黑白名单只能在群里设置。")
        return
    text = _plain(arg)
    action, _, rest = text.partition(" ")
    action = (action or "列表").strip()
    group_id = event.group_id

    if action in ("列表", "list", ""):
        black, white = _name_sets(group_id)
        await _say(
            list_cmd,
            bot,
            "本群黑名单（{}）：{}\n本群白名单（{}）：{}".format(
                len(black), "、".join(sorted(black)) or "（空）",
                len(white), "、".join(sorted(white)) or "（空）",
            ),
        )
        return

    kind = "white" if action in ("加白", "白名单", "white") else "black"
    if action in ("删除", "移除", "del", "remove") or rest.startswith(("删除", "移除")):
        qq = _target_of(event, arg)
        if qq is None:
            await _say(list_cmd, bot, "用法：黑白名单 删除 @某人")
            return
        removed = store.remove_from_list(group_id, qq)
        store.add_audit("移出名单", actor=str(event.user_id), group_id=group_id, target=qq, source="qq")
        await _say(list_cmd, bot, f"已把 {qq} 移出名单（{removed} 条记录）。")
        return

    if action in ("加黑", "黑名单", "black", "添加", "add", "加入") or action in ("加白",):
        qq = _target_of(event, arg)
        if qq is None:
            await _say(list_cmd, bot, "用法：黑白名单 加黑 @某人 / 黑白名单 加白 @某人")
            return
        store.add_to_list(group_id, qq, kind)
        store.add_audit(
            "加入名单", actor=str(event.user_id), group_id=group_id, target=qq,
            detail="白名单" if kind == "white" else "黑名单", source="qq",
        )
        await _say(list_cmd, bot, f"已把 {qq} 加入{'白名单（豁免）' if kind == 'white' else '黑名单（拦截）'}。")
        return

    await _say(list_cmd, bot, "用法：黑白名单 列表 / 加黑 @某人 / 加白 @某人 / 删除 @某人")


# ------------------------------------------------------------------ 自动回复
reply_cmd = cmdbuild("guanli.reply", "自动回复").alias("关键词回复", "autoreply").level("admin").priority(6).block().build()


@reply_cmd.handle()
async def _handle_reply(bot: Bot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    if not isinstance(event, GroupMessageEvent):
        await _say(reply_cmd, bot, "自动回复只能在群里设置。")
        return
    text = _plain(arg)
    action, _, rest = text.partition(" ")
    action = (action or "列表").strip()
    group_id = event.group_id

    if action in ("列表", "list", ""):
        items = store.list_autoreplies(group_id)
        if not items:
            await _say(reply_cmd, bot, "还没有自动回复规则。")
            return
        lines = [
            "{}. [{}] {} → {}　{}".format(
                i,
                "全局" if not r.get("group_id") else "本群",
                r.get("pattern"),
                r.get("reply"),
                "" if r.get("enabled") else "（已停用）",
            )
            for i, r in enumerate(items[:20], 1)
        ]
        await _say(reply_cmd, bot, "自动回复规则：\n" + "\n".join(lines))
        return

    if action in ("删除", "del", "remove"):
        target = rest.strip()
        if not target.isdigit():
            await _say(reply_cmd, bot, "用法：自动回复 删除 <序号>")
            return
        items = store.list_autoreplies(group_id)
        index = int(target)
        if not 1 <= index <= len(items):
            await _say(reply_cmd, bot, "序号超出范围，先看「自动回复 列表」。")
            return
        store.remove_autoreply(items[index - 1]["id"])
        await _say(reply_cmd, bot, f"已删除规则 {index}。")
        return

    # 添加：自动回复 添加 触发词 => 回复内容（模式可选：精确 / 前缀）
    mode = "contains"
    if action in ("精确", "exact"):
        mode = "exact"
        action, _, rest = rest.partition(" ")
    elif action in ("前缀", "prefix"):
        mode = "prefix"
        action, _, rest = rest.partition(" ")
    if action in ("添加", "add", "设置") or mode != "contains":
        payload = rest if mode != "contains" else rest
        pattern, _, reply = payload.partition("=>") if "=>" in payload else payload.partition(" ")
        pattern = pattern.strip()
        reply = reply.strip()
        if not pattern or not reply:
            await _say(reply_cmd, bot, "用法：自动回复 添加 触发词 => 回复内容（也可「自动回复 精确 触发词 => 内容」）")
            return
        try:
            store.add_autoreply(group_id, pattern, reply, mode)
        except ValueError as e:
            await _say(reply_cmd, bot, str(e))
            return
        store.add_audit(
            "设置自动回复", actor=str(event.user_id), group_id=group_id,
            detail=f"{pattern} → {reply}", source="qq",
        )
        await _say(reply_cmd, bot, f"已设置自动回复：命中「{pattern}」回复「{reply}」（{mode}）。")
        return

    await _say(
        reply_cmd,
        bot,
        "用法：自动回复 列表 / 添加 触发词 => 回复内容 / 精确 触发词 => 内容 / 删除 <序号>",
    )


# ------------------------------------------------------------------ 入群欢迎 / 退群提醒
welcome_cmd = cmdbuild("guanli.welcome", "入群欢迎").alias("设置欢迎", "welcome").level("admin").priority(6).block().build()
leave_cmd = cmdbuild("guanli.leave", "退群提醒").alias("设置退群提醒", "leave").level("admin").priority(6).block().build()


async def _set_notice(bot: Bot, event: MessageEvent, cmd: Any, field: str, arg: Message) -> None:
    """设置 / 查看 / 关闭欢迎语或退群提醒文案"""
    if not isinstance(event, GroupMessageEvent):
        await _say(cmd, bot, "该设置仅对群生效，请在群里使用。")
        return
    text = _plain(arg)
    data = store.get_group(event.group_id)
    current = str(data.get(field) or "")

    if text in ("关闭", "关", "off", "清空", "clear"):
        store.save_group(event.group_id, **{field: ""})
        await _say(cmd, bot, "已关闭该播报。")
        return
    if not text or text in ("查看", "show", "列表"):
        await _say(
            cmd,
            bot,
            "当前文案：{}\n\n可用占位符：{{at}} @TA、{{user}} QQ 号、{{group}} 群号".format(current or "（未设置）"),
        )
        return
    store.save_group(event.group_id, **{field: text})
    store.add_audit(
        "设置播报文案", actor=str(event.user_id), group_id=event.group_id,
        detail=f"{field} = {text[:80]}", source="qq",
    )
    await _say(cmd, bot, f"已保存文案：\n{text}")


@welcome_cmd.handle()
async def _handle_welcome(bot: Bot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    await _set_notice(bot, event, welcome_cmd, "welcome", arg)


@leave_cmd.handle()
async def _handle_leave(bot: Bot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    await _set_notice(bot, event, leave_cmd, "leave_notice", arg)


# ------------------------------------------------------------------ 操作日志
audit_cmd = cmdbuild("guanli.audit", "操作日志").alias("群管日志", "audit").level("admin").priority(6).block().build()


@audit_cmd.handle()
async def _handle_audit(bot: Bot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    if not isinstance(event, GroupMessageEvent):
        await _say(audit_cmd, bot, "操作日志只能在群里查看。")
        return
    rows = store.list_audit(20, event.group_id)
    if not rows:
        await _say(audit_cmd, bot, "本群还没有操作记录。")
        return
    lines = [
        "{} [{}] {} {} {}".format(
            str(r.get("ts") or "")[5:16],
            "面板" if r.get("source") == "panel" else "群内",
            r.get("action") or "",
            r.get("target") or "",
            r.get("detail") or "",
        ).strip()
        for r in rows
    ]
    await _say(
        audit_cmd,
        bot,
        "最近操作：\n" + "\n".join(lines),
        markdown="**最近操作日志**\n{}".format("\n".join(lines)),
    )


# ------------------------------------------------------------------ 获取配置秘钥
token_cmd = cmdbuild("guanli.token", "获取配置秘钥").alias("配置秘钥", "面板秘钥").level("super").priority(6).block().build()


def _login_ways() -> str:
    """把当前可用的登录渠道拼成一行文字，方便超管知道该用哪种方式登面板"""
    cfg = auth_config()
    ways = []
    if cfg.get("password"):
        ways.append("账号密码")
    if (cfg.get("oidc") or {}).get("enabled"):
        ways.append(str(cfg["oidc"].get("name") or "OIDC") + " 单点登录")
    ways.append("访问口令（应急）")
    return " / ".join(ways)


@token_cmd.handle()
async def _handle_token(bot: Bot, event: MessageEvent) -> None:
    url = panel_base_url()
    await _say(
        token_cmd,
        bot,
        "管理面板\n地址：{url}\n登录方式：{ways}\n应急口令：{token}\n（带口令链接：{link}）".format(
            url=url,
            ways=_login_ways(),
            token=panel_token(),
            link=_panel_link(with_token=True),
        ),
    )


# ------------------------------------------------------------------ 管理面板（关键词，不占命令前缀）
_PANEL_WORDS = {"管理面板", "管理后台", "面板地址", "后台地址"}


def _is_panel_word(event: MessageEvent) -> bool:
    """只有整条消息正好是面板关键词时才触发（避免 block 掉其它插件的消息处理）"""
    return (event.get_plaintext() or "").strip() in _PANEL_WORDS


panel_cmd = on_message(rule=Rule(_is_panel_word), priority=20, block=True)


@panel_cmd.handle()
async def _handle_panel(bot: Bot, event: MessageEvent) -> None:
    if not _is_super(event.user_id):
        await panel_cmd.finish()
    await panel_cmd.finish(
        Message(
            "管理面板\n地址：{url}\n登录方式：{ways}\n应急口令：{token}".format(
                url=panel_base_url(), ways=_login_ways(), token=panel_token()
            )
        )
    )


# ------------------------------------------------------------------ 入群 / 退群 播报
_increase = on_group_increase().priority(5).block(False).build()
_decrease = on_group_decrease().priority(5).block(False).build()


@_increase.handle()
async def _on_group_increase(bot: Bot, event: GroupIncreaseNoticeEvent) -> None:
    # 注解必须写成具体事件类型：EventBuilder 的规则已经过滤过，
    # 但 NoneBot 的参数解析器无法识别 typing.Any，会直接在导入期报错
    bind_loop()
    if str(event.user_id) == str(event.self_id):
        return
    group = store.get_group(event.group_id)
    if not (group.get("extra") or {}).get("notice", True):
        return
    template = str(group.get("welcome") or "").strip()
    if not template:
        return
    try:
        await send_group_msg(
            bot,
            event.group_id,
            _render(template, user_id=event.user_id, group_id=event.group_id, self_id=event.self_id),
        )
    except Exception as e:
        logger.warning(f"guanli 入群欢迎发送失败：{e!r}")


@_decrease.handle()
async def _on_group_decrease(bot: Bot, event: GroupDecreaseNoticeEvent) -> None:
    bind_loop()
    group = store.get_group(event.group_id)
    if not (group.get("extra") or {}).get("notice", True):
        return
    template = str(group.get("leave_notice") or "").strip()
    if not template:
        return
    try:
        await send_group_msg(
            bot,
            event.group_id,
            _render(template, user_id=event.user_id, group_id=event.group_id, self_id=event.self_id),
        )
    except Exception as e:
        logger.warning(f"guanli 退群提醒发送失败：{e!r}")


# ------------------------------------------------------------------ 群消息总入口
_watch = on_message(priority=1, block=False)


@_watch.handle()
async def _message_hook(bot: Bot, event: MessageEvent) -> None:
    """群消息总入口：登记事件循环 → 发言统计 → 黑名单 → 自动回复 → 违禁词

    - 群管理员、群主、超级用户豁免惩戒
    - 只有 ``群设置 → 扩展开关`` 里开着的项才会执行
    """
    bind_loop()
    if not isinstance(event, GroupMessageEvent):
        return

    group = store.get_group(event.group_id)
    extra = group.get("extra") or {}
    cfg = get_config()
    group_id, user_id = event.group_id, event.user_id
    text = event.get_plaintext() or ""

    is_staff = event.sender.role in ("admin", "owner") or _is_super(user_id)

    # 1) 发言统计（纯本地）
    if extra.get("stat", True) and text:
        try:
            store.add_message(group_id, user_id)
        except Exception as e:
            logger.debug(f"guanli 发言统计失败：{e!r}")

    black, white = _name_sets(group_id)

    # 2) 黑名单：撤回 + 累计违规，达阈值禁言
    if str(user_id) in black:
        try:
            await delete_msg(bot, event.message_id)
        except Exception:
            pass
        count = store.add_warn(group_id, user_id)
        limit = max(1, cfg.anti_warn_limit)
        if count >= limit:
            store.clear_warn(group_id, user_id)
            try:
                await set_group_ban(bot, group_id, user_id, cfg.anti_mute_duration)
                await _watch.finish(f"黑名单成员 {user_id} 消息已撤回并禁言 {cfg.anti_mute_duration} 秒。")
            except Exception as e:
                await _watch.finish(f"黑名单成员 {user_id} 消息已撤回（禁言失败：{e}）")
            return
        await _watch.finish(f"黑名单成员 {user_id} 的消息已撤回（{count}/{limit}）。")
        return

    # 3) 自动回复
    if extra.get("autoreply", True) and text.strip():
        rule = store.match_autoreply(group_id, text.strip())
        if rule:
            try:
                await bot.send(event, str(rule.get("reply") or ""))
            except Exception as e:
                logger.warning(f"guanli 自动回复发送失败：{e!r}")
            return

    # 4) 违禁词监测
    if is_staff or not group.get("watch_image") or str(user_id) in white:
        return
    words = [w for w in list(cfg.anti_keywords) + list(group.get("keywords") or []) if w]
    if not words:
        return
    hit = next((w for w in words if w in text), None)
    if not hit:
        return

    if extra.get("anti_recall", True):
        try:
            await delete_msg(bot, event.message_id)
        except Exception:
            pass

    count = store.add_warn(group_id, user_id)
    if count >= max(1, cfg.anti_warn_limit):
        store.clear_warn(group_id, user_id)
        try:
            await set_group_ban(bot, group_id, user_id, cfg.anti_mute_duration)
            await _watch.finish(f"检测到违禁词「{hit}」，违规累计 {count} 次，已禁言 {cfg.anti_mute_duration} 秒。")
        except Exception as e:
            await _watch.finish(f"检测到违禁词「{hit}」，违规 {count} 次（禁言失败：{e}）")
        return

    await _watch.finish(f"检测到违禁词「{hit}」，警告 {count}/{cfg.anti_warn_limit} 次。")
