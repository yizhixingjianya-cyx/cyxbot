"""
goqbot / 标准 OneBot 平台能力抽象层

目标：把 QQ 官方桥接端 goqbot 的「非标 OneBot V11」动作，
与标准 OneBot（go-cqhttp / llbot 等）里同作用的功能统一封装成函数。

规则：
- 先判断传入的 bot 是否为 goqbot 实例（get_version_info 的 app_name == "goqbot"，带缓存）；
- goqbot 实例按 huilongxiji.github.io/gd-goqbot-docs 文档走专有/别名 action；
- 发送函数额外支持 markdown 参数：goqbot 时以 type=markdown 扩展段发送富文本，
  非 goqbot（标准端）自动回落纯文本 message；
- 非 goqbot（标准 OneBot V11）按 go-cqhttp 对齐的标准 action 名与参数；
- 某能力在标准端没有等价实现时抛 PlatformNotSupportedError，由调用方决定是否跳过。

只提供函数/工具类，不注册任何命令。
"""

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from nonebot.exception import FinishedException
from nonebot.log import logger

if TYPE_CHECKING:
    from nonebot.adapters import Bot

__all__ = [
    # 检测
    "is_goqbot",
    # 基础信息 / openid
    "get_login_info",
    "get_bot_status",
    "get_version_info",
    "get_gateway",
    "get_openid",
    "get_user_openid",
    "get_group_openid",
    # 发送消息扩展
    "send_group_msg",
    "send_private_msg",
    "send_group_active_msg",
    "send_private_active_msg",
    "force_send_group_msg",
    "force_send_private_msg",
    "send_interaction_ack",
    "delete_msg",
    "get_image",
    "markdown_message",
    "md_or_plain_message",
    "finish_text",
    "send_text",
    # 群聊管理
    "get_group_info",
    "get_group_bot_state",
    "get_group_member_info",
    "get_group_system_msg",
    "get_group_join_requests",
    "set_group_add_request",
    "set_group_ban",
    "get_group_ban",
    "set_group_whole_ban",
    "get_c2c_msg_state",
    # 自定义菜单与指令面板
    "get_menu",
    "set_menu",
    "get_command_panels",
    "get_command_panel",
    "create_command_panel",
    "set_command_panel",
    "delete_command_panel",
    "set_command_panel_target",
]


class PlatformNotSupportedError(NotImplementedError):
    """当前平台（非 goqbot 的标准 OneBot）没有该专有能力的等价实现"""


_GOQBOT_CACHE: Dict[str, bool] = {}


async def _call(bot: "Bot", action: str, **params) -> Dict[str, Any]:
    """发起 OneBot 动作调用，返回成功后的 data"""
    data = await bot.call_api(action, **params)
    return data or {}


async def is_goqbot(bot: "Bot") -> bool:
    """判断 bot 是否为 goqbot（QQ 官方桥接）实例，结果按 self_id 缓存"""
    key = str(getattr(bot, "self_id", ""))
    if key in _GOQBOT_CACHE:
        return _GOQBOT_CACHE[key]
    result = False
    try:
        info = await _call(bot, "get_version_info")
        result = (info.get("app_name") or "").lower() == "goqbot"
    except Exception:
        result = False
    _GOQBOT_CACHE[key] = result
    return result


async def _need_goqbot(bot: "Bot", feature: str) -> None:
    """非 goqbot 平台若不存在标准等价功能则抛出提示"""
    if not await is_goqbot(bot):
        raise PlatformNotSupportedError(f"{feature} 仅在 goqbot 下提供，当前平台不支持")


# ============================================================
# 基础信息 / openid
# ============================================================


async def get_login_info(bot: "Bot") -> Dict[str, Any]:
    """获取登录机器人身份 {user_id, nickname}，两个平台均支持"""
    return await _call(bot, "get_login_info")


async def get_bot_status(bot: "Bot") -> Dict[str, Any]:
    """获取运行状态 {online, good}，两个平台均支持"""
    return await _call(bot, "get_status")


async def get_version_info(bot: "Bot") -> Dict[str, Any]:
    """获取版本信息 {app_name, app_version, protocol_version}"""
    return await _call(bot, "get_version_info")


