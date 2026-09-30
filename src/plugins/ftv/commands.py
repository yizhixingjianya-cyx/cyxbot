from util import cmdbuild, finish_text, is_goqbot, markdown_message
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import MessageEvent, Message, MessageSegment
from nonebot.params import CommandArg
from nonebot.typing import T_State
from io import BytesIO
from typing import Any
from nonebot.exception import FinishedException
from .api import furtv_api, check_api_grant
from .config import get_ftv_config
from .image_generator import generate_profile_image, generate_user_info_image, generate_users_collage, generate_species_stats_image
from .token_manager import token_manager
from .cache_manager import get_cache_manager
from .user_select import resolve_user_fallback
from .card_pager import publish_cards
from .stats_pager import publish_stats
from .image_host import host_image, image_markdown, md_display_size
from .gathering_select import publish_gatherings, format_gathering_detail
from datetime import datetime
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot import get_bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent


def format_user_info(item: dict) -> str:
    """格式化用户信息为文本
    
    Args:
        item: 用户数据字典
    
    Returns:
        格式化后的文本
    """
    # 基本信息
    name = item.get('nickname', item.get('username', '未知'))
    username = item.get('username', '')
    species = item.get('fursuit_species', '未知物种')
    location = item.get('location', '未知地区')
    
    # 构建文本
    text = f"{name}\n"
    
    # 添加用户名（如果有且与昵称不同）
    if username and username != name:
        text += f"ID: {username}\n"
    
    # 添加物种
    text += f"物种：{species}\n"
    
    # 添加地区
    text += f"地区：{location}\n"
    
    # 添加兽装制作人（如果有）
    maker = item.get('fursuit_maker')
    if maker:
        text += f"兽装制作：{maker}\n"
    
    # 添加浏览量（如果有）
    view_count = item.get('view_count')
    if view_count is not None:
        text += f"浏览：{view_count}\n"
    
    # 添加简介（如果有）
    introduction = item.get('introduction')
    if introduction:
        text += f"简介：{introduction}\n"
    
    # 添加联系方式（如果有）
    contact_info = item.get('contact_info')
    if contact_info:
        qq = contact_info.get('qq')
        if qq:
            text += f"QQ: {qq}\n"
    
    # 添加创建时间（转换为日期）
    created_at = item.get('created_at')
    if created_at:
        try:
            # 解析 ISO 8601 格式
            dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            date_str = dt.strftime('%Y-%m-%d')
            text += f"加入：{date_str}\n"
        except:
            pass
    
    return text


def format_user_info_md(item: dict) -> str:
    """用户信息的 Markdown 版本（goqbot 富文本用）"""
    # 基本信息
    name = item.get('nickname', item.get('username', '未知'))
    username = item.get('username', '')
    species = item.get('fursuit_species', '未知物种')
    location = item.get('location', '未知地区')

    # 构建文本
    lines = [f"**{name}**"]

    # 添加用户名（如果有且与昵称不同）
    if username and username != name:
        lines.append(f"`ID: {username}`")

    # 添加物种
    lines.append(f"**物种**：{species}")

    # 添加地区
    lines.append(f"**地区**：{location}")

    # 添加兽装制作人（如果有）
    maker = item.get('fursuit_maker')
    if maker:
        lines.append(f"**兽装制作**：{maker}")

    # 添加浏览量（如果有）
    view_count = item.get('view_count')
    if view_count is not None:
        lines.append(f"**浏览**：{view_count}")

    # 添加简介（如果有）
    introduction = item.get('introduction')
    if introduction:
        lines.append(f"**简介**：{introduction}")

    # 添加联系方式（如果有）
    contact_info = item.get('contact_info')
    if contact_info:
        qq = contact_info.get('qq')
        if qq:
            lines.append(f"**QQ**：{qq}")

    # 添加创建时间（转换为日期）
    created_at = item.get('created_at')
    if created_at:
        try:
            # 解析 ISO 8601 格式
            dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            date_str = dt.strftime('%Y-%m-%d')
            lines.append(f"**加入**：{date_str}")
        except:
            pass

    return "\n".join(lines)


