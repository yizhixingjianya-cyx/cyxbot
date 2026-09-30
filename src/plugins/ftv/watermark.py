"""
隐形水印模块

仅保留基于 PIL 的隐形水印（低透明度平铺文字）。
已移除：对抗性盲水印（blind_watermark）及相关 OpenCV 依赖——
blind_watermark 内部走 cv2.imread，无法兼容当前 OpenCV 5 环境，
会在每次生成图片时报错，故整个盲水印/线条水印模块一并删除。
"""
from typing import Tuple

from PIL import Image, ImageDraw, ImageFont
from nonebot.log import logger

from util.font_manager import Fonts, fallback_draw


class WatermarkGenerator:
    """水印生成器（当前仅提供 PIL 隐形水印）"""

    @classmethod
    def add_invisible_watermark(
        cls,
        image: Image.Image,
        watermark_text: str = "CYX-bot-mark",
        opacity: float = 0.05,
    ) -> Image.Image:
        """
        添加隐形水印（低透明度全屏平铺）

        Args:
            image: PIL 图片对象
            watermark_text: 水印文字
            opacity: 透明度 (0-1)

        Returns:
            添加水印后的图片
        """
        try:
            image = image.convert("RGBA")
            width, height = image.size

            # 创建透明覆盖层
            overlay = Image.new("RGBA", image.size, (255, 255, 255, 0))
            draw = fallback_draw(overlay)

            # 尝试加载统一字体链（覆盖生僻字/符号/emoji/外语）
            try:
                font = Fonts.cjk(40)
            except Exception:
                font = ImageFont.load_default()

            # 计算透明度颜色
            alpha = int(255 * opacity)
            watermark_color = (255, 255, 255, alpha)

            # 在图片上平铺水印
            spacing = 200  # 水印间距
            for y in range(0, height, spacing):
                for x in range(0, width, spacing):
                    # 随机偏移
                    offset_x = (x + y // 2) % spacing
                    draw.text(
                        (x + offset_x, y),
                        watermark_text,
                        font=font,
                        fill=watermark_color,
                    )

            # 合并图片
            watermarked = Image.alpha_composite(image, overlay)
            logger.info("隐形水印添加成功")
            return watermarked

        except Exception as e:
            logger.error(f"添加隐形水印失败：{e}")
            return image


def apply_all_watermarks(
    image: Image.Image,
    watermark_text: str = "CYX-bot-mark",
    blind_wm_text: str = "CYX-bot",  # 保留参数仅为兼容旧调用，不再使用
) -> Tuple[Image.Image, int]:
    """
    应用水印（当前仅隐形水印；盲水印已移除）

    Args:
        image: PIL 图片对象
        watermark_text: 隐形水印文字
        blind_wm_text: 兼容参数（已废弃）

    Returns:
        (添加水印后的图片, 盲水印长度 0)
    """
    logger.info("开始应用所有水印")
    final_image = WatermarkGenerator.add_invisible_watermark(
        image,
        watermark_text=watermark_text,
        opacity=0.03,
    )
    logger.info("所有水印应用完成")
    return final_image, 0