async def get_gateway(bot: "Bot") -> Dict[str, Any]:
    """goqbot：查询 QQ /gateway/bot WS 接入点与建连频控"""
    await _need_goqbot(bot, "get_gateway")
    return await _call(bot, "get_gateway")


async def get_openid(
    bot: "Bot",
    *,
    user_id: Optional[int] = None,
    group_id: Optional[int] = None,
) -> Dict[str, Any]:
    """goqbot：数字 id → QQ openid 反查。

    data: {user_openid?, group_openid?}。user_id/group_id 至少给一个。
    """
    await _need_goqbot(bot, "get_openid")
    return await _call(bot, "get_openid", user_id=user_id, group_id=group_id)


async def get_user_openid(bot: "Bot", user_id: int) -> Optional[str]:
    """goqbot：快捷取用户 openid，未命中返回 None"""
    try:
        data = await get_openid(bot, user_id=user_id)
    except Exception:
        return None
    return data.get("user_openid")


async def get_group_openid(bot: "Bot", group_id: int) -> Optional[str]:
    """goqbot：快捷取群 openid，未命中返回 None"""
    try:
        data = await get_openid(bot, group_id=group_id)
    except Exception:
        return None
    return data.get("group_openid")


# ============================================================
# 发送消息扩展
# ============================================================


def markdown_message(markdown: str, keyboard: Optional[dict] = None) -> Any:
    """构建 goqbot 扩展 markdown 消息段（type=markdown，正文在 data.data.markdown.content）

    keyboard 传 QQ 键盘对象（形如 ``{"content": {"rows": [...]}}``）时随消息下发按钮。

    仅 goqbot 下有意义；非 goqbot 平台请传纯文本回落。
    """
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    inner: dict = {"markdown": {"content": markdown}}
    if keyboard:
        inner["keyboard"] = keyboard
    return Message(MessageSegment("markdown", data={"data": inner}))


async def md_or_plain_message(bot: "Bot", markdown: str, plain_text: Any) -> Any:
    """插件可直接用返回值 finish/send 的便捷工具：
    goqbot → Markdown 富文本消息；非 goqbot → 回落 plain_text 纯文本消息。
    """
    if await is_goqbot(bot):
        return markdown_message(markdown)
    from nonebot.adapters.onebot.v11 import Message

    return plain_text if isinstance(plain_text, Message) else Message(plain_text)


def _plain_message(plain: Any) -> Any:
    """把纯文本内容包装成 Message"""
    from nonebot.adapters.onebot.v11 import Message

    return plain if isinstance(plain, Message) else Message(plain)


async def send_text(bot: "Bot", event: Any, plain: str, markdown: str = "") -> Any:
    """发送一条文本消息（不判断客户端类型）：

    默认按 Markdown 富文本发送；协议端返回不支持时，自动回退为纯文本重发。
    发送失败会向上抛出异常，调用方自行决定是否忽略。
    """
    md = markdown or plain
    try:
        return await bot.send(event, markdown_message(md))
    except Exception as e:
        logger.warning(f"Markdown 消息发送失败，回退纯文本：{e}")
        return await bot.send(event, _plain_message(plain))


async def finish_text(cmd: Any, bot: "Bot", plain: str, markdown: str = "") -> Any:
    """纯文字返回的统一出口：默认按 Markdown 富文本发送（不判断客户端类型），
    协议端返回不支持时自动回退纯文本。

    cmd 传当前 matcher，函数内部调用它的 finish 结束本次处理；
    markdown 留空时按 plain 原样作为 Markdown 发送。
    """
    md = markdown or plain
    try:
        return await cmd.finish(markdown_message(md))
    except FinishedException:
        raise
    except Exception as e:
        logger.warning(f"Markdown 消息发送失败，回退纯文本：{e}")
        return await cmd.finish(_plain_message(plain))