async def send_image_with_text(cmd, bot: Bot, image_url: str = None, image_bytes: BytesIO = None, text: str = "", title: str = ""):
    """发送图片和文本（图 + 文合并成同一条消息）

    按 goqbot「单图快路径」：整条消息恰好是「若干文本 + 1 张图片」且不含 markdown 时，
    所有文本会并入图片的富媒体消息，一个气泡（文字在上、图在下），不再拆成两条。
    因此这里把文本段放在图片段**之前**，且不使用 markdown 段。

    Args:
        cmd: 命令处理器
        image_url: 图片 URL
        image_bytes: 图片字节流
        text: 文本内容
        title: 标题（用于合并转发）
    """
    if image_url:
        segs = []
        if text:
            segs.append(MessageSegment.text(text))
        segs.append(MessageSegment.image(image_url))
        return await cmd.finish(Message(segs))
    elif image_bytes:
        segs = []
        if text:
            segs.append(MessageSegment.text(text))
        segs.append(MessageSegment.image(image_bytes))
        return await cmd.finish(Message(segs))
    else:
        return await finish_text(cmd, bot, text)


async def send_images_with_text(cmd, bot: Bot, images: list, text: str = ""):
    """把**多张**图塞进同一个消息框（图 + 文合并成一条消息）

    - 只有一张：退化成 ``send_image_with_text`` 的单图快路径；
    - goqbot 且有公网图床：markdown 里连写多个 ``![说明 #宽 #高](url)``，
      一条消息贴完所有图（显示尺寸按官方建议压小，图床上仍是原图）；
    - 其它情况：一条普通消息挂多个 image 段。

    无论哪条路都只发**一条**消息，不会再一页一条刷屏。

    Args:
        cmd: 命令处理器
        bot: Bot 实例
        images: PIL 图片列表
        text: 附在图片上的说明文字
    """
    images = [img for img in (images or []) if img is not None]
    if not images:
        return await finish_text(cmd, bot, text) if text else None

    if len(images) == 1:
        buf = BytesIO()
        images[0].save(buf, format="PNG")
        buf.seek(0)
        return await send_image_with_text(cmd, bot, image_bytes=buf, text=text)

    # 首选 markdown：一条消息里连贴多张图（仅 goqbot 支持 markdown 段）
    if await is_goqbot(bot):
        urls = [host_image(img) for img in images]
        if all(urls):
            blocks = [
                image_markdown(url, "统计图", *md_display_size(img.width, img.height))
                for url, img in zip(urls, images)
            ]
            body = "\n\n".join(blocks)
            content = f"{text}\n\n{body}" if text else body
            return await cmd.finish(markdown_message(content))

    # 回落：普通消息里挂多个图片段
    segs = []
    if text:
        segs.append(MessageSegment.text(text))
    for img in images:
        buf = BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        segs.append(MessageSegment.image(buf))
    return await cmd.finish(Message(segs))


async def send_forward_message(cmd, bot: Bot, event: MessageEvent, items: list, title: str = "", detail: bool = True):
    """发送用户信息图片（单用户卡片、分页拼图或整张拼图）

    Args:
        cmd: 命令处理器
        event: 消息事件
        items: 列表项，每项包含用户数据
        title: 标题
        detail: 点数字时是否直接进入档案详情（档案列表 True；崽崽等角色列表传 False）
    """
    if not items:
        return

    # 提取用户数据
    user_data_list = []
    for item in items:
        # 从 items 中提取用户数据
        # 这里假设 items 中的每个元素包含用户数据
        # 实际使用时需要根据具体数据结构调整
        user_data = item.get('user_data') or item
        user_data_list.append(user_data)

    # goqbot：多结果每页 6 个 + 数字/翻页按钮；单条档案也给「查看详细档案」按钮（内部已 finish）
    if await publish_cards(cmd, bot, event, user_data_list, title, detail=detail):
        return

    # 生成图片
    if len(user_data_list) == 1:
        # 单用户：生成卡片
        img = await generate_user_info_image(user_data_list[0])
    else:
        # 多用户：生成拼图
        img = await generate_users_collage(user_data_list)
    
    # 发送图片
    if img:
        img_bytes = BytesIO()
        img.save(img_bytes, format='PNG')
        img_bytes.seek(0)
        await send_image_with_text(cmd, bot, image_bytes=img_bytes, text="")
    else:
        # 如果图片生成失败，发送文本信息
        plain = f"{title}\n\n"
        md = f"**{title}**\n\n"
        for i, item in enumerate(items, 1):
            plain += f"{i}. {item.get('text') or format_user_info(item)}\n\n"
            md += f"{i}. {item.get('md_text') or item.get('text') or format_user_info_md(item)}\n\n"
        await finish_text(cmd, bot, plain, md)


