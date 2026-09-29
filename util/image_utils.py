"""
图片处理工具模块
提供常用的图片处理功能，包括文字换行、字体查找、圆角处理、渐变背景等
"""

import os
import logging
from io import BytesIO
from typing import Tuple, Union, Optional, List
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

logger = logging.getLogger(__name__)


class DrawText:
    """文字绘制工具类"""

    def __init__(self, image: ImageDraw.ImageDraw, font: Union[str, Path]) -> None:
        self._img = image
        self._font = str(font)

    def get_box(self, text: str, size: int) -> Tuple[float, float, float, float]:
        """获取文字边界框"""
        return ImageFont.truetype(self._font, size).getbbox(text)

    def draw(
        self,
        pos_x: int,
        pos_y: int,
        size: int,
        text: Union[str, int, float],
        color: Tuple[int, int, int, int] = (255, 255, 255, 255),
        anchor: str = 'lt',
        stroke_width: int = 0,
        stroke_fill: Tuple[int, int, int, int] = (0, 0, 0, 0),
        multiline: bool = False
    ) -> None:
        """绘制文字"""
        font = ImageFont.truetype(self._font, size)
        if multiline:
            self._img.multiline_text(
                (pos_x, pos_y),
                str(text),
                color,
                font,
                anchor,
                stroke_width=stroke_width,
                stroke_fill=stroke_fill
            )
        else:
            self._img.text(
                (pos_x, pos_y),
                str(text),
                color,
                font,
                anchor,
                stroke_width=stroke_width,
                stroke_fill=stroke_fill
            )


def find_font_path(preferred_fonts: Optional[List[str]] = None) -> Optional[str]:
    """
    查找可用的字体文件

    Args:
        preferred_fonts: 优先查找的字体路径列表

    Returns:
        找到的字体路径，如果都没找到返回None
    """
    if preferred_fonts is None:
        preferred_fonts = []

    # 常用字体路径
    font_paths = preferred_fonts + [
        "C:/Windows/Fonts/simhei.ttf",       # Windows 黑体
        "C:/Windows/Fonts/msyh.ttc",         # Windows 微软雅黑
        "/System/Library/Fonts/PingFang.ttc",  # macOS
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",  # Linux
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",    # Linux 文泉驿正黑
    ]

    for font_path in font_paths:
        if os.path.exists(font_path):
            return font_path

    return None


def load_font_with_fallback(font_path: Optional[str] = None, size: int = 24) -> ImageFont.FreeTypeFont:
    """
    加载字体，失败时回退到默认字体

    Args:
        font_path: 字体文件路径，None则自动查找
        size: 字体大小

    Returns:
        加载的字体对象
    """
    if font_path is None:
        font_path = find_font_path()

    if font_path and os.path.exists(font_path):
        try:
            return ImageFont.truetype(font_path, size)
        except Exception as e:
            logger.warning(f"加载字体失败: {e}，使用默认字体")

    return ImageFont.load_default()


def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    """
    根据最大宽度对文本进行换行处理

    Args:
        text: 原始文本
        font: 字体对象
        max_width: 最大宽度

    Returns:
        换行后的文本
    """
    lines = []
    for paragraph in text.split('\n'):
        if not paragraph:
            lines.append('')
            continue

        words = paragraph.split(' ')
        line = words[0]
        for word in words[1:]:
            test_line = line + ' ' + word
            bbox = font.getbbox(test_line) if hasattr(font, 'getbbox') else font.getsize(test_line)
            line_width = bbox[2] - bbox[0] if len(bbox) == 4 else bbox[0]
            if line_width <= max_width:
                line = test_line
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return '\n'.join(lines)


