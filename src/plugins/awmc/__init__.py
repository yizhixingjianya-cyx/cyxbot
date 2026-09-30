import random
from pathlib import Path
from typing import Optional

import aiofiles
from nonebot import get_plugin_config
from nonebot.log import logger
from nonebot.plugin import PluginMetadata
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.params import CommandArg
from nonebot.adapters import Message

from util import cmdbuild, host_bytes
from util.env_value import text as env_text

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="随机表情包",
    description="发送一张随机表情包",
    usage="发送指令：随机表情包 / 来个表情包",
    type="application",
    homepage="https://github.com/nonebot/nonebot2",
    config=Config,
)

config = get_plugin_config(Config)

DEFAULT_MEME_FOLDER = Path(__file__).parent / "image"
MEME_FOLDER = Path(env_text("RANDOM_MEME_FOLDER", str(DEFAULT_MEME_FOLDER)))

SUPPORTED_FORMATS = (".jpg", ".jpeg", ".png", ".gif", ".webp")

send_random_meme = (
    cmdbuild("lezi.awmc", "玩舞萌玩的")
    .alias("玩乌蒙玩的", "awmc", "舞萌表情包")
    .priority(5)
    .block(True)
    .build()
)


async def _publish_meme(path: Path) -> Optional[str]:
    """把本地表情包上传到图床，返回公网 URL；图床不可用或失败返回 None。

    为什么不直接发本地文件：在 goqbot（QQ 官方机器人）下，``MessageSegment.image()``
    里放本地路径需要适配器自己再做一次富媒体上传（``msg_type=7``），适配器不做的话
    图片就发不出去、群里只剩空白。改成「本地图 -> 图床公网 URL -> 普通消息发 URL」
    之后，消息里带的是链接，适配器按链接取图即可，和 ftv 的档案图是同一条路。

    图片按内容 md5 命名，同图复用同一 URL，与 ftv 共用同一个静态目录。
    """
    try:
        async with aiofiles.open(path, "rb") as f:
            data = await f.read()
    except OSError as e:
        logger.warning(f"随机表情包：读取本地图片失败 {e} | {path}")
        return None
    if not data:
        logger.warning(f"随机表情包：本地图片内容为空 | {path}")
        return None

    url = host_bytes(data, path.suffix.lower())
    if not url:
        logger.warning(f"随机表情包：图床不可用，本次回落为直接发送本地文件 | {path}")
    return url


@send_random_meme.handle()
async def handle_random_meme(args: Message = CommandArg()):
    if not MEME_FOLDER.exists():
        MEME_FOLDER.mkdir(parents=True, exist_ok=True)
        await send_random_meme.finish("表情包文件夹不存在，已自动创建，请放入表情包后重试~")

    meme_files = []
    for file in MEME_FOLDER.iterdir():
        if file.is_file() and file.suffix.lower() in SUPPORTED_FORMATS:
            meme_files.append(file)

    if not meme_files:
        await send_random_meme.finish("当前没有表情包哦，请先放入图片到表情包文件夹~")

    random_meme = random.choice(meme_files)

    # 优先走图床：普通消息里带公网 URL（适配器直接按链接取图，不依赖本地文件上传）
    hosted = await _publish_meme(random_meme)
    if hosted:
        await send_random_meme.finish(MessageSegment.image(hosted))

    # 图床没配 / 写盘失败：回落为原来的直发本地文件，至少还能出图
    await send_random_meme.send(MessageSegment.image(random_meme))