# 初始化配置
config = get_ftv_config()


# 热门推荐命令
popular_cmd = cmdbuild("ftv.popular", "热门").alias("热门推荐").priority(5).block().build()


@popular_cmd.handle()
async def _(bot: Bot, event: MessageEvent, state: T_State, arg: Message = CommandArg()):
    limit = 10
    if arg.extract_plain_text().strip():
        try:
            limit = int(arg.extract_plain_text().strip())
            limit = max(1, min(limit, 50))
        except ValueError:
            pass
    
    try:
        data = await furtv_api.get_popular(limit)
        
        # 支持多种返回结构
        popular_list = None
        if 'users' in data:
            popular_list = data.get('users', [])
        elif 'fursuits' in data:
            popular_list = data.get('fursuits', [])
        elif 'fursuit' in data:
            popular_list = [data.get('fursuit', {})]
        elif 'data' in data:
            popular_list = data.get('data', [])
        
        if not popular_list:
            await finish_text(popular_cmd, bot, "暂无热门推荐")
        
        # 构建消息列表（直接传递用户数据）
        items = []
        for item in popular_list:
            items.append(item)
        
        # 使用新的图片生成函数
        await send_forward_message(popular_cmd, bot, event, items, "推荐档案")
        return await popular_cmd.finish()
        
    except PermissionError as e:
        await finish_text(popular_cmd, bot, f"端点权限故障，请联系 bot 管理员：{str(e)}")
    except FileNotFoundError as e:
        await finish_text(popular_cmd, bot, f"什么都没有找到呢UwU")
    except FinishedException:
        # 已经发送过消息，无需处理
        pass
    except Exception as e:
        await finish_text(popular_cmd, bot, f"啊呀，似乎出问题了呢：{str(e)}")


# 随机推荐命令
random_cmd = cmdbuild("ftv.random", "随机").alias("随机档案").priority(5).block().build()


@random_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    count = 1
    if arg.extract_plain_text().strip():
        try:
            count = int(arg.extract_plain_text().strip())
            count = max(1, min(count, 20))
        except ValueError:
            pass
    
    try:
        data = await furtv_api.get_random(count)
        
        # 支持多种返回结构
        random_list = None
        if 'fursuit' in data:
            # 单个 fursuit 对象，转为列表
            random_list = [data.get('fursuit', {})]
        elif 'data' in data:
            random_list = data.get('data', [])
        
        if not random_list:
            await finish_text(random_cmd, bot, "暂无随机推荐")
        
        # 构建消息列表（直接传递用户数据）
        items = []
        for item in random_list:
            items.append(item)
        
        # 使用新的图片生成函数
        await send_forward_message(random_cmd, bot, event, items, "随机档案")
        return await random_cmd.finish()
        
    except PermissionError as e:
        await finish_text(random_cmd, bot, f"权限不足：{str(e)}")
    except FileNotFoundError as e:
        await finish_text(random_cmd, bot, f"什么都没有找到呢UwU")
    except FinishedException:
        pass
    except Exception as e:
        await finish_text(random_cmd, bot, f"获取失败：{str(e)}")


# 物种搜索命令
species_cmd = cmdbuild("ftv.species", "物种").alias("物种搜索").priority(5).block().build()