async def _resolve_message(
    message: Any, markdown: Optional[str], is_goqbot_bot: bool
) -> Any:
    """goqbot 且传了 markdown 时改用 markdown 段，否则回落 message 纯文本"""
    if is_goqbot_bot and markdown is not None:
        return markdown_message(markdown)
    if message is None:
        raise ValueError("message 与 markdown 至少提供一个")
    return message


async def send_group_msg(
    bot: "Bot",
    group_id: int,
    message: Any = None,
    *,
    markdown: Optional[str] = None,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """发群消息。goqbot 默认被动优先；可 user_id(lazy 凭证)、markdown(富文本)"""
    is_gq = await is_goqbot(bot)
    params: Dict[str, Any] = {
        "group_id": group_id,
        "message": await _resolve_message(message, markdown, is_gq),
    }
    if is_gq and user_id is not None:
        params["user_id"] = user_id
    return await _call(bot, "send_group_msg", **params)


async def send_private_msg(
    bot: "Bot",
    user_id: int,
    message: Any = None,
    *,
    markdown: Optional[str] = None,
    is_wakeup: bool = False,
) -> Dict[str, Any]:
    """发单聊(C2C)。goqbot 下 is_wakeup=True 走互动召回(30 天内分周期限次)；支持 markdown"""
    is_gq = await is_goqbot(bot)
    params: Dict[str, Any] = {
        "user_id": user_id,
        "message": await _resolve_message(message, markdown, is_gq),
    }
    if is_gq and is_wakeup:
        params["is_wakeup"] = True
    return await _call(bot, "send_private_msg", **params)


async def send_group_active_msg(
    bot: "Bot", group_id: int, message: Any = None, *, markdown: Optional[str] = None
) -> Dict[str, Any]:
    """goqbot：强制主动发群(不套被动凭证)，支持 markdown"""
    await _need_goqbot(bot, "send_group_active_msg")
    return await _call(
        bot,
        "send_group_active_msg",
        group_id=group_id,
        message=await _resolve_message(message, markdown, True),
    )


async def send_private_active_msg(
    bot: "Bot", user_id: int, message: Any = None, *, markdown: Optional[str] = None
) -> Dict[str, Any]:
    """goqbot：强制主动发单聊(不含召回语义)，支持 markdown"""
    await _need_goqbot(bot, "send_private_active_msg")
    return await _call(
        bot,
        "send_private_active_msg",
        user_id=user_id,
        message=await _resolve_message(message, markdown, True),
    )


async def force_send_group_msg(
    bot: "Bot", group_id: int, message: Any
) -> Dict[str, Any]:
    """“主动发群”统一入口：goqbot 用 send_group_active_msg，其余用标准 send_group_msg"""
    if await is_goqbot(bot):
        return await _call(
            bot, "send_group_active_msg", group_id=group_id, message=message
        )
    return await _call(bot, "send_group_msg", group_id=group_id, message=message)


async def force_send_private_msg(
    bot: "Bot", user_id: int, message: Any
) -> Dict[str, Any]:
    """“主动发单聊”统一入口：goqbot 用 send_private_active_msg，其余用标准 send_private_msg"""
    if await is_goqbot(bot):
        return await _call(
            bot, "send_private_active_msg", user_id=user_id, message=message
        )
    return await _call(bot, "send_private_msg", user_id=user_id, message=message)


async def send_interaction_ack(
    bot: "Bot", interaction_id: str, code: int = 0
) -> Dict[str, Any]:
    """goqbot：互动按钮回执（框架通常已自动回 code=0，出错/无权限时覆盖即可）"""
    await _need_goqbot(bot, "put_interaction")
    return await _call(
        bot, "put_interaction", interaction_id=interaction_id, code=code
    )


# ============================================================
# 通用动作（消息管理等，两端 action 同名）
# ============================================================


async def delete_msg(bot: "Bot", message_id: int) -> Dict[str, Any]:
    """撤回消息。goqbot 仅能撤回其自身分配出的 message_id"""
    return await _call(bot, "delete_msg", message_id=message_id)


async def get_image(bot: "Bot", file: str) -> Dict[str, Any]:
    """按 file_id 取图片信息（url/file 等）。goqbot 未实现该 action 时返回空 data"""
    return await _call(bot, "get_image", file=file)


# ============================================================
# 群聊管理
# ============================================================


async def get_group_info(bot: "Bot", group_id: int) -> Dict[str, Any]:
    """查群信息（goqbot 需官方白名单）"""
    return await _call(bot, "get_group_info", group_id=group_id)


async def get_group_bot_state(bot: "Bot", group_id: int) -> Dict[str, Any]:
    """goqbot：查机器人在群状态（allow_proactive_msg/member_role 等）"""
    await _need_goqbot(bot, "get_group_bot_state")
    return await _call(bot, "get_group_bot_state", group_id=group_id)


async def get_group_member_info(
    bot: "Bot", group_id: int, user_id: int
) -> Dict[str, Any]:
    """查成员信息。goqbot 仅支持 user_id=self_id（走 bot_state），标准端支持任意成员"""
    return await _call(bot, "get_group_member_info", group_id=group_id, user_id=user_id)


async def get_group_system_msg(
    bot: "Bot", group_id: int
) -> Dict[str, Any]:
    """取入群申请。两个平台动作名一致但结构不同：
    - goqbot（get_group_join_requests）：data.list[] 含 join_request_id/flag/user_id 等
    - 标准 go-cqhttp：data.{invite_requests, join_requests}[]，flag 为申请 flag
    """
    return await _call(bot, "get_group_system_msg", group_id=group_id)


async def get_group_join_requests(
    bot: "Bot",
    group_id: int,
    *,
    cursor: Optional[str] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """goqbot：分页拉取入群申请列表（并把 join_request_id 写入 idmap 供审批）"""
    await _need_goqbot(bot, "get_group_join_requests")
    params: Dict[str, Any] = {"group_id": group_id}
    if cursor is not None:
        params["cursor"] = cursor
    if limit is not None:
        params["limit"] = limit
    return await _call(bot, "get_group_join_requests", **params)


async def set_group_add_request(
    bot: "Bot",
    flag: str,
    approve: bool = True,
    reason: Optional[str] = None,
    *,
    group_id: Optional[int] = None,
    user_id: Optional[int] = None,
    blacklist: bool = False,
    sub_type: str = "add",
) -> Dict[str, Any]:
    """审批入群申请。

    - goqbot：flag=官方 join_request_id；flag 无缓存时可带 group_id/user_id 兜底反查
    - 标准端（go-cqhttp）：还需 sub_type(add/invite)
    """
    if await is_goqbot(bot):
        params: Dict[str, Any] = {"flag": flag, "approve": approve}
        if reason:
            params["reason"] = reason
        if blacklist:
            params["blacklist"] = True
        if group_id is not None:
            params["group_id"] = group_id
        if user_id is not None:
            params["user_id"] = user_id
    else:
        params = {"flag": flag, "sub_type": sub_type, "approve": approve}
        if reason:
            params["reason"] = reason
    return await _call(bot, "set_group_add_request", **params)


async def set_group_ban(
    bot: "Bot", group_id: int, user_id: int, duration: int = 0
) -> Dict[str, Any]:
    """禁言/解禁单成员（duration>0 禁言 N 秒；<=0 解除）。两个平台 action 与参数一致"""
    return await _call(
        bot, "set_group_ban", group_id=group_id, user_id=user_id, duration=duration
    )


async def get_group_ban(bot: "Bot", group_id: int) -> Dict[str, Any]:
    """goqbot：查群禁言状态（global_mode + 当前被禁成员列表）。标准端无等价接口"""
    await _need_goqbot(bot, "get_group_ban")
    return await _call(bot, "get_group_ban", group_id=group_id)


async def set_group_whole_ban(
    bot: "Bot", group_id: int, enable: bool
) -> Dict[str, Any]:
    """全员禁言。goqbot 官方仅支持成员级禁言，一律不支持；
    标准 go-cqhttp 使用 set_group_whole_ban(group_id, enable)。"""
    if await is_goqbot(bot):
        raise PlatformNotSupportedError("goqbot(QQ群v2) 不支持全员禁言")
    return await _call(bot, "set_group_whole_ban", group_id=group_id, enable=enable)


async def get_c2c_msg_state(
    bot: "Bot",
    *,
    user_id: Optional[int] = None,
    user_openid: Optional[str] = None,
) -> Dict[str, Any]:
    """goqbot：查用户 C2C 主动推送开关（本地缓存，非官方接口）"""
    await _need_goqbot(bot, "get_c2c_msg_state")
    params: Dict[str, Any] = {}
    if user_id is not None:
        params["user_id"] = user_id
    if user_openid is not None:
        params["user_openid"] = user_openid
    return await _call(bot, "get_c2c_msg_state", **params)


# ============================================================
# 自定义菜单与指令面板（均仅 goqbot，其余平台无等价实现）
# ============================================================


async def get_menu(bot: "Bot") -> Dict[str, Any]:
    """goqbot：查全局自定义菜单（单聊底部）"""
    await _need_goqbot(bot, "get_menu")
    return await _call(bot, "get_menu")


async def set_menu(bot: "Bot", items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """goqbot：整体覆盖全局菜单。menu 结构见文档；约 5 QPM 频控"""
    await _need_goqbot(bot, "set_menu")
    return await _call(bot, "set_menu", menu={"items": items})


async def get_command_panels(
    bot: "Bot",
    scope: str,
    *,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """goqbot：按场景分页列指令面板（scope: c2c/group/channel/dm）"""
    await _need_goqbot(bot, "get_command_panels")
    params: Dict[str, Any] = {"scope": scope}
    if limit is not None:
        params["limit"] = limit
    if cursor is not None:
        params["cursor"] = cursor
    return await _call(bot, "get_command_panels", **params)


async def get_command_panel(bot: "Bot", panel_id: str) -> Dict[str, Any]:
    """goqbot：查单个指令面板详情"""
    await _need_goqbot(bot, "get_command_panel")
    return await _call(bot, "get_command_panel", panel_id=panel_id)


async def create_command_panel(
    bot: "Bot",
    scope: str,
    items: List[Dict[str, Any]],
    *,
    target_type: str = "all",
    user_openids: Optional[List[str]] = None,
    group_openids: Optional[List[str]] = None,
    remark: Optional[str] = None,
) -> Dict[str, Any]:
    """goqbot：创建指令面板。specific 场景传 QQ openid 而非数字 id"""
    await _need_goqbot(bot, "create_command_panel")
    panel: Dict[str, Any] = {"items": items}
    if remark is not None:
        panel["remark"] = remark
    params: Dict[str, Any] = {"scope": scope, "target_type": target_type, "panel": panel}
    if user_openids:
        params["user_openids"] = user_openids
    if group_openids:
        params["group_openids"] = group_openids
    return await _call(bot, "create_command_panel", **params)


async def set_command_panel(
    bot: "Bot",
    panel_id: str,
    items: List[Dict[str, Any]],
    *,
    remark: Optional[str] = None,
) -> Dict[str, Any]:
    """goqbot：整体覆盖已有面板内容（不改 scope / 关联对象）"""
    await _need_goqbot(bot, "set_command_panel")
    panel: Dict[str, Any] = {"items": items}
    if remark is not None:
        panel["remark"] = remark
    return await _call(bot, "set_command_panel", panel_id=panel_id, panel=panel)


async def delete_command_panel(bot: "Bot", panel_id: str) -> Dict[str, Any]:
    """goqbot：删除指令面板（不可恢复）"""
    await _need_goqbot(bot, "delete_command_panel")
    return await _call(bot, "delete_command_panel", panel_id=panel_id)


async def set_command_panel_target(
    bot: "Bot",
    panel_id: str,
    op: str,
    *,
    user_openids: Optional[List[str]] = None,
    group_openids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """goqbot：修改 specific 面板的关联对象（op: add/del）"""
    await _need_goqbot(bot, "set_command_panel_target")
    params: Dict[str, Any] = {"panel_id": panel_id, "op": op}
    if user_openids:
        params["user_openids"] = user_openids
    if group_openids:
        params["group_openids"] = group_openids
    return await _call(bot, "set_command_panel_target", **params)
