"""每日鉴毛 (Furry Will / Furry Gallery) 命令

对接 https://fur-bot.com/zh/blog-detail.html?post=mrjm 文档中的 API。
支持：随机抽卡、期数查询、名称搜索、地区搜索、工作室搜索、健康检查。

用法示例：
    每日鉴毛           随机抽一张
    每日鉴毛 600       查询第 600 期(随机一张)
    每日鉴毛 佩里斯    按名称搜索(随机一张)
    每日鉴毛 城市 北京  按地区搜索
    每日鉴毛 工作室 天邪鬼工作室
    每日鉴毛 状态      检查 API 健康状态
"""

import asyncio
import io
import re
import time
from typing import Optional, Tuple

import aiohttp
import jwt
from nonebot import on_command
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg
from nonebot.typing import T_State
from PIL import Image

from util import host_bytes, image_markdown, is_goqbot, markdown_message, md_display_size

from ..config.furry_will import (
    FURRY_WILL_BASE_URL,
    FURRY_WILL_JWT_QQ,
    FURRY_WILL_JWT_SECRET,
)

API_PREFIX = "/furry_will"

# 官方《Markdown 格式说明》：markdown 里的图片必须写成 ``![文本 #宽px #高px](url)``，
# 漏掉尺寸就只渲染成一个 ``[文本]`` 方块（线上实测已确认）。
# 回填的尺寸走 ``util.md_display_size()``：只按官方建议（宽 ≤720px、高 ≤1080px）
# 把**显示尺寸**等比压小，图床上依旧放原图 —— 不重新编码、不掉画质。

# 命令注册：优先使用官方主命令
furry_will = on_command(
    "每日鉴毛",
    aliases={"鉴毛", "furrywill", "fw"},
    priority=5,
    block=True,
)


class ApiError(Exception):
    """业务/系统错误，message 可直接展示给用户"""


def generate_jwt(qq: str, secret_key: str) -> str:
    """按文档生成 HS256 JWT，token 有效期 120 秒，须每次新生成"""
    payload = {
        "qq": qq,
        "timestamp": int(time.time()),
    }
    return jwt.encode(payload, secret_key, algorithm="HS256")


def _has_config() -> bool:
    return bool(FURRY_WILL_BASE_URL and FURRY_WILL_JWT_SECRET and FURRY_WILL_JWT_QQ)


# ── 卡片排版 ──────────────────────────────────────────────────────────
# QQ 官方 markdown 能用的样式就那么多（官方《Markdown 格式说明》《Markdown 消息》）：
# 一级/多级标题、**加粗**、_斜体_、~~删除线~~、有序/无序列表、块引用（>，只支持一级，
# 不支持 >>）、图片、分割线。**没有字号、没有颜色、也没有高亮**（官方 FAQ 原话：
# 「不支持 html 标签比如 <font>，故不支持更改字体颜色」「不支持代码块及高亮」）。
#
# 现在这张卡的排布（图片由调用方拼在最前面）：
#
#     # **篝橙**                ← 名字：一级标题 + 加粗
#     > 【第1644期】             ← 期号：一级引用（一层灰底）
#     >> 地区：包头              ← 三项信息：二级引用（再嵌一层）
#     >> 种族：龙
#     >> 工作室：可乐酱
#     _--每日鉴毛by狼神米迩_      ← 底部署名，末尾接出品方
#
# 想换样式改下面几个常量就够，不用动排版逻辑。
CARD_NAME = "# **{name}**"            # 名字：一级标题 + 加粗
CARD_QISHU = "> 【第{qishu}期】"       # 期号：一级引用（一层灰底）
# 三项信息：二级引用（再嵌一层）。官方文档在「块引用」一节写过「不支持 >> 二级引用」，
# 线上若把 >> 原样显示出来，把这行换成 CARD_QUOTE = "> " 即可退回一级引用，其余不用动。
CARD_QUOTE = ">> "
CARD_FOOTER = "{producer}"  # 底部署名，末位接出品方（producer）

# 正文里展示的字段，顺序即显示顺序；字段缺了就整行不显示；标签不带 emoji
CARD_FIELDS = (
    ("地区", "city"),
    ("种族", "race"),
    ("工作室", "studio"),
)


def _card_name(card: dict) -> str:
    """名字（一级标题 + 加粗），没名字就返回空串"""
    name = (card.get("name") or "").strip()
    return CARD_NAME.format(name=name) if name else ""