@species_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    args = arg.extract_plain_text().strip().split()
    if not args:
        await finish_text(species_cmd, bot, "请提供物种名称，例如：.物种 狼")
    
    species = args[0]
    page = 1
    if len(args) > 1:
        try:
            page = int(args[1])
        except ValueError:
            pass
    
    try:
        data = await furtv_api.search_by_species(species, page=page)
        
        # 支持多种返回结构
        result_list = None
        if 'users' in data:
            result_list = data.get('users', [])
        elif 'data' in data:
            result_list = data.get('data', [])
        
        if not result_list:
            await finish_text(species_cmd, bot, f"什么都没有找到呢UwU")
        
        # 构建消息列表（直接传递用户数据）
        items = []
        for i, item in enumerate(result_list[:10], 1):
            items.append(item)
        
        # 使用新的图片生成函数
        await send_forward_message(species_cmd, bot, event, items, f"物种'{species}'搜索")
        return await species_cmd.finish()
        
    except PermissionError as e:
        await finish_text(species_cmd, bot, f"端点权限不足，请联系 bot 管理员{str(e)}")
    except FileNotFoundError as e:
        await finish_text(species_cmd, bot, f"什么都没有找到呢UwU")
    except FinishedException:
        pass
    except Exception as e:
        await finish_text(species_cmd, bot, f"搜索失败：{str(e)}")


# 关键词搜索命令
search_cmd = cmdbuild("ftv.search", "搜索").alias("兽搜索").priority(5).block().build()


@search_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    args = arg.extract_plain_text().strip().split()
    if not args:
        await finish_text(search_cmd, bot, "请提供搜索关键词，例如：.搜索 毛毛")
    
    q = args[0]
    search_type = 'all'
    if len(args) > 1:
        search_type = args[1]
    
    try:
        data = await furtv_api.search(q, type=search_type)
        
        # 支持多种返回结构：{'users': [...]} 或 {'data': [...]}
        result_list = None
        if 'users' in data:
            result_list = data.get('users', [])
        elif 'data' in data:
            result_list = data.get('data', [])
        
        if not result_list:
            await finish_text(search_cmd, bot, f"什么都没有找到呢UwU")
        
        # 构建消息列表（直接传递用户数据）
        items = []
        for i, item in enumerate(result_list[:10], 1):
            items.append(item)
        
        # 使用新的图片生成函数
        await send_forward_message(search_cmd, bot, event, items, f"关键词'{q}'搜索")
        return await search_cmd.finish()
        
    except PermissionError as e:
        await finish_text(search_cmd, bot, f"权限不足：{str(e)}")
    except FileNotFoundError as e:
        await finish_text(search_cmd, bot, f"什么都没有找到呢UwU")
    except FinishedException:
        pass
    except Exception as e:
        await finish_text(search_cmd, bot, f"搜索失败：{str(e)}")


# 热门地区命令
locations_cmd = cmdbuild("ftv.locations", "热门地区").priority(5).block().build()


@locations_cmd.handle()
async def _(bot: Bot, event: MessageEvent):
    try:
        raw = await furtv_api.get_popular_locations()

        # 拆掉 success/data 外层包装后取列表，支持多种返回结构
        inner = raw.get('data') if isinstance(raw, dict) and isinstance(raw.get('data'), (dict, list)) else raw
        sources = [s for s in (inner, raw) if isinstance(s, (dict, list))]

        locations = []
        for source in sources:
            if isinstance(source, list):
                locations = source
            else:
                for key in ('popular_cities', 'popular_provinces', 'locations', 'items'):
                    value = source.get(key)
                    if isinstance(value, list) and value:
                        locations = value
                        break
            if locations:
                break

        if not locations:
            await finish_text(locations_cmd, bot, "暂无热门地区数据")

        # 地区名与档案数（兼容省份/城市两种格式）
        rows = []
        for loc in locations:
            if not isinstance(loc, dict):
                continue
            province = loc.get('province') or ''
            city = loc.get('city') or ''
            name = f"{province}{city}" or loc.get('name') or '未知'
            rows.append((name, loc.get('count', 0)))

        # 总用户数：优先取接口字段，取不到时按各地区档案数求和（接口不返回该字段时不再恒为 0）
        total_users = 0
        for source in sources:
            if not isinstance(source, dict):
                continue
            for key in ('total_users', 'totalUsers', 'total', 'total_count'):
                value = source.get(key)
                if isinstance(value, (int, float)) and value > 0:
                    total_users = int(value)
                    break
            if total_users:
                break
        if not total_users:
            total_users = sum(count for _, count in rows if isinstance(count, (int, float)))

        lines = [f"地区总数：{len(rows)} 个", f"总用户数：{total_users} 个", ""]
        lines.extend(f"{i}. {name} {count}" for i, (name, count) in enumerate(rows, 1))
        body = "\n".join(lines).rstrip("\n")
        md = f"**CYX-BOT 热门地区统计**\n\n```text\n{body}\n```"
        await finish_text(locations_cmd, bot, body, md)
    except FinishedException:
        pass
    except Exception as e:
        await finish_text(locations_cmd, bot, f"获取失败：{str(e)}")