def draw_wrapped_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    x: int,
    y: int,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    line_spacing: int = 5,
    max_lines: Optional[int] = None,
    fill: Union[str, Tuple[int, int, int]] = "#333333"
) -> int:
    """
    自动换行绘制文本，支持最大行数限制

    Args:
        draw: ImageDraw对象
        text: 要绘制的文本
        x: 起始x坐标
        y: 起始y坐标
        font: 字体对象
        max_width: 最大宽度
        line_spacing: 行间距
        max_lines: 最大行数，None表示无限制
        fill: 文字颜色

    Returns:
        绘制结束后的y坐标
    """
    words = text.split()
    current_line = ""
    current_y = y
    line_count = 0

    for word in words:
        if max_lines is not None and line_count >= max_lines:
            if current_line:
                if draw.textlength(f"{current_line}...", font=font) <= max_width:
                    current_line += "..."
                else:
                    current_line = current_line[:-3] + "..."
                draw.text((x, current_y), current_line, font=font, fill=fill)
                current_y += font.size + line_spacing
            return current_y

        test_line = f"{current_line} {word}".strip()
        if draw.textlength(test_line, font=font) <= max_width:
            current_line = test_line
        else:
            draw.text((x, current_y), current_line, font=font, fill=fill)
            current_y += font.size + line_spacing
            current_line = word
            line_count += 1

    if current_line and (max_lines is None or line_count < max_lines):
        draw.text((x, current_y), current_line, font=font, fill=fill)
        current_y += font.size + line_spacing

    return current_y


def tricolor_gradient(
    width: int,
    height: int,
    color1: Tuple[int, int, int] = (124, 129, 255),
    color2: Tuple[int, int, int] = (193, 247, 225),
    color3: Tuple[int, int, int] = (255, 255, 255)
) -> Image.Image:
    """
    绘制三色渐变色背景

    Args:
        width: 图片宽度
        height: 图片高度
        color1: 顶部颜色
        color2: 中间颜色
        color3: 底部颜色

    Returns:
        渐变背景图片
    """
    array = np.zeros((height, width, 3), dtype=np.uint8)

    for y in range(height):
        if y < height * 0.4:
            ratio = y / (height * 0.4)
            color = (1 - ratio) * np.array(color1) + ratio * np.array(color2)
        else:
            ratio = (y - height * 0.4) / (height * 0.6)
            color = (1 - ratio) * np.array(color2) + ratio * np.array(color3)
        array[y, :] = np.clip(color, 0, 255)

    image = Image.fromarray(array).convert('RGBA')
    return image


def rounded_corners(
    image: Image.Image,
    radius: int,
    corners: Tuple[bool, bool, bool, bool] = (True, True, True, True)
) -> Image.Image:
    """
    为图片添加圆角

    Args:
        image: 原始图片
        radius: 圆角半径
        corners: 四个角是否绘制圆角，分别是左上、右上、右下、左下

    Returns:
        带圆角的图片
    """
    mask = Image.new('L', image.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, image.size[0], image.size[1]), radius, fill=255, corners=corners)

    new_im = ImageOps.fit(image, mask.size)
    new_im.putalpha(mask)

    return new_im


def create_rounded_rectangle(
    size: Tuple[int, int],
    radius: int,
    fill: Union[Tuple[int, int, int], Tuple[int, int, int, int]],
    outline: Optional[Union[str, Tuple[int, int, int]]] = None,
    outline_width: int = 0
) -> Image.Image:
    """
    创建圆角矩形图片

    Args:
        size: (宽度, 高度)
        radius: 圆角半径
        fill: 填充颜色，支持RGBA
        outline: 边框颜色
        outline_width: 边框宽度

    Returns:
        圆角矩形图片
    """
    img = Image.new('RGBA', size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [(0, 0), size],
        radius=radius,
        fill=fill,
        outline=outline,
        width=outline_width
    )
    return img


def load_image_from_url(
    url: str,
    timeout: int = 5,
    retries: int = 2
) -> Optional[Image.Image]:
    """
    从URL加载图片

    Args:
        url: 图片URL
        timeout: 超时时间（秒）
        retries: 重试次数

    Returns:
        加载的图片，失败返回None
    """
    import requests

    for retry in range(retries + 1):
        try:
            response = requests.get(url, timeout=timeout)
            if response.status_code == 200:
                content_type = response.headers.get("Content-Type", "")
                if content_type.startswith("image/"):
                    return Image.open(BytesIO(response.content))
            logger.warning(f"加载图片失败，状态码: {response.status_code}")
        except requests.exceptions.Timeout:
            logger.warning(f"加载图片超时（第{retry+1}次重试）")
        except Exception as e:
            logger.warning(f"加载图片失败: {e}")
            break

    return None


