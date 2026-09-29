"""
图像处理工具模块
提供图像处理和消息段转换功能
"""
from typing import List
from PIL import Image
from nonebot.adapters.onebot.v11 import MessageSegment


def colorize(image: Image.Image, from_color: str, to_color: str, target_color: tuple) -> Image.Image:
    """
    将图像中的特定颜色替换为目标颜色
    
    Args:
        image: 原始图像
        from_color: 源颜色（用于匹配）
        to_color: 目标颜色（用于匹配）
        target_color: 替换后的颜色
    
    Returns:
        处理后的图像
    """
    image = image.convert("RGBA")
    data = image.getdata()
    
    from_rgb = tuple(int(from_color[i:i+2], 16) for i in (1, 3, 5)) if from_color.startswith('#') else \
               tuple(int(from_color[i:i+2], 16) for i in (0, 2, 4)) if len(from_color) == 6 else \
               (0, 0, 0)
    
    to_rgb = tuple(int(to_color[i:i+2], 16) for i in (1, 3, 5)) if to_color.startswith('#') else \
             tuple(int(to_color[i:i+2], 16) for i in (0, 2, 4)) if len(to_color) == 6 else \
             (255, 255, 255)
    
    new_data = []
    for item in data:
        r, g, b, a = item
        if (r, g, b) == from_rgb or (r, g, b) == to_rgb:
            new_data.append((target_color[0], target_color[1], target_color[2], a))
        else:
            new_data.append(item)
    
    image.putdata(new_data)
    return image


def to_segment(frames: List[Image.Image], duration: int = 75) -> MessageSegment:
    """
    将图像帧列表转换为 GIF 消息段
    
    Args:
        frames: 图像帧列表
        duration: 每帧持续时间（毫秒）
    
    Returns:
        GIF 消息段
    """
    import io
    
    buffer = io.BytesIO()
    frames[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=duration,
        loop=0,
        disposal=2,
    )
    buffer.seek(0)
    
    return MessageSegment.image(buffer)