species_list_cmd = cmdbuild("ftv.species_list", "物种列表").alias("物种统计").priority(5).block().build()
@species_list_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    try:
        data = await furtv_api.get_species_list()
        
        species_list = data.get('species', []) or data.get('data', [])
        
        if not species_list:
            await finish_text(species_list_cmd, bot, "暂无物种数据")
        
        total_profiles = sum(s.get('count', 0) for s in species_list)

        # 带 all 参数：一次发完整列表；否则 goqbot 多页时走分页（一次一页 + 按钮）
        if arg.extract_plain_text().strip().lower() != "all":
            if await publish_stats(species_list_cmd, bot, event, species_list, total_profiles):
                return

        # 生成图片（现在会返回多张）
        images = await generate_species_stats_image(species_list, total_profiles)
        text = f"兽频道 物种统计总览\n"
        text += f"物种总数：{len(species_list)} 种\n"
        text += f"档案总数：{total_profiles} 个"

        # 发送所有图片：多页塞进同一条消息，不再一页一条
        if images:
            await send_images_with_text(species_list_cmd, bot, images, text=text)

        await species_list_cmd.finish()
    except FinishedException:
        raise
    except Exception as e:
        await finish_text(species_list_cmd, bot, f"获取失败：{str(e)}")
    

# 学校搜索命令
school_cmd = cmdbuild("ftv.school", "学校").alias("学校搜索").priority(5).block().build()


@school_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    query = arg.extract_plain_text().strip()
    if not query:
        await finish_text(school_cmd, bot, "请提供学校名称，例如：.学校 北京大学")
    
    try:
        data = await furtv_api.search_schools(query)
        
        # 支持多种返回结构
        schools = None
        if 'schools' in data:
            schools = data.get('schools', [])
        elif 'data' in data:
            schools = data.get('data', [])
        
        if not schools:
            await finish_text(school_cmd, bot, f"未找到学校 '{query}'")
        
        lines = []
        for i, school in enumerate(schools[:10], 1):
            name = school.get('name') or '未知'
            short_name = school.get('short_name')
            lines.append(f"{i}. {name}" + (f"（{short_name}）" if short_name else ""))
            school_id = school.get('id')
            if school_id not in (None, ""):
                lines.append(f"   ID：{school_id}")
            location = school.get('location')
            if location:
                lines.append(f"   地区：{location}")
            school_type = school.get('type')
            if school_type:
                lines.append(f"   类型：{school_type}")
            student_count = school.get('student_count')
            lines.append(f"   用户：{'未知' if student_count is None else student_count}")

        body = "\n".join(lines)
        result = f"学校搜索结果：'{query}'\n\n{body}"
        md = f"**学校搜索结果：「{query}」**\n\n```text\n{body}\n```"

        await finish_text(school_cmd, bot, result, md)
    except FinishedException:
        pass
    except FileNotFoundError as e:
        await finish_text(school_cmd, bot, f"什么都没有找到呢UwU")
    except Exception as e:
        await finish_text(school_cmd, bot, f"搜索失败：{str(e)}")


# 用户资料命令
user_profile_cmd = cmdbuild("ftv.profile", "兽档案").alias("用户信息").priority(5).block().build()


