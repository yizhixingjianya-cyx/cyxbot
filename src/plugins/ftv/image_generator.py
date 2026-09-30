from PIL import Image, ImageDraw, ImageFont, ImageFilter
from io import BytesIO
from typing import Dict, Optional, List, Tuple
import os
import hashlib
import time
import asyncio
from pathlib import Path
import concurrent.futures
import multiprocessing
import aiohttp
import aiofiles
from nonebot.log import logger
from util.font_manager import Fonts, fallback_draw
from .watermark import apply_all_watermarks
from .token_manager import token_manager

# 事件循环执行器
event_loop = None
def get_event_loop():
    """获取或创建事件循环"""
    global event_loop
    if event_loop is None:
        event_loop = asyncio.get_event_loop()
    return event_loop


# 当前文件路径
current_file_path = os.path.abspath(__file__)
# ftv 插件文件夹路径
ftv_dir = os.path.dirname(current_file_path)

# 图片缓存目录
image_cache_dir = Path(ftv_dir) / "cache" / "images"
image_cache_dir.mkdir(parents=True, exist_ok=True)

# 兜底图：任何图片缺失/请求失败时统一使用它（铺满裁切）
NOPIC_PATH = Path(ftv_dir) / "nopic.png"
_NOPIC_CACHE: Optional[Image.Image] = None

# 缓存过期时间（1 小时）
CACHE_EXPIRY = 3600  # 秒

# 字体对象缓存
FONT_CACHE = {}

# 初始化字体缓存
def _init_font_cache():
    """初始化字体缓存"""
    global FONT_CACHE
    try:
        # 预加载常用字体（统一使用支持生僻字/符号/emoji 的字体）
        FONT_CACHE['title'] = Fonts.cjk(20)
        FONT_CACHE['content'] = Fonts.cjk(12)
        FONT_CACHE['small'] = Fonts.cjk(10)
        logger.info("字体缓存初始化成功")
    except Exception as e:
        logger.warning(f"字体缓存初始化失败：{e}")
        # 使用默认字体
        FONT_CACHE['title'] = ImageFont.load_default()
        FONT_CACHE['content'] = ImageFont.load_default()
        FONT_CACHE['small'] = ImageFont.load_default()

# 初始化字体缓存
_init_font_cache()


def _get_cache_key(prefix: str, urls: list) -> str:
    """生成缓存文件的唯一键"""
    url_str = "|".join(urls)
    hash_key = hashlib.md5(url_str.encode()).hexdigest()
    return f"{prefix}_{hash_key}"


def _load_cached_image(cache_key: str) -> Optional[Image.Image]:
    """从缓存加载图片"""
    cache_file = image_cache_dir / f"{cache_key}.png"
    
    if cache_file.exists():
        # 检查缓存是否过期（通过文件修改时间）
        file_mtime = cache_file.stat().st_mtime
        if time.time() - file_mtime < CACHE_EXPIRY:
            try:
                logger.debug(f"使用缓存图片：{cache_file.name}")
                return Image.open(cache_file)
            except Exception as e:
                logger.warning(f"读取缓存图片失败：{e}")
                cache_file.unlink(missing_ok=True)
        else:
            logger.debug(f"缓存图片已过期：{cache_file.name}")
            cache_file.unlink(missing_ok=True)
    
    return None


def _save_to_cache(img: Image.Image, cache_key: str):
    """保存图片到缓存"""
    cache_file = image_cache_dir / f"{cache_key}.png"
    try:
        img.save(cache_file, format='PNG')
        logger.debug(f"已保存缓存图片：{cache_file.name}")
    except Exception as e:
        logger.warning(f"保存缓存图片失败：{e}")


