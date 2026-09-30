"""兽频道菜单：ftv 指令汇总为一个 markdown 按钮菜单。

- goqbot：markdown 标题 + 按钮键盘，每行 3 个、共 15 个（14 个常用指令 + 「帮助」）；
- 指令按钮（action.type=2，enter=False）：点击只把指令填入输入框、不自动发送，
  参数由用户补全后自行发出；
- 「帮助」按钮为回调按钮：点它才回复完整指令用法（含未上按钮的低频指令）；
- 其他平台：直接回纯文本指令用法。

回调按钮（action.type=1）需要 goqbot 打开 InteractionHandler：点击后会推送一条
文本为 button_data 的普通 message，因此这里用 on_message 规则直接接管该文本。
"""
from typing import List, Tuple

from nonebot import on_message
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import MessageEvent

from util import cmdbuild, finish_text, is_goqbot, markdown_message

__all__ = ["menu_cmd"]

_PREFIX = "ftvmenu"  # 帮助按钮回调文本：ftvmenu:help
_PER_ROW = 3  # 每行按钮数
_HELP_LABEL = "帮助"
_MENU_TITLE = "CYX-bot 兽频道菜单"
_MENU_FOOTER = "若本群未开启全量信息请一定@机器人使用！"

# (按钮文字, 填入输入框的指令, 用法说明)；指令以 / 开头，尾巴留空格便于补参数
_MENU_BUTTONS: List[Tuple[str, str, str]] = [
    ("热门", "/热门", "获取热门推荐档案"),
    ("随机", "/随机", "获取随机推荐档案"),
    ("物种", "/物种 ", "按物种搜索档案，如：/物种 狼"),
    ("兽搜索", "/兽搜索 ", "关键词搜索档案，如：/兽搜索 小明"),
    ("兽档案", "/兽档案 ", "查看档案资料，如：/兽档案 小明"),
    ("崽崽", "/崽崽 ", "查看档案崽崽列表，如：/崽崽 小明"),
    ("学校", "/学校 ", "按学校搜索档案，如：/学校 北京大学"),
    ("聚会统计", "/聚会统计", "查看年度聚会统计"),
    ("本月聚会", "/本月聚会", "查看本月聚会列表，点数字看完整详情"),
    ("聚会详情", "/聚会详情 ", "查看聚会完整信息，需补聚会 ID"),
    ("热门地区", "/热门地区", "热门地区统计"),
    ("物种列表", "/物种列表", "物种统计（加 all 可一次全发）"),
    ("动态流", "/动态流", "时间线动态（可补日期）"),
]

# 未上按钮的低频指令，只在「帮助」里列出
_EXTRA_ITEMS: List[Tuple[str, str]] = [
    ("动态详情", "查看单条动态，需补动态 ID"),
    ("话题动态", "话题下的动态，如：/话题动态 #兽聚"),
    ("聚会动态", "聚会的动态，需补聚会 ID"),
    ("用户动态", "用户动态时间线，需补用户名"),
    ("今日发布", "某人今天发布的动态，需补用户名"),
    ("搜索建议", "联想搜索词，如：/搜索建议 兽"),
    ("点赞状态", "查看某用户点赞情况，需补用户名"),
    ("平台状态", "检查平台状态"),
    ("清除缓存", "清除查询缓存"),
]

menu_cmd = cmdbuild("ftv.menu", "兽频道菜单").priority(5).block().build()


def _help_text() -> str:
    """完整指令用法（帮助按钮 / 非 goqbot 平台共用）。"""
    lines = ["CYX-bot 兽频道菜单 · 指令一览", ""]
    idx = 0
    for _, cmd, usage in _MENU_BUTTONS:
        idx += 1
        lines.append(f"{idx}. {cmd.strip()} — {usage}")
    for name, usage in _EXTRA_ITEMS:
        idx += 1
        lines.append(f"{idx}. /{name} — {usage}")
    idx += 1
    lines.append(f"{idx}. /兽频道帮助 — 查看帮助图片")
    return "\n".join(lines)


def _help_markdown() -> str:
    """帮助内容的 markdown 版本：正文放在 text 代码块里。"""
    body = _help_text().split("\n", 1)[1].lstrip("\n")
    return f"**CYX-bot 兽频道菜单 · 指令一览**\n\n```text\n{body}\n```"


def _cmd_button(label: str, command: str) -> dict:
    """指令按钮：点击后把指令填入输入框（不自动发送），参数由用户补全。"""
    return {
        "id": command.strip() or label,
        "render_data": {"label": label, "visited_label": label, "style": 1},
        "action": {
            "type": 2,  # 指令按钮：自动在输入框插入 @bot + data
            "permission": {"type": 2},  # 所有人可操作
            "data": command,
            "enter": False,  # 仅填入输入框，不自动发送
            "reply": False,
            "unsupport_tips": "当前QQ版本不支持按钮，请升级后重试",
        },
    }


def _help_button() -> dict:
    """回调按钮：点击后回复完整指令用法。"""
    return {
        "id": f"{_PREFIX}:help",
        "render_data": {"label": _HELP_LABEL, "visited_label": _HELP_LABEL, "style": 0},
        "action": {
            "type": 1,  # 回调按钮：点击上报 INTERACTION_CREATE
            "permission": {"type": 2},
            "data": f"{_PREFIX}:help",
            "unsupport_tips": "当前QQ版本不支持按钮，请升级后重试",
        },
    }


def _build_menu_keyboard() -> dict:
    buttons = [_cmd_button(label, cmd) for label, cmd, _ in _MENU_BUTTONS]
    buttons.append(_help_button())
    rows = [
        {"buttons": buttons[start : start + _PER_ROW]}
        for start in range(0, len(buttons), _PER_ROW)
    ]
    return {"content": {"rows": rows}}


@menu_cmd.handle()
async def _(bot: Bot, event: MessageEvent):
    if not await is_goqbot(bot):
        return await finish_text(menu_cmd, bot, _help_text())
    content = f"**{_MENU_TITLE}**\n\n{_MENU_FOOTER}"
    await menu_cmd.finish(markdown_message(content, _build_menu_keyboard()))


def _menu_payload_rule(event: MessageEvent) -> bool:
    return event.get_plaintext().strip().startswith(f"{_PREFIX}:")


ftv_menu_pick = on_message(rule=_menu_payload_rule, priority=1, block=True)


@ftv_menu_pick.handle()
async def _(bot: Bot, event: MessageEvent):
    await finish_text(ftv_menu_pick, bot, _help_text(), _help_markdown())