async def show_user_profile(cmd: Any, bot: Bot, event: MessageEvent, username: str) -> bool:
    """按用户名展示兽档案；返回 False 表示查不到该用户。"""
    try:
        data = await furtv_api.get_user_profile(username)
    except FileNotFoundError:
        return False

    # 支持多种返回结构
    user = None
    if 'user' in data:
        user = data.get('user', {})
    elif 'data' in data:
        user = data.get('data', {})

    if not user:
        return False

    # 使用图片生成器生成档案图片
    profile_data = {
        "id": user.get('id', username),
        "nickname": user.get('nickname', '未知昵称'),
        "username": user.get('username', username),
        "fursuit_species": user.get('fursuit_species', '未知'),
        "fursuit_birthday": user.get('fursuit_birthday', '无'),
        "fursuit_maker": user.get('fursuit_maker', '未知'),
        "location": user.get('location', '未知'),
        "introduction": user.get('introduction', '无'),
        "interests": user.get('interests', [])
    }

    # 获取图片 URL（使用正确的字段）
    avatar_url = user.get('avatar_url', 'https://picsum.photos/id/101/200/200')
    vertical_img_url = user.get('showcase_portrait', 'https://picsum.photos/id/100/800/1600')  # 竖向 1080*1920
    horizontal_img_url = user.get('showcase_landscape', 'https://picsum.photos/id/102/700/250')  # 横向 1980*1080
    showcase_other_url = user.get('showcase_other', 'https://picsum.photos/id/103/400/400')  # 方形 1000*1000

    # 生成图片（带缓存）
    img = await generate_profile_image(
        vertical_img_url=vertical_img_url,
        avatar_url=avatar_url,
        horizontal_img_url=horizontal_img_url,
        showcase_other_url=showcase_other_url,
        profile_data=profile_data,
        title_text="CYX-bot 兽频道档案"
    )

    # 文字内容：markdown 的 text 代码块（末尾多余换行去掉，避免代码块内留空行）
    info_text = format_user_info(user).rstrip("\n")
    text = f"用户档案：{username}\n\n{info_text}"
    md = f"**用户档案：{username}**\n\n```text\n{info_text}\n```"

    if img:
        # 图 + 文字合并成同一条消息：
        # 有公网图床时把图片以 URL 嵌进 markdown（文字仍用代码块排版）；
        # 没有图床时回落「单图快路径」（纯文本 + 图片，一个气泡）。
        url = host_image(img)
        if url:
            # 图床上是原图；只把回填给 QQ 的显示尺寸按官方建议压小（不重编码）
            await cmd.finish(
                markdown_message(
                    f"{image_markdown(url, '档案图', *md_display_size(img.width, img.height))}"
                    f"\n\n{md}"
                )
            )
            return True
        img_bytes = BytesIO()
        img.save(img_bytes, format='PNG')
        img_bytes.seek(0)
        await cmd.finish(
            Message([MessageSegment.text(f"{text}\n"), MessageSegment.image(img_bytes)])
        )
        return True
    await finish_text(cmd, bot, text, md)
    return True


@user_profile_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    username = arg.extract_plain_text().strip()
    if not username:
        await finish_text(user_profile_cmd, bot, "请提供用户名，例如：.用户资料 example")

    try:
        if await show_user_profile(user_profile_cmd, bot, event, username):
            return
        if not await resolve_user_fallback(
            user_profile_cmd, bot, event, username, show_user_profile
        ):
            await finish_text(user_profile_cmd, bot, f"什么都没有找到呢UwU")
    except FinishedException:
        pass
    except Exception as e:
        await finish_text(user_profile_cmd, bot, f"获取失败：{str(e)}")


# 用户角色命令
user_characters_cmd = cmdbuild("ftv.characters", "崽崽").alias("角色列表").priority(5).block().build()