async def generate_profile_image(
    vertical_img_url: str,
    avatar_url: str,
    horizontal_img_url: str,
    showcase_other_url: str,
    profile_data: Dict,
    title_text: str = "CYX-bot 兽频道档案",
    max_intro_length: int = 20,  # 简介最大长度限制
    max_intro_lines: int = 3     # 简介最大行数限制
) -> Optional[Image.Image]:
    try:
        # 生成缓存键
        cache_key = _get_cache_key(
            "profile",
            [vertical_img_url, avatar_url, horizontal_img_url, showcase_other_url, str(profile_data.get('id', ''))]
        )
        
        # 尝试从缓存加载
        cached_img = _load_cached_image(cache_key)
        if cached_img:
            logger.info(f"使用缓存的档案图片 | 用户：{profile_data.get('nickname', 'unknown')}")
            return cached_img
        
        logger.info(f"开始生成档案图片，使用的 URLs: "
                   f"vertical={vertical_img_url[:50]}..., "
                   f"avatar={avatar_url[:50]}..., "
                   f"horizontal={horizontal_img_url[:50]}..., "
                   f"showcase={showcase_other_url[:50]}...")
        
        # 1. 处理竖版背景图（1000x1500）
        img_v = await _load_valid_image(
            img_url=vertical_img_url,
            img_type="竖版背景图",
            profile_id=profile_data.get("id", "unknown")
        )
        if not img_v:
            logger.error(f"档案{profile_data.get('id')}无法加载竖版背景图，终止图片生成")
            return None
        
        blurred_img = img_v.filter(ImageFilter.GaussianBlur(radius=10))
        background = _fit_cover(blurred_img, (1000, 1500))

        # 2. 绘制圆角矩形（70% 透明度，范围：100-900x100-1400）
        rect_left, rect_top = 100, 100
        rect_right, rect_bottom = 900, 1400  # 矩形宽 800，高 1300
        corner_radius = 20
        rect_fill = (255, 255, 255, 127)  # 70% 透明度

        draw = fallback_draw(background)
        draw.rounded_rectangle(
            [(rect_left, rect_top), (rect_right, rect_bottom)],
            radius=corner_radius,
            outline="white",
            width=5,
            fill=rect_fill
        )

        # 3. 加载字体（定义层级）
        try:
            title_font = Fonts.cjk(40)
            nickname_font = Fonts.cjk(30)
            content_font = Fonts.cjk(20)
            footer_font = Fonts.cjk(18)
            logger.debug("成功加载统一字体")
        except Exception as e:
            logger.warning(f"加载统一字体失败：{str(e)}，使用默认字体")
            title_font = ImageFont.load_default()
            nickname_font = ImageFont.load_default()
            content_font = ImageFont.load_default()
            footer_font = ImageFont.load_default()

        # 4. 顶部标题（居中）
        title_y = rect_top + 30  # 距矩形顶部 30px
        title_x = (rect_left + rect_right) // 2  # 水平居中
        title_width = draw.textlength(title_text, font=title_font)
        draw.text(
            (title_x - title_width//2, title_y),
            title_text,
            font=title_font,
            fill="#222222"
        )

        # 5. 处理头像（200x200）+ 下方放大昵称
        img_a = await _load_valid_image(
            img_url=avatar_url,
            img_type="头像",
            profile_id=profile_data.get("id", "unknown")
        )
        if not img_a:
            logger.error(f"档案{profile_data.get('id')}无法加载头像，终止图片生成")
            return None
        
        img_a_resized = _fit_cover(img_a, (200, 200))
        # 头像位置：左对齐，距矩形顶部 150px（避开标题）
        img_a_x = rect_left + 50  # 距左边缘 50px
        img_a_y = rect_top + 150  # 距顶部 150px（标题占约 60px+ 间距 40px）
        background.paste(
            img_a_resized,
            (img_a_x, img_a_y),
            img_a_resized.convert("RGBA").split()[-1] if img_a.mode == "RGBA" else None
        )

        # 头像下方的放大昵称（居中对齐头像）
        nickname = profile_data.get("nickname")
        nickname_str = nickname if nickname else "未知昵称"
        nickname_y = img_a_y + 200 + 15  # 头像底部 +15px 间距
        nickname_x = img_a_x + 100  # 头像中点 x 坐标（200/2=100）
        nickname_width = draw.textlength(nickname_str, font=nickname_font)
        draw.text(
            (nickname_x - nickname_width//2, nickname_y),  # 相对于头像居中
            nickname_str,
            font=nickname_font,
            fill="#222222"  # 深色突出昵称
        )

        # 6. 右侧文字信息（用户 ID、兽种等）
        # 文字起始位置：头像右侧 50px，与头像顶部平齐
        text_x = img_a_x + 200 + 50  # 头像右侧 50px
        text_y = img_a_y  # 与头像顶部对齐（y 坐标相同）

        # 用户名
        username = profile_data.get("username")
        username_str = username if username else "unknown_username"
        draw.text((text_x, text_y), f"用户名：{username_str}", font=content_font, fill="#333333")
        text_y += 30
        
        # 用户 ID
        user_id = profile_data.get("id", "未知 ID")
        draw.text((text_x, text_y), f"用户 ID: {user_id}", font=content_font, fill="#333333")
        text_y += 30

        # 兽种
        species = profile_data.get("fursuit_species")
        species_str = species if species else "未填写"
        draw.text((text_x, text_y), f"兽种：{species_str}", font=content_font, fill="#333333")
        text_y += 30

        # 兽装生日
        birthday = profile_data.get("fursuit_birthday")
        if birthday:
            birthday_str = birthday[:10]
        else:
            birthday_str = "无"
        draw.text((text_x, text_y), f"兽装生日：{birthday_str}", font=content_font, fill="#333333")
        text_y += 30
        
        # 制作师/工作室
        maker = profile_data.get("fursuit_maker")
        maker_str = maker if maker else "未知"
        draw.text((text_x, text_y), f"制作师：{maker_str}", font=content_font, fill="#333333")
        text_y += 30
        
        # 位置信息
        location = profile_data.get("location")
        location_str = location if location else "未知"
        draw.text((text_x, text_y), f"位置：{location_str}", font=content_font, fill="#333333")
        text_y += 20

        # 个人简介（自动换行并限制长度和行数）
        intro = profile_data.get("introduction", "无")
        intro_str = str(intro) if intro is not None else "无"
        
        # 限制简介最大长度
        if len(intro_str) > max_intro_length:
            intro_str = intro_str[:max_intro_length] + "..."
            logger.debug(f"档案{profile_data.get('id')}的简介过长，已截断至{max_intro_length}字符")
        
        draw.text((text_x, text_y), "个人简介:", font=content_font, fill="#333333")
        text_y += 25
        
        # 绘制带行数限制的简介文本
        text_y = _draw_wrapped_text(
            draw, 
            intro_str, 
            text_x, 
            text_y, 
            content_font, 
            max_width=500, 
            line_spacing=5,
            max_lines=max_intro_lines  # 限制最大行数
        )
        text_y += 30

        # 兴趣爱好
        interests = profile_data.get("interests", [])
        if isinstance(interests, list):
            interests_str = ", ".join([str(item) for item in interests]) if interests else "无"
        else:
            interests_str = str(interests) if interests else "无"
        draw.text((text_x, text_y), "兴趣爱好:", font=content_font, fill="#333333")
        text_y += 25
        text_y = _draw_wrapped_text(draw, interests_str, text_x, text_y, content_font, max_width=500, line_spacing=5)
        text_y += 40  # 与下方图片留间距

        # 7. showcase_other 图片（1:1 比例，裁剪为 400x400）
        img_showcase = await _load_valid_image(
            img_url=showcase_other_url,
            img_type="showcase_other 图片",
            profile_id=profile_data.get("id", "unknown")
        )
        if not img_showcase:
            logger.warning(f"档案{profile_data.get('id')}的 showcase_other 图片加载失败，使用占位空间")
            # 绘制灰色占位框（400x400）
            showcase_x = rect_left + 100
            showcase_y = text_y
            draw.rectangle(
                [(showcase_x, showcase_y), (showcase_x + 400, showcase_y + 400)],
                fill=(240, 240, 240),
                outline="#cccccc",
                width=2
            )
            text_y += 400 + 20
        else:
            # 裁剪为 1:1 比例（取中心区域）
            img_width, img_height = img_showcase.size
            min_dim = min(img_width, img_height)
            left = (img_width - min_dim) // 2
            top = (img_height - min_dim) // 2
            right = left + min_dim
            bottom = top + min_dim
            img_showcase_cropped = img_showcase.crop((left, top, right, bottom))
            img_showcase_resized = img_showcase_cropped.resize((400, 400), Image.Resampling.LANCZOS)
            showcase_x = (rect_left + rect_right) // 2 - 200
            showcase_y = text_y
            background.paste(
                img_showcase_resized,
                (showcase_x, showcase_y),
                img_showcase_resized.convert("RGBA").split()[-1] if img_showcase.mode == "RGBA" else None
            )
            text_y = showcase_y + 400 + 20

        # 8. 横版展示图（700x250）
        img_h = await _load_valid_image(
            img_url=horizontal_img_url,
            img_type="横版展示图",
            profile_id=profile_data.get("id", "unknown")
        )
        if not img_h:
            logger.error(f"档案{profile_data.get('id')}无法加载横版展示图，终止图片生成")
            return None
        
        img_h_resized = _fit_cover(img_h, (570, 300))
        img_h_x = rect_left + 100
        img_h_y = rect_bottom - 150 - 250
        background.paste(
            img_h_resized,
            (img_h_x, img_h_y),
            img_h_resized.convert("RGBA").split()[-1] if img_h.mode == "RGBA" else None
        )

        # 9. 底部尾部文字
        footer_text = "兽频道 API from VDS"
        footer_y = img_h_y + 300 + 20
        footer_width = draw.textlength(footer_text, font=footer_font)
        footer_x = (rect_left + rect_right) // 2 - footer_width//2
        draw.text(
            (footer_x, footer_y),
            footer_text,
            font=footer_font,
            fill="#666666"
        )

        logger.info(f"档案{profile_data.get('id')}图片生成成功")
        
        # 应用水印（隐形水印）
        logger.info(f"开始为档案{profile_data.get('id')}添加水印")
        watermarked_background, _ = apply_all_watermarks(
            background,
            watermark_text=f"FTV-{profile_data.get('id', 'Unknown')}",
            blind_wm_text=f"FTV-Copyright-{profile_data.get('id', 'Unknown')}"
        )
        logger.info(f"档案{profile_data.get('id')}水印添加完成")
        
        # 保存到缓存
        _save_to_cache(watermarked_background, cache_key)
        
        return watermarked_background

    except Exception as e:
        logger.error(f"档案{profile_data.get('id')}图片生成失败：{str(e)}", exc_info=True)
        return None


async def _download_and_cache_image(url: str, img_type: str = "图片") -> Optional[BytesIO]:
    """下载并缓存图片（异步）
    
    Args:
        url: 图片 URL
        img_type: 图片类型描述
    
    Returns:
        BytesIO 对象，失败返回 None
    """
    import aiofiles  # 确保 aiofiles 可用
    try:
        # 去除 URL 中的反引号、空格和其他特殊字符
        original_url = url
        # 移除首尾的反引号和空格
        url = url.strip('`').strip()
        # 移除所有空格
        url = url.replace(' ', '')
        # 移除省略号（如果有的话）
        url = url.replace('...', '')
        # 确保 URL 格式正确
        if not url.startswith('http://') and not url.startswith('https://'):
            logger.error(f"下载{img_type}失败：URL 格式无效：{url}")
            return None
        if original_url != url:
            logger.debug(f"处理{img_type} URL：{original_url[:50]}... -> {url[:50]}...")
        
        # 生成缓存文件名（使用 URL 的 MD5）
        # 使用 .png 扩展名，因为我们保存为 PNG 格式
        filename = hashlib.md5(url.encode()).hexdigest() + '.png'
        cache_path = image_cache_dir / filename
        logger.debug(f"{img_type}缓存路径：{cache_path}")
        
        # 检查缓存
        if cache_path.exists():
            logger.debug(f"✅ 发现{img_type}缓存：{filename}")
            try:
                async with aiofiles.open(cache_path, 'rb') as f:
                    data = await f.read()
                logger.debug(f"读取{img_type}缓存成功，大小：{len(data)} 字节")
                
                # 验证缓存文件是否为有效的图片
                cache_buffer = BytesIO(data)
                cache_buffer.seek(0)  # 重置文件指针到开头
                img = Image.open(cache_buffer)
                img.load()
                logger.debug(f"验证{img_type}缓存成功，格式：{img.format}，尺寸：{img.size}")
                
                # 返回验证过的图片数据，而不是原始 data
                validated_buffer = BytesIO()
                img.save(validated_buffer, format='PNG')
                validated_buffer.seek(0)  # 重置文件指针到开头
                logger.debug(f"返回验证后的{img_type}缓存数据")
                return validated_buffer
            except Exception as e:
                logger.error(f"读取{img_type}缓存失败：{e}")
                # 删除损坏的缓存文件
                try:
                    cache_path.unlink()
                    logger.debug(f"已删除损坏的缓存文件：{filename}")
                except Exception as unlink_error:
                    logger.error(f"删除损坏的缓存文件失败：{unlink_error}")
        else:
            logger.debug(f"❌ 未找到{img_type}缓存：{filename}")
        
        # 获取临时 accessToken
        access_token = await token_manager.get_access_token()
        
        # 下载图片
        logger.info(f"开始下载{img_type} | 完整 URL：{url[:80]}...")
        try:
            # 禁用 SSL 验证以避免证书错误
            connector = aiohttp.TCPConnector(verify_ssl=False)
            # 添加完整的浏览器请求头
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 STARFURE/1.0.11',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
                'Accept-Encoding': 'gzip, deflate, br',
                'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
                'Cache-Control': 'max-age=0',
                'Connection': 'keep-alive',
                'Sec-Fetch-Dest': 'image',
                'Sec-Fetch-Mode': 'no-cors',
                'Sec-Fetch-Site': 'cross-site',
                'Sec-Fetch-User': '?1'
            }
            
            # 将 accessToken 添加到请求头
            if access_token:
                headers['x-api-Token'] = access_token
                logger.info(f"✅ 附加 accessToken 到请求头 | {img_type} | Token: {access_token[:30]}...")
            else:
                logger.warning(f"❌ 未获取到 accessToken，请求头将不携带 token")
            async with aiohttp.ClientSession(connector=connector) as session:
                logger.info(f"图片请求头 | {img_type} | URL: {url[:80]}... | Headers: {headers}")
                async with session.get(url, timeout=15, headers=headers) as response:
                    # 即使状态码不是 200，也尝试读取内容
                    content = await response.read()
                    # 检查状态码（200 或 304 都表示成功）
                    if response.status not in [200, 304]:
                        logger.error(f"下载{img_type}失败：{response.status}, message='{response.reason}', url='{url}'")
                        logger.error(f"下载{img_type} | 响应头：{dict(response.headers)}")
                        try:
                            logger.error(f"下载{img_type} | 响应内容：{content.decode('utf-8', errors='replace')}")
                        except:
                            logger.error(f"下载{img_type} | 响应内容：{content}")
                        return None
                    # 打印内容类型和长度
                    content_type = response.headers.get('Content-Type', 'unknown')
                    logger.info(f"下载{img_type}成功 | 状态码：{response.status} | 内容类型：{content_type} | 长度：{len(content)} 字节")
                    
                    # 检查内容类型是否为图片
                    if not content_type.startswith('image/'):
                        logger.error(f"下载{img_type}失败：内容类型不是图片，而是：{content_type}")
                        logger.error(f"下载{img_type} | 响应头：{dict(response.headers)}")
                        # 如果是 text 类型，打印完整内容
                        if content_type.startswith('text/'):
                            logger.info(f"下载{img_type} | 完整响应内容：{content.decode('utf-8', errors='replace')}")
                        else:
                            logger.info(f"下载{img_type} | 响应内容前 200 字节：{content[:200]}")
                        return None
        except Exception as e:
            logger.error(f"下载{img_type}失败：{e}", exc_info=True)
            return None
        
        # 检查内容是否为空
        if not content:
            logger.error(f"下载{img_type}失败：内容为空")
            return None
        
        # 检查内容长度是否合理
        if len(content) < 100:
            logger.error(f"下载{img_type}失败：内容长度过短（{len(content)} 字节），可能不是有效的图片")
            logger.debug(f"下载{img_type} | 内容：{content[:100]}")
            return None
        
        # 验证是否为有效的图片
        try:
            # 打印前 20 字节的内容，用于调试
            logger.debug(f"下载{img_type} | 前 20 字节：{content[:20]}")
            img = Image.open(BytesIO(content))
            img.load()
            logger.info(f"验证{img_type}成功 | 图片格式：{img.format} | 尺寸：{img.size}")
        except Exception as e:
            logger.error(f"下载{img_type}失败：无效的图片数据：{e}")
            # 打印更多信息以便调试
            if len(content) > 0:
                logger.debug(f"下载{img_type} | 失败内容前 100 字节：{content[:100]}")
                logger.debug(f"下载{img_type} | 内容长度：{len(content)} 字节")
            return None
        
        # 保存到缓存（转换为 PNG 格式）
        try:
            img.save(cache_path, format='PNG')
            logger.debug(f"💾 已缓存{img_type} | {filename} | 大小：{len(content)} 字节")
        except Exception as e:
            logger.error(f"保存{img_type}缓存失败：{e}")
            # 即使保存失败，仍然返回图片数据
        
        # 返回验证过的图片数据，而不是原始 content
        # 这样可以确保返回的数据是有效的图片格式
        validated_buffer = BytesIO()
        img.save(validated_buffer, format='PNG')
        validated_buffer.seek(0)  # 重置文件指针到开头
        logger.debug(f"返回验证后的{img_type}数据")
        return validated_buffer
    
    except Exception as e:
        logger.error(f"下载{img_type}失败：{e}", exc_info=True)
        return None


def _load_nopic_image() -> Optional[Image.Image]:
    """加载本地兜底图 nopic.png（带内存缓存，返回副本避免被污染）"""
    global _NOPIC_CACHE
    if _NOPIC_CACHE is None:
        try:
            img = Image.open(NOPIC_PATH)
            img.load()
            _NOPIC_CACHE = img.convert("RGBA")
        except Exception as e:
            logger.error(f"加载兜底图 nopic.png 失败：{e}")
            return None
    return _NOPIC_CACHE.copy()


def _fit_cover(
    img: Image.Image,
    size: Tuple[int, int],
    resample: Image.Resampling = Image.Resampling.LANCZOS,
) -> Image.Image:
    """等比缩放并居中裁剪到目标尺寸（铺满裁切，不拉伸变形）"""
    target_w, target_h = size
    src_w, src_h = img.size
    if src_w <= 0 or src_h <= 0:
        return img.resize(size, resample)
    ratio = max(target_w / src_w, target_h / src_h)
    scaled = img.resize(
        (max(target_w, round(src_w * ratio)), max(target_h, round(src_h * ratio))),
        resample,
    )
    left = (scaled.width - target_w) // 2
    top = (scaled.height - target_h) // 2
    return scaled.crop((left, top, left + target_w, top + target_h))


async def _load_valid_image(
    img_url: str,
    img_type: str,
    profile_id: str
) -> Optional[Image.Image]:
    """加载并验证图片，使用缓存机制（异步）。

    原图缺失或请求失败时，统一返回本地兜底图 nopic.png（铺满裁切由调用方处理）。
    """
    # 尝试从缓存加载
    cached_data = await _download_and_cache_image(img_url, img_type)
    
    if cached_data:
        try:
            cached_data.seek(0)  # 重置文件指针到开头
            img = Image.open(cached_data)
            img.load()  # 强制加载图片数据
            return img
        except Exception as e:
            logger.warning(f"{img_type}缓存文件损坏，改用兜底图：{e}")
    
    logger.warning(f"{img_type}加载失败（档案 ID: {profile_id}），改用兜底图 nopic.png")
    return _load_nopic_image()


def _wrap_text_to_lines(
    draw: ImageDraw.ImageDraw,
    paragraph: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list:
    """将单段文本按宽度自动换行。

    - 英文/数字等以空格分隔的单词尽量保持完整；
    - 无空格的中文长句（单“词”超宽）会按字符逐字断行。
    """
    lines: list = []
    current_line = ""
    for word in paragraph.split():
        if draw.textlength(word, font=font) <= max_width:
            test_line = f"{current_line} {word}".strip() if current_line else word
            if draw.textlength(test_line, font=font) <= max_width:
                current_line = test_line
            else:
                lines.append(current_line)
                current_line = word
        else:
            # 单词本身超宽（如连续中文无空格），按字符拆行
            if current_line:
                lines.append(current_line)
                current_line = ""
            for ch in word:
                test_line = current_line + ch
                if draw.textlength(test_line, font=font) <= max_width:
                    current_line = test_line
                else:
                    lines.append(current_line)
                    current_line = ch
    if current_line:
        lines.append(current_line)
    return lines


def _draw_wrapped_text(
    draw: ImageDraw.ImageDraw, 
    text: str, 
    x: int, 
    y: int, 
    font: ImageFont.FreeTypeFont, 
    max_width: int, 
    line_spacing: int,
    max_lines: Optional[int] = None  # 新增：最大行数限制，None 表示无限制
) -> int:
    """自动换行绘制文本，支持中文逐字断行、最大行数限制和换行符处理"""
    # 按换行符分割文本
    paragraphs = text.split('\n')
    current_y = y

    # 先将各段文本按宽度换行，得到全部行
    all_lines: list = []
    for paragraph in paragraphs:
        if not paragraph.strip():
            # 空行，直接跳过
            continue
        all_lines.extend(_wrap_text_to_lines(draw, paragraph, font, max_width))

    # 超过最大行数时截断，并在最后一行补省略号
    if max_lines is not None and len(all_lines) > max_lines:
        all_lines = all_lines[:max_lines]
        last_line = all_lines[-1]
        while last_line and draw.textlength(f"{last_line}...", font=font) > max_width:
            last_line = last_line[:-1]
        all_lines[-1] = f"{last_line}..."
        logger.debug(f"文本已达到最大行数限制 ({max_lines}行)，已截断")

    for line in all_lines:
        draw.text((x, current_y), line, font=font, fill="#333333")
        current_y += font.size + line_spacing

    return current_y


def _card_avatar_url(item: dict) -> Optional[str]:
    """取卡片头像地址：优先 avatar_url，其次角色的 images[0]。

    兼容某些接口返回的 URL 带反引号包裹的脏格式。
    """
    url = item.get('avatar_url')
    if not url:
        images = item.get('images') or []
        if images:
            url = images[0]
    if isinstance(url, str):
        url = url.strip().strip('`')
    return url or None


def _normalize_card_item(item: dict) -> dict:
    """把「用户档案」或「角色(崽崽)」两种数据源归一化为卡片文本字段。

    用户档案键：nickname/username/fursuit_species/location/...
    角色键：    name/species/gender/birthday/worldview/images
    缺失字段保持空值，渲染时跳过，避免出现多余的“未知”。
    """
    name = item.get('nickname') or item.get('name') or item.get('username') or '未知'
    username = item.get('username') or ''
    species = item.get('fursuit_species') or item.get('species') or ''
    location = item.get('location') or ''
    gender = item.get('gender') or ''
    birthday = item.get('birthday') or ''
    maker = item.get('fursuit_maker') or ''
    view_count = item.get('view_count')
    introduction = item.get('introduction') or ''
    worldview = item.get('worldview') or ''
    if worldview and not introduction:
        bio, bio_label = worldview, '设定'
        bio_max_lines = 15  # 角色(崽崽)设定允许展示更多行
    else:
        bio, bio_label = introduction, '简介'
        bio_max_lines = 3   # 普通用户简介保持精简
    return {
        'name': name,
        'username': username,
        'species': species,
        'location': location,
        'gender': gender,
        'birthday': birthday,
        'maker': maker,
        'view_count': view_count,
        'bio': bio,
        'bio_label': bio_label,
        'bio_max_lines': bio_max_lines,
    }


def _draw_card_info_lines(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    norm: dict,
    content_font,
    small_font,
    max_width: int = 100,
) -> int:
    """绘制卡片右侧信息区域（物种/地区/性别/生日等），只画存在值的字段。

    Returns:
        绘制结束后的 y 坐标
    """
    current_y = y
    username = norm['username']
    name = norm['name']

    # 用户名
    if username and username != name:
        draw.text((x, current_y), f"ID: {username}", font=small_font, fill="#666666")
        current_y += 14

    # 性别（角色数据）
    if norm['gender']:
        draw.text((x, current_y), f"性别：{norm['gender']}", font=content_font, fill="#333333")
        current_y += 16

    # 物种（用户档案 / 角色都有）
    if norm['species']:
        draw.text((x, current_y), f"物种：{norm['species']}", font=content_font, fill="#333333")
        current_y += 16

    # 地区（用户档案）
    if norm['location']:
        draw.text((x, current_y), f"地区：{norm['location']}", font=content_font, fill="#333333")
        current_y += 16

    # 生日（角色数据）
    if norm['birthday']:
        draw.text((x, current_y), f"生日：{norm['birthday']}", font=content_font, fill="#333333")
        current_y += 16

    # 兽装制作人
    if norm['maker']:
        draw.text((x, current_y), f"制作：{norm['maker']}", font=content_font, fill="#333333")
        current_y += 16

    # 浏览量
    if norm['view_count'] is not None:
        draw.text((x, current_y), f"浏览：{norm['view_count']}", font=content_font, fill="#333333")
        current_y += 16

    # 简介 / 角色设定（角色最多 15 行，用户简介最多 3 行）
    if norm['bio']:
        draw.text((x, current_y), f"{norm['bio_label']}：", font=content_font, fill="#333333")
        current_y += 16
        max_lines = norm.get('bio_max_lines', 3)
        current_y = _draw_wrapped_text(
            draw, norm['bio'], x, current_y,
            small_font, max_width=max_width, line_spacing=2, max_lines=max_lines
        )

    return current_y


async def generate_user_info_image(item: dict) -> Optional[Image.Image]:
    """生成单用户信息卡片图片（优化排版）
    
    Args:
        item: 用户数据字典
    
    Returns:
        生成的图片对象，失败返回 None
    """
    try:
        username = item.get('username') or item.get('name') or item.get('nickname') or 'unknown'
        logger.info(f"开始生成用户信息卡片 | 用户：{username}")
        
        # 卡片尺寸（根据用户提供的尺寸图）
        card_width = 250
        card_height = 350
        
        # 1. 创建空白卡片
        card = Image.new('RGBA', (card_width, card_height), (255, 255, 255, 255))
        draw = fallback_draw(card)
        logger.debug(f"创建画布 | 尺寸：{card_width}x{card_height}")
        
        # 2. 绘制圆角矩形背景
        corner_radius = 10
        draw.rounded_rectangle(
            [(0, 0), (card_width, card_height)],
            radius=corner_radius,
            fill="#f8f9fa",
            outline="#e9ecef",
            width=1
        )
        
        # 3. 使用字体缓存
        title_font = FONT_CACHE.get('title', ImageFont.load_default())
        content_font = FONT_CACHE.get('content', ImageFont.load_default())
        small_font = FONT_CACHE.get('small', ImageFont.load_default())
        
        # 4. 处理头像（异步下载优化）
        avatar_url = _card_avatar_url(item)
        avatar_size = 80
        avatar_x = 20
        avatar_y = 60
        
        # 绘制头像占位圆
        draw.ellipse(
            [(avatar_x, avatar_y), (avatar_x + avatar_size, avatar_y + avatar_size)],
            fill="#e9ecef"
        )
        
        # 尝试加载头像（使用缓存）
        if avatar_url:
            logger.debug(f"加载头像 | URL: {avatar_url[:50]}...")
            img_a = await _load_valid_image(
                img_url=avatar_url,
                img_type="头像",
                profile_id=item.get('username', 'unknown')
            )
            if img_a:
                logger.debug("头像加载成功，处理并粘贴到卡片")
                # 铺满裁切并裁剪为圆形
                img_a = _fit_cover(img_a, (avatar_size, avatar_size), Image.Resampling.BILINEAR)  # 更快的缩放算法
                # 创建圆形蒙版
                mask = Image.new('L', (avatar_size, avatar_size), 0)
                mask_draw = ImageDraw.Draw(mask)
                mask_draw.ellipse([(0, 0), (avatar_size, avatar_size)], fill=255)
                # 粘贴头像
                card.paste(img_a, (avatar_x, avatar_y), mask)
            else:
                logger.debug("头像加载失败，使用占位圆")
        else:
            logger.debug("无头像URL，使用占位圆")
        
        # 5. 顶部昵称（居中）
        norm = _normalize_card_item(item)
        name = norm['name']
        name_width = draw.textlength(name, font=title_font)
        name_x = (card_width - name_width) // 2
        name_y = 20
        draw.text((name_x, name_y), name, font=title_font, fill="#333333")
        
        # 6. 右侧信息区域
        info_x = avatar_x + avatar_size + 15
        info_y = 60

        _draw_card_info_lines(draw, info_x, info_y, norm, content_font, small_font, max_width=100)
        
        # 7. 底部水印
        footer_text = "VDS API - starfure"
        footer_width = draw.textlength(footer_text, font=small_font)
        footer_x = (card_width - footer_width) // 2
        footer_y = card_height - 20
        draw.text((footer_x, footer_y), footer_text, font=small_font, fill="#999999")
        
        # 应用水印（隐形水印）
        logger.info(f"开始为用户{username}添加水印")
        watermarked_card, _wm_len = apply_all_watermarks(
            card,
            watermark_text=f"FTV-{username}",
            blind_wm_text=f"FTV-Copyright-{username}"
        )
        logger.info(f"用户{username}水印添加完成")
        
        logger.info(f"用户信息卡片生成完成 | 用户：{username}")
        return watermarked_card
        
    except Exception as e:
        logger.error(f"生成用户信息图片失败：{e}", exc_info=True)
        return None


async def generate_users_collage(items: list, columns: int = 2) -> Optional[Image.Image]:
    """生成多用户拼图图片（并行优化）
    
    Args:
        items: 用户数据列表
        columns: 每行卡片数（分页每页 6 个时用 3 列 2 行）
    
    Returns:
        生成的拼图图片，失败返回 None
    """
    try:
        num_users = len(items)
        logger.info(f"开始生成多用户拼图 | 用户数量：{num_users} | 列数：{columns}")
        
        # 单张卡片尺寸
        card_width = 250
        card_height = 350
        gap = 10
        
        # 计算拼图尺寸（columns x N 网格；末页不满时收窄，避免右侧留白）
        columns = max(1, min(columns, num_users))
        rows = (num_users + columns - 1) // columns
        collage_width = columns * card_width + (columns - 1) * gap
        collage_height = rows * card_height + (rows - 1) * gap
        
        logger.debug(f"拼图尺寸：{collage_width}x{collage_height} | 网格：{columns}x{rows}")
        
        # 并行生成卡片
        cards = []
        max_workers = min(multiprocessing.cpu_count(), 12)  # 使用最多 12 个核心
        
        logger.info(f"使用 {max_workers} 个核心并行生成 {num_users} 个用户卡片")
        
        # 批量处理：先并行下载所有头像，再生成卡片
        # 这样可以减少 I/O 等待时间
        
        # 第一步：并行下载所有头像
        avatar_cache = {}
        
        # 异步下载头像
        async def download_avatar(item):
            avatar_url = _card_avatar_url(item)
            if avatar_url:
                try:
                    img = await _load_valid_image(
                        img_url=avatar_url,
                        img_type="头像",
                        profile_id=item.get('nickname') or item.get('name') or item.get('username') or 'unknown'
                    )
                    return item, img
                except Exception as e:
                    logger.warning(f"下载头像失败：{e}")
                    return item, None
            return item, None
        
        # 并行下载头像
        tasks = [download_avatar(item) for item in items]
        results = await asyncio.gather(*tasks)
        for item, img in results:
            if img:
                # 用对象身份做缓存键，避免多个同名角色/用户互相覆盖头像
                avatar_cache[str(id(item))] = img
        
        # 第二步：并行生成卡片（使用缓存的头像）
        async def generate_card_with_cache(item):
            try:
                # 创建卡片
                card = Image.new('RGBA', (card_width, card_height), (255, 255, 255, 255))
                draw = fallback_draw(card)
                
                # 绘制背景
                corner_radius = 10
                draw.rounded_rectangle(
                    [(0, 0), (card_width, card_height)],
                    radius=corner_radius,
                    fill="#f8f9fa",
                    outline="#e9ecef",
                    width=1
                )
                
                # 使用字体缓存
                title_font = FONT_CACHE.get('title', ImageFont.load_default())
                content_font = FONT_CACHE.get('content', ImageFont.load_default())
                small_font = FONT_CACHE.get('small', ImageFont.load_default())
                
                # 处理头像
                avatar_size = 80
                avatar_x = 20
                avatar_y = 60
                
                # 绘制头像占位圆
                draw.ellipse(
                    [(avatar_x, avatar_y), (avatar_x + avatar_size, avatar_y + avatar_size)],
                    fill="#e9ecef"
                )
                
                # 使用缓存的头像（缓存键与写入端一致：对象 id）
                img_a = avatar_cache.get(str(id(item)))
                if img_a:
                    # 铺满裁切并裁剪为圆形
                    img_a = _fit_cover(img_a, (avatar_size, avatar_size), Image.Resampling.BILINEAR)
                    # 创建圆形蒙版
                    mask = Image.new('L', (avatar_size, avatar_size), 0)
                    mask_draw = ImageDraw.Draw(mask)
                    mask_draw.ellipse([(0, 0), (avatar_size, avatar_size)], fill=255)
                    # 粘贴头像
                    card.paste(img_a, (avatar_x, avatar_y), mask)
                
                # 顶部昵称（居中）
                norm = _normalize_card_item(item)
                name = norm['name']
                name_width = draw.textlength(name, font=title_font)
                name_x = (card_width - name_width) // 2
                name_y = 20
                draw.text((name_x, name_y), name, font=title_font, fill="#333333")
                
                # 右侧信息区域
                info_x = avatar_x + avatar_size + 15
                info_y = 60

                _draw_card_info_lines(draw, info_x, info_y, norm, content_font, small_font, max_width=100)
                
                # 底部水印
                footer_text = "VDS API - starfure"
                footer_width = draw.textlength(footer_text, font=small_font)
                footer_x = (card_width - footer_width) // 2
                footer_y = card_height - 20
                draw.text((footer_x, footer_y), footer_text, font=small_font, fill="#999999")
                
                return card
            except Exception as e:
                logger.error(f"生成卡片失败：{e}")
                return None
        
        # 并行生成卡片
        tasks = [generate_card_with_cache(item) for item in items]
        results = await asyncio.gather(*tasks)
        for card in results:
            if card:
                cards.append(card)
        
        # 创建拼图画布
        collage = Image.new('RGBA', (collage_width, collage_height), (255, 255, 255, 255))
        
        # 排列卡片
        for i, card in enumerate(cards):
            # 计算卡片位置
            row = i // columns
            col = i % columns
            x = col * (card_width + gap)
            y = row * (card_height + gap)
            collage.paste(card, (x, y))
        
        logger.info(f"多用户拼图生成完成 | 实际生成卡片数：{len(cards)} | 拼图尺寸：{collage_width}x{collage_height}")
        return collage
        
    except Exception as e:
        logger.error(f"生成多用户拼图失败：{e}", exc_info=True)
        return None


# 异步版本的图片生成函数
async def async_generate_user_info_image(item: dict) -> Optional[Image.Image]:
    """异步生成单用户信息卡片图片
    
    Args:
        item: 用户数据字典
    
    Returns:
        生成的图片对象，失败返回 None
    """
    return await generate_user_info_image(item)


async def async_generate_users_collage(items: list) -> Optional[Image.Image]:
    """异步生成多用户拼图图片
    
    Args:
        items: 用户数据列表
    
    Returns:
        生成的拼图图片，失败返回 None
    """
    return await generate_users_collage(items)


STATS_PER_PAGE = 2000  # 每页统计条数（stats_pager 分页会话与本函数共用）


async def generate_species_stats_image(
    species_list: list,
    total: int,
    title: str = "CYX-BOT 物种统计",
    page: Optional[int] = None,
) -> Optional[List[Image.Image]]:
    """生成统计图片（四列布局，带竖向分割线）

    每页 STATS_PER_PAGE 条；page 为 None 时生成全部分页，
    指定 page（从 1 开始）时只生成该页，供统计分页按需重绘。
    """
    try:
        total_species = len(species_list)
        all_starts = list(range(0, total_species, STATS_PER_PAGE))
        if not all_starts:
            return []
        total_pages = len(all_starts)
        if page is not None:
            if not 1 <= page <= total_pages:
                return None
            all_starts = all_starts[page - 1 : page]
        images = []

        # 全局不再做任何数量截断
        for page_idx, page_start in enumerate(all_starts, 1):
            page_no = page if page is not None else page_idx
            page_data = species_list[page_start : page_start + STATS_PER_PAGE]
            page_len = len(page_data)

            # 画布基础配置
            width = 1000
            row_height = 25
            header_height = 80
            footer_height = 50
            columns = 4
            column_width = width // columns
            column_padding = 15

            # 计算当前页行数
            rows_needed = (page_len + columns - 1) // columns
            actual_height = header_height + rows_needed * row_height + footer_height

            # 创建画布
            image = Image.new('RGBA', (width, actual_height), (255, 255, 255, 255))
            draw = fallback_draw(image)

            title_font = FONT_CACHE.get('title', ImageFont.load_default())
            content_font = FONT_CACHE.get('small', ImageFont.load_default())

            # 绘制标题与页码
            title_text = title
            title_width = draw.textlength(title_text, font=title_font)
            draw.text(((width - title_width) // 2, 20), title_text, font=title_font, fill="#333333")
            page_text = f"第 {page_no}/{total_pages} 页"
            page_w = draw.textlength(page_text, font=content_font)
            draw.text((width - page_w - 15, 25), page_text, font=content_font, fill="#999999")

            # 顶部分割线
            draw.line([(0, header_height - 10), (width, header_height - 10)], fill="#e9ecef", width=1)
            # 竖线分割
            for col in range(1, columns):
                x = col * column_width
                draw.line([(x, header_height), (x, actual_height - footer_height)], fill="#e9ecef", width=1)

            # 绘制当前页数据
            start_y = header_height
            for row in range(rows_needed):
                row_y = start_y + row * row_height
                for col in range(columns):
                    idx_in_page = row * columns + col
                    if idx_in_page >= page_len:
                        break

                    real_num = page_start + idx_in_page + 1
                    col_s = col * column_width + column_padding
                    col_e = (col + 1) * column_width - column_padding

                    sp = page_data[idx_in_page]
                    name = sp.get("species", "未知")
                    cnt = sp.get("count", 0)

                    show_text = f"{real_num}. {name}"
                    if len(show_text) > 20:
                        show_text = show_text[:17] + "..."
                    draw.text((col_s, row_y), show_text, font=content_font, fill="#333333")

                    cnt_str = str(cnt)
                    cnt_w = draw.textlength(cnt_str, font=content_font)
                    draw.text((col_e - cnt_w, row_y), cnt_str, font=content_font, fill="#333333")

            # 底部水印
            footer_text = "VDS API - starfure"
            foot_w = draw.textlength(footer_text, font=content_font)
            draw.text(((width - foot_w) // 2, actual_height - 30), footer_text, font=content_font, fill="#999999")

            images.append(image)

        logger.info(f"物种统计分页完成 | 共{total_species}条 | 分为{len(images)}页")
        return images

    except Exception as e:
        logger.error(f"生成物种统计图片失败：{e}", exc_info=True)
        return None