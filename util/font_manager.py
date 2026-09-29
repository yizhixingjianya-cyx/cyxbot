"""
字体管理模块
统一管理项目中的字体文件，提供字体路径获取和加载功能
"""

import os
import logging
import functools
from pathlib import Path
from typing import List, Optional, Dict, Sequence, Tuple
from PIL import ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# 字体目录路径
FONTS_DIR = Path(__file__).parent / "fonts"

# 字体映射表
FONT_MAP = {
    # 字体名称: 文件名
    "cjk": "CJKUnified.ttf",  # 统一字体：思源黑体 + Plangothic 补字 + 符号 + emoji
    "noto_sans": "NotoSans.ttf",  # 拉丁扩展/希腊/西里尔/越南语
    "noto_sans_kr": "NotoSansKR.ttf",  # 韩文谚文
    "noto_sans_arabic": "NotoSansArabic.ttf",  # 阿拉伯文
    "noto_sans_hebrew": "NotoSansHebrew.ttf",  # 希伯来文
    "noto_sans_thai": "NotoSansThai.ttf",  # 泰文
    "noto_sans_devanagari": "NotoSansDevanagari.ttf",  # 天城文
    "cubic": "Cubic.ttf",
    "cubic_11": "Cubic_11.ttf",
    "hanyi": "hanyi.otf",
    "mi": "mi.ttf",
}

# 默认回退链：主字体未覆盖的字符依次向后找，全部开源（OFL）
FALLBACK_FONTS = (
    "cjk",
    "noto_sans",
    "noto_sans_kr",
    "noto_sans_arabic",
    "noto_sans_hebrew",
    "noto_sans_thai",
    "noto_sans_devanagari",
)


def get_font_path(font_name: str) -> Optional[Path]:
    """
    获取字体文件路径

    Args:
        font_name: 字体名称或文件名

    Returns:
        字体文件路径，不存在返回None
    """
    # 如果是完整文件名，直接使用
    if font_name in FONT_MAP.values():
        font_path = FONTS_DIR / font_name
    else:
        # 从映射表中查找
        font_file = FONT_MAP.get(font_name.lower())
        if font_file:
            font_path = FONTS_DIR / font_file
        else:
            # 尝试直接拼接
            font_path = FONTS_DIR / font_name

    if font_path.exists():
        return font_path

    logger.warning(f"字体文件不存在: {font_path}")
    return None


def load_font(font_name: str, size: int = 24) -> ImageFont.FreeTypeFont:
    """
    加载字体

    Args:
        font_name: 字体名称或文件名
        size: 字体大小

    Returns:
        字体对象，失败则返回默认字体
    """
    font_path = get_font_path(font_name)

    if font_path:
        try:
            return ImageFont.truetype(str(font_path), size)
        except Exception as e:
            logger.warning(f"加载字体失败 {font_path}: {e}")

    return ImageFont.load_default()


def get_all_fonts() -> Dict[str, Path]:
    """
    获取所有可用字体

    Returns:
        字体名称到路径的映射字典
    """
    fonts = {}
    for name, filename in FONT_MAP.items():
        path = FONTS_DIR / filename
        if path.exists():
            fonts[name] = path
    return fonts


def register_font(name: str, filename: str):
    """
    注册新字体

    Args:
        name: 字体别名
        filename: 字体文件名
    """
    FONT_MAP[name.lower()] = filename
    logger.info(f"注册字体: {name} -> {filename}")


@functools.lru_cache(maxsize=None)
def _coverage(font_file: str) -> frozenset:
    """读取字体 cmap 码位集合（按文件名缓存）。

    逐字回退需要知道每个字体覆盖了哪些字符，Pillow 不暴露 cmap，故用 fontTools 读取。
    """
    try:
        from fontTools.ttLib import TTFont

        tt = TTFont(str(FONTS_DIR / font_file), lazy=True)
        try:
            return frozenset(tt.getBestCmap().keys())
        finally:
            tt.close()
    except Exception as e:
        logger.warning(f"读取字体码表失败 {font_file}: {e}")
        return frozenset()