async def show_user_characters(cmd: Any, bot: Bot, event: MessageEvent, username: str) -> bool:
    """按用户名展示角色列表；返回 False 表示查不到该用户或该用户没有角色。"""
    try:
        data = await furtv_api.get_user_characters(username)
    except FileNotFoundError:
        return False

    # 支持多种返回结构
    characters = None
    if 'characters' in data:
        characters = data.get('characters', [])
    elif 'data' in data:
        characters = data.get('data', [])

    if not characters:
        return False

    # 构建消息列表：直接传角色结构化数据，图片渲染层与文本兜底都可用
    items = []
    for char in characters:
        # ==============================================
        # 🔥 对齐接口字段：name/images/species/gender/birthday/worldview
        # ==============================================
        name = char.get('name', '未知名称')
        gender = char.get('gender')
        species = char.get('species')
        birthday = char.get('birthday')
        worldview = char.get('worldview')

        # 构建文本兜底（图片失败时发送）
        text = f"【角色】{name}\n"
        if gender:
            text += f"性别：{gender}\n"
        if species:
            text += f"物种：{species}\n"
        if birthday:
            text += f"生日：{birthday}\n"
        if worldview:
            if len(worldview) > 50:
                worldview = worldview[:47] + '...'
            text += f"设定：{worldview}\n"

        # 构建 Markdown 兜底（goqbot 富文本用）
        md_text = f"**【角色】{name}**\n"
        if gender:
            md_text += f"性别：{gender}\n"
        if species:
            md_text += f"物种：{species}\n"
        if birthday:
            md_text += f"生日：{birthday}\n"
        if worldview:
            md_text += f"设定：{worldview}\n"

        entry = dict(char)          # 保留完整角色数据（images/name/species 等）
        entry['text'] = text        # 文本兜底
        entry['md_text'] = md_text  # Markdown 兜底（goqbot 富文本用）
        items.append(entry)

    # 使用合并转发或单条消息（崽崽是角色列表，点数字不进入档案详情）
    await send_forward_message(cmd, bot, event, items, f"{username}的角色", detail=False)
    return True


@user_characters_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    username = arg.extract_plain_text().strip()
    if not username:
        await finish_text(user_characters_cmd, bot, "请提供用户名，例如：.用户角色 example")

    try:
        if await show_user_characters(user_characters_cmd, bot, event, username):
            return
        if not await resolve_user_fallback(
            user_characters_cmd, bot, event, username, show_user_characters
        ):
            await finish_text(user_characters_cmd, bot, f"什么都没有找到呢UwU")
    except FinishedException:
        pass
    except Exception as e:
        await finish_text(user_characters_cmd, bot, f"获取失败：{str(e)}")


# 聚会统计命令
gatherings_stats_cmd = cmdbuild("ftv.gatherings_stats", "聚会统计").priority(5).block().build()


@gatherings_stats_cmd.handle()
async def _(bot: Bot, event: MessageEvent):
    try:
        data = await furtv_api.get_gatherings_yearly_stats()
        
        # 支持多种返回结构
        stats = None
        if 'data' in data:
            stats = data.get('data', {})
        elif 'total' in data:
            # 直接返回 total 的情况
            stats = {'total': data.get('total', 0)}
        
        if not stats:
            await finish_text(gatherings_stats_cmd, bot, "获取聚会统计失败")
        
        result = "聚会年度统计\n\n"
        result += f"今年聚会总数：{stats.get('total', 0)}\n"
        result += f"参与人数：{stats.get('participants', 0)}\n"

        md = "**聚会年度统计**\n\n"
        md += f"今年聚会总数：**{stats.get('total', 0)}**\n"
        md += f"参与人数：**{stats.get('participants', 0)}**\n"
        
        await finish_text(gatherings_stats_cmd, bot, result, md)
    except FinishedException:
        pass
    except FileNotFoundError as e:
        await finish_text(gatherings_stats_cmd, bot, f"什么都没有找到呢UwU")
    except Exception as e:
        await finish_text(gatherings_stats_cmd, bot, f"获取失败：{str(e)}")


# 本月聚会命令
monthly_gatherings_cmd = cmdbuild("ftv.monthly_gatherings", "本月聚会").priority(5).block().build()


