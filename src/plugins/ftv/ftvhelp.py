from pathlib import Path
import os
import traceback
from PIL import Image, ImageDraw, ImageFont
from nonebot import on_command
from nonebot.plugin import PluginMetadata
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, MessageSegment
from nonebot.log import logger
from nonebot.exception import FinishedException

from util import finish_text
from util.font_manager import Fonts, fallback_draw


# 插件元数据（可根据实际需求修改）
__plugin_meta__ = PluginMetadata(
    name="help",
    description="生成帮助图片，包含标题、自定义内容和固定底部文字",
    usage="发送「帮助」或「help」指令获取帮助图片",
)

# 帮助命令（触发词不变）
help_cmd = on_command("兽频道帮助", aliases={"ftvhelp"}, priority=5, block=True)


def load_custom_font(font_type: str) -> ImageFont.ImageFont:
    """加载统一字体（覆盖生僻字/符号/emoji），失败时回退系统默认字体

    :param font_type: "title" 标题字体，"content" 内容字体
    """
    size = 36 if font_type == "title" else 24
    try:
        return Fonts.cjk(size)
    except Exception as e:
        logger.error(f"加载 {font_type} 字体失败：{str(e)}")
        return ImageFont.load_default()


def generate_help_image(help_content: str) -> str:
    """
    生成帮助图片（含标题、居中的内容、固定底部文字，页脚在最底下且居中）
    :param help_content: 从 helptext.txt 读取的帮助内容
    :return: 生成的图片路径
    """
    title_font = load_custom_font("title")  
    content_font = load_custom_font("content")  

    current_dir = os.path.dirname(os.path.abspath(__file__))  
    background_path = os.path.join(current_dir + "/help", "background.png")
    default_bg_path = os.path.join(current_dir, "default_background.png")

    if os.path.exists(background_path):
        try:
            background = Image.open(background_path).convert("RGBA")
            logger.info(f"加载自定义背景图：{background_path}")
        except Exception as e:
            logger.error(f"加载自定义背景图失败：{str(e)}，使用默认背景")
            background = Image.new("RGBA", (800, 1000), color=(245, 245, 245))
    else:
        if os.path.exists(default_bg_path):
            try:
                background = Image.open(default_bg_path).convert("RGBA")
                logger.info(f"加载默认背景图：{default_bg_path}")
            except Exception as e:
                logger.error(f"加载默认背景图失败：{str(e)}，创建纯色背景")
                background = Image.new("RGBA", (800, 1000), color=(245, 245, 245))
        else:
            logger.info("未找到背景图，创建纯色背景（宽800x高1000）")
            background = Image.new("RGBA", (800, 1000), color=(245, 245, 245))
            draw_temp = ImageDraw.Draw(background)
            draw_temp.rectangle([40, 40, 760, 960], outline=(100, 100, 100), width=2)
            try:
                background.save(default_bg_path)
                logger.info(f"默认背景图已保存：{default_bg_path}")
            except Exception as e:
                logger.warning(f"保存默认背景图失败：{str(e)}")

    fixed_title = "兽频道插件帮助"  
    fixed_footer = "—— furtv API from vds，plugin by starfure ——"  

    draw = fallback_draw(background)
    margin = 60  
    max_width = background.width - 2 * margin  

    title_bbox = draw.textbbox((0, 0), fixed_title, font=title_font)
    title_width = title_bbox[2] - title_bbox[0]
    title_x = (background.width - title_width) // 2  
    title_y = 20  
    draw.text((title_x, title_y), fixed_title, font=title_font, fill=(255, 50, 50), align="center")

    content_top_y = title_y + (title_bbox[3] - title_bbox[1]) + 30
    wrapped_content = []
    for paragraph in help_content.split('\n'):
        if not paragraph.strip():
            wrapped_content.append('')
            continue
        words = paragraph.split(' ')
        current_line = words[0]
        for word in words[1:]:
            test_line = f"{current_line} {word}"
            test_bbox = draw.textbbox((0, 0), test_line, font=content_font)
            test_width = test_bbox[2] - test_bbox[0]
            if test_width <= max_width:
                current_line = test_line
            else:
                wrapped_content.append(current_line)
                current_line = word
        wrapped_content.append(current_line)
    wrapped_content_str = '\n'.join(wrapped_content)
    content_bbox = draw.multiline_textbbox((0, 0), wrapped_content_str, font=content_font)
    content_width = content_bbox[2] - content_bbox[0]
    content_x = (background.width - content_width) // 2  # 计算内容水平居中的X坐标
    # 绘制内容（水平居中，顶部为 content_top_y）
    draw.multiline_text(
        (content_x, content_top_y),
        wrapped_content_str,
        font=content_font,
        fill=(30, 30, 30),
        align="left",  # 行内文字左对齐，整体块居中
        spacing=8
    )

    # 计算页脚垂直位置：基于背景图高度，距离底部20像素
    footer_margin_bottom = 20
    footer_bbox = draw.textbbox((0, 0), fixed_footer, font=content_font)
    footer_height = footer_bbox[3] - footer_bbox[1]
    footer_y = background.height - footer_height - footer_margin_bottom
    footer_width = footer_bbox[2] - footer_bbox[0]
    footer_x = (background.width - footer_width) // 2  
    draw.text((footer_x, footer_y), fixed_footer, font=content_font, fill=(100, 100, 100), align="center")

    output_path = os.path.join(current_dir, "help_output.png")
    try:
        background.save(output_path)
        logger.info(f"帮助图片生成完成：{output_path}")
        return output_path
    except Exception as e:
        logger.error(f"保存帮助图片失败：{str(e)}")
        raise


@help_cmd.handle()
async def handle_help(bot: Bot, event: MessageEvent):
    try:
        logger.info("收到帮助指令，开始处理...")
        
        current_dir = os.path.dirname(os.path.abspath(__file__))
        help_text_path = os.path.join(current_dir + "/help", "helptext.txt")
        
        if not os.path.exists(help_text_path):
            raise FileNotFoundError(f"帮助内容文件不存在：{help_text_path}\n请在 command 文件夹下创建 helptext.txt 并填写帮助内容")
        
        with open(help_text_path, "r", encoding="utf-8") as f:
            help_content = f.read().strip()
        
        if not help_content:
            raise ValueError("helptext.txt 内容为空，请填写帮助内容后重试")
        
        image_path = generate_help_image(help_content)
        
        await help_cmd.finish(MessageSegment.image(file=image_path))
        
    except FinishedException:
        raise
    
    except Exception as e:
        error_msg = f"获取帮助失败：{str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        await finish_text(help_cmd, bot, error_msg)