class FontChain:
    """按字符自动回退的多字体集合。

    对外接口对齐 ``ImageFont.FreeTypeFont`` 常用部分（``size`` / ``path``），
    实际绘制需配合 :class:`FallbackImageDraw` 使用。
    """

    def __init__(self, size: int, names: Sequence[str] = FALLBACK_FONTS):
        self.size = size
        self._fonts: List[Tuple[ImageFont.FreeTypeFont, frozenset]] = []
        for name in names:
            path = get_font_path(name)
            if not path:
                continue
            try:
                font = ImageFont.truetype(str(path), size)
            except Exception as e:
                logger.warning(f"加载字体失败 {path}: {e}")
                continue
            self._fonts.append((font, _coverage(path.name)))
        if not self._fonts:
            logger.warning("回退链中没有任何可用字体，使用 Pillow 内置位图字体")
            self._fonts.append((ImageFont.load_default(), frozenset()))
        self.path = str(self._fonts[0][0].path) if hasattr(self._fonts[0][0], "path") else ""

    @property
    def fonts(self) -> List[Tuple[ImageFont.FreeTypeFont, frozenset]]:
        """回退链中的 (字体, 覆盖码位) 列表"""
        return self._fonts

    def font_for(self, ch: str):
        """取能显示该字符的第一个字体，都不支持时用主字体（渲染为方块）"""
        cp = ord(ch)
        for font, covered in self._fonts:
            if cp in covered:
                return font
        return self._fonts[0][0]

    def runs(self, text: str) -> List[Tuple[ImageFont.FreeTypeFont, str]]:
        """把文本切成 ``[(字体, 连续片段)]``，相邻同字体自动合并"""
        runs: List[list] = []
        for ch in text:
            font = self.font_for(ch)
            if runs and runs[-1][0] is font:
                runs[-1][1].append(ch)
            else:
                runs.append([font, [ch]])
        return [(font, "".join(chars)) for font, chars in runs]