async def async_load_image_from_url(
    url: str,
    timeout: int = 5,
    retries: int = 2
) -> Optional[Image.Image]:
    """
    异步从URL加载图片

    Args:
        url: 图片URL
        timeout: 超时时间（秒）
        retries: 重试次数

    Returns:
        加载的图片，失败返回None
    """
    try:
        import httpx
    except ImportError:
        logger.warning("httpx library not available, falling back to sync")
        return load_image_from_url(url, timeout, retries)

    for retry in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url)
                if response.status_code == 200:
                    content_type = response.headers.get("Content-Type", "")
                    if content_type.startswith("image/"):
                        return Image.open(BytesIO(response.content))
                logger.warning(f"异步加载图片失败，状态码: {response.status_code}")
        except httpx.TimeoutException:
            logger.warning(f"异步加载图片超时（第{retry+1}次重试）")
        except Exception as e:
            logger.warning(f"异步加载图片失败: {e}")
            break

    return None


def resize_image_with_ratio(
    image: Image.Image,
    target_size: Tuple[int, int],
    crop_center: bool = True
) -> Image.Image:
    """
    按比例缩放图片并裁剪到目标尺寸

    Args:
        image: 原始图片
        target_size: 目标尺寸 (宽, 高)
        crop_center: 是否从中心裁剪

    Returns:
        处理后的图片
    """
    target_width, target_height = target_size
    img_width, img_height = image.size

    # 计算缩放比例
    ratio = max(target_width / img_width, target_height / img_height)
    new_size = (int(img_width * ratio), int(img_height * ratio))

    # 缩放图片
    resized = image.resize(new_size, Image.Resampling.LANCZOS)

    if crop_center:
        # 从中心裁剪
        left = (new_size[0] - target_width) // 2
        top = (new_size[1] - target_height) // 2
        right = left + target_width
        bottom = top + target_height
        return resized.crop((left, top, right, bottom))

    return resized


def create_text_image(
    text: str,
    background_path: Optional[str] = None,
    default_size: Tuple[int, int] = (800, 600),
    font_size: int = 24,
    font_path: Optional[str] = None,
    text_color: Tuple[int, int, int] = (0, 0, 0),
    margin: int = 60,
    align: str = "center"
) -> Image.Image:
    """
    创建带文字的图片

    Args:
        text: 要显示的文本
        background_path: 背景图片路径，None则创建纯色背景
        default_size: 默认图片尺寸
        font_size: 字体大小
        font_path: 字体路径
        text_color: 文字颜色
        margin: 边距
        align: 文字对齐方式

    Returns:
        生成的图片
    """
    # 加载或创建背景
    if background_path and os.path.exists(background_path):
        background = Image.open(background_path).convert("RGBA")
    else:
        background = Image.new("RGBA", default_size, (240, 240, 240, 255))

    draw = ImageDraw.Draw(background)

    # 加载字体
    font = load_font_with_fallback(font_path, font_size)

    # 计算文本区域
    max_width = background.width - margin * 2
    max_height = background.height - margin * 2

    # 处理文本换行
    wrapped_text = wrap_text(text, font, max_width)

    # 计算文本位置（居中）
    text_bbox = draw.multiline_textbbox((0, 0), wrapped_text, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]

    x_position = (background.width - text_width) // 2
    y_position = (background.height - text_height) // 2

    # 绘制文本
    draw.multiline_text(
        (x_position, y_position),
        wrapped_text,
        font=font,
        fill=text_color,
        align=align
    )

    return background


def paste_image_with_alpha(
    background: Image.Image,
    foreground: Image.Image,
    position: Tuple[int, int]
) -> None:
    """
    带透明通道的图片粘贴

    Args:
        background: 背景图片
        foreground: 要粘贴的前景图片
        position: 粘贴位置
    """
    if foreground.mode == "RGBA":
        background.paste(foreground, position, foreground.split()[-1])
    else:
        background.paste(foreground, position)