def _card_footer(producer: str = "") -> str:
    """底部署名：``--每日鉴毛by`` + 出品方（没有出品方就只留前半截）"""
    return CARD_FOOTER.format(producer=(producer or "").strip())


def _card_fields(card: dict) -> list:
    """要展示的字段，返回``[(标签, 值)]``，空值整条略过"""
    return [
        (label, card.get(key))
        for label, key in CARD_FIELDS
        if card.get(key)
    ]


def _card_quote(card: dict) -> list:
    """引用区块里的行：期号一级引用，三项信息二级引用

    期号和三项信息**同属一个引用块**（行与行之间不空行），只是引用层级不同，
    所以在 markdown 里是紧挨着的 ``> `` 和 ``>> `` 行。
    """
    lines = []
    qishu = card.get("qishu")
    if qishu:
        lines.append(CARD_QISHU.format(qishu=qishu))
    lines += [
        f"{CARD_QUOTE}{label}：{value}"
        for label, value in _card_fields(card)
    ]
    return lines


def _card_blocks(card: dict, producer: str = "") -> list:
    """卡片正文的主体「块」：名字 / 引用区 / 署名，各自成块

    块与块之间在 markdown 里必须空一行，理由见 ``build_card_markdown``。
    """
    blocks = [b for b in (_card_name(card),) if b]
    quoted = _card_quote(card)
    if quoted:
        blocks.append("\n".join(quoted))
    blocks.append(f"_{_card_footer(producer)}_")
    return blocks


def build_card_text(card: dict, producer: str = "") -> str:
    """纯文本版卡片正文（非 goqbot 回落时发，不能带任何 markdown 标记）

    与 markdown 版同一套排版，只是去掉标题 / 加粗 / 引用 / 斜体这些标记：
    名字一行、期数一行、字段各一行、署名一行。
    """
    lines = []
    name = (card.get("name") or "").strip()
    if name:
        lines.append(name)
    qishu = card.get("qishu")
    if qishu:
        lines.append(f"【第{qishu}期】")
    for label, value in _card_fields(card):
        lines.append(f"{label}：{value}")
    lines.append(_card_footer(producer))
    return "\n".join(lines)


def build_card_markdown(card: dict, producer: str = "") -> str:
    """markdown 版卡片正文（图片由调用方用 ``image_markdown`` 拼在最前面）

    成品长这样::

        # **篝橙**

        > 【第1644期】
        >> 地区：包头
        >> 种族：龙
        >> 工作室：可乐酱

        _--每日鉴毛by狼神米迩_

    期号和三项信息同属一个引用块，中间**不能空行** —— 一空行就会被拆成两个引用块，
    二级引用也就没了宿主。块与块之间反过来**必须空一行**，官方写得很死：引用「前面是
    普通文本，则需要用换行隔开，否则无法识别」「后面需要跟其他文本字段，则需要在结尾处
    增加一行换行以区分不同段落」。少了这个空行，引用会跟上一行粘成一句、署名会被并进
    引用里。
    """
    return "\n\n".join(_card_blocks(card, producer))


async def _request(session: aiohttp.ClientSession, path: str, params=None) -> dict:
    """发起带认证的 POST，统一处理 HTTP 非 200 与业务错误"""
    token = generate_jwt(FURRY_WILL_JWT_QQ, FURRY_WILL_JWT_SECRET)
    url = f"{FURRY_WILL_BASE_URL}{path}"
    async with session.post(
        url,
        params=params,
        json={"qq": FURRY_WILL_JWT_QQ, "token": token},
    ) as resp:
        # 图片端点之外的所有接口错误时 body 仍是 JSON
        body = await resp.json(content_type=None)
    if resp.status != 200:
        raise ApiError(
            f"[HTTP {resp.status}] {body.get('message', '访问受限/服务异常')}"
        )
    if body.get("status") != "success":
        raise ApiError(body.get("message", "业务错误"))
    return body


def _probe_size(data: bytes) -> Optional[Tuple[int, int]]:
    """读图片真实宽高；格式不支持或内容损坏时返回 None"""
    try:
        with Image.open(io.BytesIO(data)) as img:
            return int(img.width), int(img.height)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"每日鉴毛：读取图片尺寸失败，按原图处理 | {e}")
        return None