class FallbackImageDraw(ImageDraw.ImageDraw):
    """支持字符级字体回退的 ImageDraw。

    把 ``font`` 传成 :class:`FontChain` 时，会按字符拆分分别用回退链中的字体绘制；
    传普通字体时行为与 ``ImageDraw.ImageDraw`` 完全一致。
    """

    def text(self, xy, text, fill=None, font=None, *args, **kwargs):
        if not isinstance(font, FontChain):
            return super().text(xy, text, fill, font, *args, **kwargs)
        # 带 anchor 时位置语义依赖整体排版，直接交给主字体，避免错位
        if kwargs.get("anchor") is not None or (len(args) > 0 and args[0] is not None):
            return super().text(xy, text, fill, font.fonts[0][0], *args, **kwargs)

        x, y = xy
        result = (x, y)
        for sub_font, chunk in font.runs(str(text)):
            result = super().text((x, y), chunk, fill, sub_font, *args, **kwargs)
            x += self.textlength(chunk, font=sub_font)
        return result

    def textlength(self, text, font=None, *args, **kwargs):
        if not isinstance(font, FontChain):
            return super().textlength(text, font, *args, **kwargs)
        return sum(
            self.textlength(chunk, font=sub_font, *args, **kwargs)
            for sub_font, chunk in font.runs(str(text))
        )

    def textbbox(self, xy, text, font=None, *args, **kwargs):
        if not isinstance(font, FontChain):
            return super().textbbox(xy, text, font, *args, **kwargs)
        x, y = xy
        # 竖直范围沿用主字体度量，宽度按回退结果累加
        _, top, _, bottom = super().textbbox((0, 0), "汉", font.fonts[0][0])
        return (x, y + top, x + self.textlength(text, font=font), y + bottom)

    def multiline_textlength(self, text, font=None, spacing=4, *args, **kwargs):
        if not isinstance(font, FontChain):
            return super().multiline_textlength(text, font, spacing, *args, **kwargs)
        widths = [self.textlength(line, font=font) for line in str(text).split("\n")]
        return max(widths) if widths else 0

    def multiline_textbbox(self, xy, text, font=None, spacing=4, align="left", *args, **kwargs):
        if not isinstance(font, FontChain):
            return super().multiline_textbbox(xy, text, font, spacing, *args, **kwargs)
        x, y = xy
        lines = str(text).split("\n")
        _, top, _, bottom = super().textbbox((0, 0), "汉", font.fonts[0][0])
        height = len(lines) * (font.size + spacing) - spacing if lines else 0
        return (x, y + top, x + self.multiline_textlength(text, font=font, spacing=spacing), y + height + bottom)

    def multiline_text(
        self,
        xy,
        text,
        fill=None,
        font=None,
        anchor=None,
        spacing=4,
        align="left",
        direction=None,
        features=None,
        language=None,
        stroke_width=0,
        stroke_fill=None,
        embedded_color=False,
    ):
        if not isinstance(font, FontChain):
            return super().multiline_text(
                xy, text, fill, font, anchor, spacing, align, direction,
                features, language, stroke_width, stroke_fill, embedded_color,
            )
        x, y = xy
        lines = str(text).split("\n")
        widths = [self.textlength(line, font=font) for line in lines]
        block_width = max(widths) if widths else 0
        for line, line_width in zip(lines, widths):
            if align == "center":
                line_x = x + (block_width - line_width) / 2
            elif align == "right":
                line_x = x + (block_width - line_width)
            else:
                line_x = x
            self.text(
                (line_x, y),
                line,
                fill=fill,
                font=font,
                anchor=anchor,
                direction=direction,
                features=features,
                language=language,
                stroke_width=stroke_width,
                stroke_fill=stroke_fill,
                embedded_color=embedded_color,
            )
            y += font.size + spacing
        return x, y


def fallback_draw(image) -> ImageDraw.ImageDraw:
    """创建支持字体回退的画布对象"""
    return FallbackImageDraw(image)


# 常用字体快捷访问
class Fonts:
    """字体快捷访问类"""

    @staticmethod
    def cjk(size: int = 24) -> FontChain:
        """统一字体链：覆盖常用/生僻汉字（CJK 扩展 A–G）、各类符号、emoji 及多国外语。

        由 CJKUnified.ttf（思源黑体 + Plangothic 补字 + Noto Symbols + Noto Emoji）
        加 Noto Sans / KR / Arabic / Hebrew / Thai / Devanagari 组成逐字回退链，
        因此不受单字体 65535 字形上限约束。
        """
        return FontChain(size)

    @staticmethod
    def cubic(size: int = 24) -> ImageFont.FreeTypeFont:
        """Cubic字体"""
        return load_font("cubic", size)

    @staticmethod
    def cubic_11(size: int = 24) -> ImageFont.FreeTypeFont:
        """Cubic_11字体"""
        return load_font("cubic_11", size)

    @staticmethod
    def hanyi(size: int = 24) -> ImageFont.FreeTypeFont:
        """汉仪字体"""
        return load_font("hanyi", size)

    @staticmethod
    def mi(size: int = 24) -> ImageFont.FreeTypeFont:
        """小米字体"""
        return load_font("mi", size)


# 向后兼容的快捷函数
def get_cubic_font(size: int = 24) -> ImageFont.FreeTypeFont:
    """获取Cubic字体"""
    return Fonts.cubic(size)


def get_hanyi_font(size: int = 24) -> ImageFont.FreeTypeFont:
    """获取汉仪字体"""
    return Fonts.hanyi(size)


def get_mi_font(size: int = 24) -> ImageFont.FreeTypeFont:
    """获取小米字体"""
    return Fonts.mi(size)