@monthly_gatherings_cmd.handle()
async def _(bot: Bot, event: MessageEvent):
    from datetime import datetime
    now = datetime.now()
    year = now.year
    month = now.month
    
    try:
        data = await furtv_api.get_gatherings_monthly(year, month)
        
        # 支持多种返回结构
        gatherings = None
        if 'gatherings' in data:
            gatherings = data.get('gatherings', [])
        elif 'data' in data:
            data_content = data.get('data')
            # 确保 data 是列表而不是字典
            if isinstance(data_content, list):
                gatherings = data_content
            elif isinstance(data_content, dict) and 'gatherings' in data_content:
                # data 是字典，包含 gatherings 字段
                gatherings = data_content.get('gatherings', [])
            else:
                gatherings = []
        
        if not gatherings:
            await finish_text(monthly_gatherings_cmd, bot, f"{year}年{month}月暂无聚会")

        # 多个聚会：goqbot 下只发简要列表（名字/地点/日期），点数字按钮查看完整详情
        if await publish_gatherings(monthly_gatherings_cmd, bot, event, gatherings, month=month):
            return

        result = f"{year}年{month}月聚会列表\n\n"
        md = f"**{year}年{month}月聚会列表**\n\n"
        for i, gathering in enumerate(gatherings[:10], 1):
            title = gathering.get('title', '未知')
            day = gathering.get('day', '未知日期')
            location = gathering.get('locationPublic', gathering.get('location', '未知地点'))
            description = gathering.get('description', '')
            
            # 截断过长的描述
            if description and len(description) > 50:
                description = description[:47] + '...'
            
            result += f"{i}. {title}\n"
            result += f"   日期：{month}月{day}日\n"
            result += f"   地点：{location}\n"
            if description:
                result += f"   简介：{description}\n"
            result += "\n"

            md += f"**{i}. {title}**\n"
            md += f"日期：{month}月{day}日\n"
            md += f"地点：{location}\n"
            if description:
                md += f"简介：{description}\n"
            md += "\n"
        
        await finish_text(monthly_gatherings_cmd, bot, result, md)
    except FinishedException:
        pass
    except FileNotFoundError as e:
        await finish_text(monthly_gatherings_cmd, bot, f"什么都没有找到呢UwU")
    except Exception as e:
        await finish_text(monthly_gatherings_cmd, bot, f"获取失败：{str(e)}")





# 聚会详情命令
gathering_detail_cmd = cmdbuild("ftv.gathering_detail", "聚会详情").priority(5).block().build()


@gathering_detail_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    gathering_id = arg.extract_plain_text().strip()
    if not gathering_id:
        await finish_text(gathering_detail_cmd, bot, "请提供聚会 ID，例如：.聚会详情 12345")
    
    try:
        data = await furtv_api.get_gathering_detail(gathering_id)
        
        # 支持多种返回结构
        gathering = None
        if 'gathering' in data:
            gathering = data.get('gathering', {})
        elif 'data' in data:
            gathering = data.get('data', {})
        
        if not gathering:
            await finish_text(gathering_detail_cmd, bot, f"未找到聚会 ID '{gathering_id}'")

        result, md = format_gathering_detail(gathering)
        await finish_text(gathering_detail_cmd, bot, result, md)
    except FinishedException:
        pass
    except FileNotFoundError as e:
        await finish_text(gathering_detail_cmd, bot, f"什么都没有找到呢UwU")
    except Exception as e:
        await finish_text(gathering_detail_cmd, bot, f"获取失败：{str(e)}")



# 清除缓存命令
clear_cache_cmd = cmdbuild("ftv.clear_cache", "清除缓存").priority(5).block().build()


@clear_cache_cmd.handle()
async def _(bot: Bot, event: MessageEvent):
    """清除缓存文件"""
    import os
    import shutil
    from pathlib import Path
    
    # 缓存目录路径
    current_file_path = os.path.abspath(__file__)
    ftv_dir = os.path.dirname(current_file_path)
    cache_dir = Path(ftv_dir) / "cache"
    image_cache_dir = cache_dir / "images"
    
    try:
        # 计算要删除的文件数量
        total_files = 0
        if image_cache_dir.exists():
            total_files = len(list(image_cache_dir.glob("*")))
        
        # 删除图片缓存目录
        if image_cache_dir.exists():
            shutil.rmtree(image_cache_dir)
        
        # 重新创建空的缓存目录
        image_cache_dir.mkdir(parents=True, exist_ok=True)
        
        await finish_text(clear_cache_cmd, bot, f"缓存清除成功，共删除 {total_files} 个文件")
    except FinishedException:
        pass
    except Exception as e:
        await finish_text(clear_cache_cmd, bot, f"清除缓存失败：{str(e)}")