async def _publish_image(image_url: str) -> Optional[Tuple[str, int, int]]:
    """把源站图片**原样**转存到本机图床，返回 (公网 URL, 宽, 高)；失败返回 None

    源站链接带签名、几分钟就过期、还可能有防盗链，直接交给 QQ 不保险；
    转存到我们自己的图床后 URL 干净、无鉴权、长期有效，跟 awmc 的表情包是同一条路。

    这里刻意**不压缩、不重编码**：显示多大由 markdown 里的 ``#宽px #高px`` 决定
    （见 ``util.md_display_size``），图床上留原图，画质不受影响。

    图床落盘时带 ``persistent=True``：每日鉴毛走 ``keep/`` 目录**永久保留**，
    不参与 24 小时临时图回收 —— 这些卡片是「作品图」，用户翻旧消息还得看得见
    （ftv 的档案/统计图这种随时能重画的才进 ``tmp/`` 等回收）。

    宽高读不出来时按 ``0, 0`` 返回（调用方据此回落普通消息，不发 markdown 图）。
    """
    if not image_url:
        return None
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            async with session.get(image_url) as resp:
                if resp.status != 200:
                    logger.warning(
                        f"每日鉴毛：下载源图失败 HTTP {resp.status} | {image_url}"
                    )
                    return None
                data = await resp.read()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"每日鉴毛：下载源图出错 {e} | {image_url}")
        return None
    if not data:
        logger.warning(f"每日鉴毛：源图内容为空 | {image_url}")
        return None

    size = _probe_size(data)
    width, height = size if size else (0, 0)

    url = host_bytes(data, persistent=True)  # 后缀按文件头自动判断；持久图进 keep/
    if not url:
        logger.warning("每日鉴毛：图床不可用，本次回落为普通消息 + 源站链接")
        return None
    return url, width, height


async def _send_card(bot: Bot, body: dict) -> None:
    """把 data 中的卡片（取第一条）带图片发给用户，图和文落在同一条消息里。

    **markdown 里的图片必须带尺寸**：官方《Markdown 格式说明》要求写成
    ``![文本 #宽px #高px](url)``（宽高为实际像素），漏掉尺寸客户端只画一个
    ``[文本]`` 方块 —— 线上实测 A（带尺寸）出图、B（不带）不出，就是这么回事。
    尺寸这关过了之后，图床那个「IP + 非标准端口」地址平台照样能下载转存
    （官方原话：「请使用可在公网访问的资源 url，开放平台会下载转存该资源」），
    所以 markdown 这条路是通的，不需要去报备域名。

    **回落顺序**（任一步失败都不会让用户什么都看不到）：

        源图 → 原样转存本机图床 → goqbot 用 markdown「图 + 文」
                                → 否则普通消息「纯文本 + 图片 URL」
                                → 再不行退回纯文本 + 源站链接

    markdown 回填的是 ``md_display_size()`` 算出的**显示尺寸**（官方建议宽 ≤720px、
    高 ≤1080px），图床上是原图：只把 QQ 里的显示大小压下来，不重新编码。
    """
    data = body.get("data") or []
    if isinstance(data, dict):
        data = [data]
    if not data:
        raise ApiError("没有获取到卡片数据，请换关键词再试")

    producer = body.get("producer", "")
    card = data[0]
    text = build_card_text(card, producer)
    image_url = (card.get("url") or "").strip()

    # 先转存到自己的图床（源站链接会过期），转存不了就退回源站链接
    published = await _publish_image(image_url) if image_url else None
    if published:
        hosted, width, height = published
    else:
        hosted, width, height = "", 0, 0
    final_url = hosted or image_url

    # 首选 markdown：图 + 文一条发出（尺寸齐了才发，否则又会退化成方块）
    if final_url and width and height and await is_goqbot(bot):
        disp_w, disp_h = md_display_size(width, height)
        content = image_markdown(
            final_url, card.get("name") or "每日鉴毛", disp_w, disp_h
        )
        md = build_card_markdown(card, producer)
        if md:
            content = f"{content}\n\n{md}"
        await furry_will.send(markdown_message(content))
        return

    # 回落：普通消息「纯文本 + 图片 URL」，图片段走富媒体上传通道，不吃尺寸
    if final_url:
        segs = []
        if text:
            segs.append(MessageSegment.text(f"{text}\n"))
        segs.append(MessageSegment.image(final_url))
        await furry_will.send(Message(segs))
        return

    await furry_will.send(text or "没有获取到卡片数据，请换关键词再试")


@furry_will.handle()
async def handle_first_receive(
    bot: Bot, event: MessageEvent, state: T_State, cmd_arg=CommandArg()
):
    args = cmd_arg.extract_plain_text().strip() if cmd_arg else ""
    raw = args or ""

    # 帮助
    if raw.lower() in ("help", "帮助", "?", "？"):
        help_text = (
            "每日鉴毛 用法：\n"
            "  每日鉴毛            随机抽卡\n"
            "  每日鉴毛 600        查询第600期\n"
            "  每日鉴毛 佩里斯      按名称搜索\n"
            "  每日鉴毛 城市 北京   按地区搜索\n"
            "  每日鉴毛 工作室 XX   按工作室搜索\n"
            "  每日鉴毛 状态        检查API健康状态"
        )
        await furry_will.finish(help_text)

    # 健康检查（无需认证）
    if raw.lower() in ("状态", "状态查询", "health"):
        await _send_health()
        return

    if not _has_config():
        await furry_will.finish(
            "尚未配置每日鉴毛 API 凭证，请先填写 config.yaml 中 "
            "furry_will 的 base_url / jwt_secret / jwt_qq"
        )

    # 解析操作类型与参数
    action, keyword = _parse_args(raw)
    if action is None:
        await furry_will.finish(
            "无法识别的参数，发送“每日鉴毛 帮助”查看用法"
        )

    logger.info(f"每日鉴毛：action={action} keyword={keyword}")
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as session:
            if action == "random":
                body = await _request(session, f"{API_PREFIX}/random")
            elif action == "qishu":
                body = await _request(
                    session, f"{API_PREFIX}/qishu",
                    params={"qishu": keyword, "all": "0"},
                )
            elif action == "name":
                body = await _request(
                    session, f"{API_PREFIX}/name",
                    params={"name": keyword, "all": "0"},
                )
            elif action == "city":
                body = await _request(
                    session, f"{API_PREFIX}/city",
                    params={"city": keyword, "model": "random", "count": "1"},
                )
            elif action == "studio":
                body = await _request(
                    session, f"{API_PREFIX}/studio",
                    params={"studio": keyword, "model": "random", "count": "1"},
                )
            else:  # pragma: no cover
                await furry_will.finish("未知操作")
        await _send_card(bot, body)
    except ApiError as e:
        await furry_will.finish(str(e))
    except aiohttp.ClientConnectorError:
        logger.exception("每日鉴毛网络连接失败")
        await furry_will.finish("网络连接失败，请稍后再试")
    except asyncio.TimeoutError:
        logger.exception("每日鉴毛请求超时")
        await furry_will.finish("请求超时，请稍后再试")
    except Exception as e:  # noqa: BLE001
        logger.exception("每日鉴毛请求出错")
        await furry_will.finish(f"获取数据失败：{e}")


async def _send_health() -> None:
    url = f"{FURRY_WILL_BASE_URL}/health"
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            async with session.get(url) as resp:
                status = resp.status
                body = await resp.json(content_type=None)
        if status == 200 and body.get("status") == "ok":
            await furry_will.finish(
                "每日鉴毛 API 状态：正常\n"
                f"数据库：{body.get('db')}\n"
                f"Redis：{body.get('redis')}"
            )
        await furry_will.finish(
            f"每日鉴毛 API 异常（HTTP {status}）："
            f"{body.get('status')} db={body.get('db')} redis={body.get('redis')}"
        )
    except aiohttp.ClientError as e:
        logger.exception("每日鉴毛健康检查失败")
        await furry_will.finish(f"健康检查失败：无法连接 {FURRY_WILL_BASE_URL}（{e}）")


def _parse_args(raw: str):
    """返回 (action, keyword)。无法识别时返回 (None, None)"""
    if not raw:
        return "random", ""
    if raw.isdigit():
        return "qishu", raw

    m = re.match(r"^(?:第)?\s*(\d+)\s*期$", raw)
    if m:
        return "qishu", m.group(1)

    m = re.match(r"^(城市|地区)\s*[:：]?\s*(.+?)\s*$", raw)
    if m:
        return "city", m.group(2).strip()

    m = re.match(r"^工作室\s*[:：]?\s*(.+?)\s*$", raw)
    if m:
        return "studio", m.group(2).strip()

    # 其他文字一律视为名称搜索
    return "name", raw
