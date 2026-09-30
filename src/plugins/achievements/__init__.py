from nonebot import get_driver, require, on_command, get_plugin_config
from nonebot.plugin import PluginMetadata
from pathlib import Path
import sqlite3
from io import BytesIO
from typing import Dict, List, Optional, Set, Tuple
from datetime import datetime, date, timedelta
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.params import CommandArg
from nonebot.adapters.onebot.v11 import Message
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.log import logger

from .config import Config

# 导入新的工具模块
from util import cmdbuild, send_group_msg, send_private_msg

# 尝试导入Pillow库，用于生成成就图片
try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# 不需要加载 apscheduler，因为没有实际使用
scheduler = None

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="achievements",
    description="通用成就系统插件，支持其他插件注册和给予成就",
    usage="""\n\
    注册成就：使用register_achievement函数\n\
    给予成就：使用grant_achievement函数\n\
    检查成就：使用has_achievement函数\n\
    查询用户成就：使用get_user_achievements函数\n\
    查询所有成就：使用get_all_achievements函数\n\
    """,
    type="application",
    homepage="",
    config=Config,
)

config = get_plugin_config(Config)

# 数据文件路径
DATA_DIR = Path(__file__).parent / "data"
DB_FILE = DATA_DIR / "achievements.db"

# 确保数据目录存在
DATA_DIR.mkdir(exist_ok=True)

# 数据库连接
conn = None

# 初始化数据库连接
async def init_db():
    """初始化数据库连接和表结构"""
    global conn
    
    # 创建数据库连接
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # 创建成就表
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS achievements (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT NOT NULL,
        icon TEXT,
        hidden INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
    )
    ''')
    
    # 创建用户成就表
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS user_achievements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        achievement_id TEXT NOT NULL,
        obtained_at TEXT NOT NULL,
        FOREIGN KEY (achievement_id) REFERENCES achievements (id)
    )
    ''')
    
    # 创建用户表（用于签到和货币）
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        coins INTEGER DEFAULT 0,
        last_checkin TEXT,
        checkin_streak INTEGER DEFAULT 0
    )
    ''')
    
    # 提交事务
    conn.commit()
    cursor.close()

# 关闭数据库连接
async def close_db():
    """关闭数据库连接"""
    global conn
    if conn:
        conn.close()
        conn = None

# 执行SQL查询
async def execute_sql(sql: str, params: Tuple = (), fetchone: bool = False, fetchall: bool = False):
    """执行SQL查询"""
    global conn
    if not conn:
        await init_db()
    
    cursor = conn.cursor()
    cursor.execute(sql, params)
    
    result = None
    if fetchone:
        result = cursor.fetchone()
    elif fetchall:
        result = cursor.fetchall()
    
    conn.commit()
    cursor.close()
    return result

# 生成成就图片
async def generate_achievement_image(achievement: Dict) -> Optional[BytesIO]:
    """
    生成成就图片
    
    参数:
        achievement: 成就信息字典
    
    返回:
        Optional[BytesIO]: 生成的图片字节流，失败返回None
    """
    if not PIL_AVAILABLE or not config.achievements_image_enabled:
        return None
    
    try:
        # 图片尺寸
        width, height = 500, 100
        
        # 加载背景图片
        back_path = Path(__file__).parent / "back.png"
        if back_path.exists():
            img = Image.open(back_path).convert('RGB')
            # 调整背景图片大小以适应成就卡片
            img = img.resize((width, height))
        else:
            # 如果背景图片不存在，使用纯色背景作为备选
            img = Image.new('RGB', (width, height), color=config.achievements_image_background)
        draw = ImageDraw.Draw(img)
        
        # 尝试加载字体，使用默认字体如果失败
        try:
            font_path = Path(__file__).parent / "Cubic.ttf"
            title_font = ImageFont.truetype(str(font_path), 30)  # 调整字体大小适应标题区域
            desc_font = ImageFont.truetype(str(font_path), 20)  # 调整字体大小适应描述区域
        except IOError:
            title_font = ImageFont.load_default()
            desc_font = ImageFont.load_default()
        
        # 绘制成就图标
        icon_x1, icon_y1 = 20, 10
        icon_x2, icon_y2 = 70, 60
        icon_size = icon_y2 - icon_y1
        
        # 获取图标ID，默认为1
        icon_id = achievement.get("icon", 1)
        
        # 构造图标文件路径，支持多种格式
        icon_dir = Path(__file__).parent / "icon"
        icon_path = None
        # 尝试多种格式，按顺序查找
        for ext in ["png", "jpg", "webp"]:
            temp_path = icon_dir / f"{icon_id}.{ext}"
            if temp_path.exists():
                icon_path = temp_path
                break
        
        # 如果图标文件存在，加载并绘制
        if icon_path:
            try:
                icon_img = Image.open(icon_path).convert("RGBA")
                # 调整图标大小
                icon_img = icon_img.resize((icon_size, icon_size))
                # 绘制图标
                img.paste(icon_img, (icon_x1, icon_y1), icon_img)
            except Exception as e:
                print(f"加载图标失败: {e}")
                # 绘制默认图标
                draw.rectangle([(icon_x1, icon_y1), (icon_x2, icon_y2)], fill=config.achievements_image_title_color)
                draw.text((icon_x1 + 15, icon_y1 + 5), "🏆", font=ImageFont.truetype(str(font_path), 40), fill="white")
        else:
            # 图标文件不存在，绘制默认图标
            draw.rectangle([(icon_x1, icon_y1), (icon_x2, icon_y2)], fill=config.achievements_image_title_color)
            draw.text((icon_x1 + 15, icon_y1 + 5), "🏆", font=ImageFont.truetype(str(font_path), 40), fill="white")
        
        # 绘制成就标题（位置：左上(80,10)，右下(480,45)）
        title = achievement['name']
        title_x1, title_y1 = 80, 10
        title_x2, title_y2 = 480, 45
        title_area_width = title_x2 - title_x1
        title_area_height = title_y2 - title_y1
        
        # 调整字体大小以适应标题区域
        title_font_size = 30
        while True:
            title_font = ImageFont.truetype(str(font_path), title_font_size)
            title_bbox = draw.textbbox((0, 0), title, font=title_font)
            title_width = title_bbox[2] - title_bbox[0]
            title_height = title_bbox[3] - title_bbox[1]
            if title_width <= title_area_width and title_height <= title_area_height:
                break
            title_font_size -= 1
            if title_font_size <= 10:
                break
        
        # 居中绘制标题文字
        title_x = title_x1 + (title_area_width - title_width) // 2
        title_y = title_y1 + (title_area_height - title_height) // 2
        draw.text((title_x, title_y), title, font=title_font, fill=config.achievements_image_title_color)
        
        # 绘制成就描述（位置：左上(80,50)，右下(480,75)）
        description = achievement['description']
        desc_x1, desc_y1 = 80, 50
        desc_x2, desc_y2 = 480, 75
        desc_area_width = desc_x2 - desc_x1
        desc_area_height = desc_y2 - desc_y1
        
        # 调整字体大小以适应描述区域
        desc_font_size = 20
        while True:
            desc_font = ImageFont.truetype(str(font_path), desc_font_size)
            desc_bbox = draw.textbbox((0, 0), description, font=desc_font)
            desc_width = desc_bbox[2] - desc_bbox[0]
            desc_height = desc_bbox[3] - desc_bbox[1]
            if desc_width <= desc_area_width and desc_height <= desc_area_height:
                break
            desc_font_size -= 1
            if desc_font_size <= 10:
                break
        
        # 居中绘制描述文字
        desc_x = desc_x1 + (desc_area_width - desc_width) // 2
        desc_y = desc_y1 + (desc_area_height - desc_height) // 2
        draw.text((desc_x, desc_y), description, font=desc_font, fill=config.achievements_image_text_color)
        
        # 绘制成就ID
        achievement_id = f"成就ID: {achievement['id']}"
        id_font = ImageFont.truetype(str(font_path), 16)
        id_bbox = draw.textbbox((0, 0), achievement_id, font=id_font)
        id_x = (width - id_bbox[2] + id_bbox[0]) // 2
        id_y = 75
        draw.text((id_x, id_y), achievement_id, font=id_font, fill=config.achievements_image_text_color)
        
        # 保存图片到字节流
        img_bytes = BytesIO()
        img.save(img_bytes, format='PNG')
        img_bytes.seek(0)
        
        return img_bytes
    except Exception as e:
        print(f"生成成就图片失败: {e}")
        return None

# 注册成就
async def register_achievement(
    achievement_id: str,
    name: str,
    description: str,
    icon: Optional[int] = None,
    hidden: bool = False
) -> bool:
    """
    注册一个新成就
    
    参数:
        achievement_id: 成就唯一标识符
        name: 成就名称
        description: 成就描述
        icon: 成就图标ID（可选，默认为1）
        hidden: 是否隐藏成就（可选，默认False）
    
    返回:
        bool: 注册成功返回True，已存在返回False
    """
    logger.info(f"[成就系统] 收到成就注册请求: ID={achievement_id}, 名称={name}")
    
    # 检查成就是否已存在
    existing = await execute_sql(
        "SELECT id FROM achievements WHERE id = ?",
        (achievement_id,),
        fetchone=True
    )
    
    if existing:
        logger.warning(f"[成就系统] 成就已存在，注册失败: ID={achievement_id}, 名称={name}")
        return False
    
    # 设置默认图标ID为1
    icon_id = icon if icon is not None else 1
    
    # 插入新成就
    await execute_sql(
        "INSERT INTO achievements (id, name, description, icon, hidden, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (achievement_id, name, description, icon_id, 1 if hidden else 0, datetime.now().isoformat())
    )
    
    logger.info(f"[成就系统] 成就注册成功: ID={achievement_id}, 名称={name}, 图标ID={icon_id}, 隐藏状态={hidden}")
    return True

# 初始化用户（如果不存在）
async def init_user(user_id: str):
    """初始化用户（如果不存在）"""
    existing = await execute_sql(
        "SELECT user_id FROM users WHERE user_id = ?",
        (user_id,),
        fetchone=True
    )
    
    if not existing:
        await execute_sql(
            "INSERT INTO users (user_id, coins, checkin_streak) VALUES (?, 0, 0)",
            (user_id,)
        )

# 给予用户成就
async def grant_achievement(
    user_id: str, 
    achievement_id: str, 
    allow_duplicate: bool = False, 
    event=None, 
    matcher=None
) -> Tuple[bool, Optional[Dict], Optional[BytesIO]]:
    """
    给予用户一个成就
    
    参数:
        user_id: 用户唯一标识符
        achievement_id: 成就唯一标识符
        allow_duplicate: 是否允许重复授予（默认False）
        event: 事件对象，用于发送成就通知（可选）
        matcher: Matcher对象，用于发送成就通知（可选）
    
    返回:
        Tuple[bool, Optional[Dict], Optional[BytesIO]]: (是否成功授予, 成就信息字典, 成就图片字节流)
    """
    logger.info(f"[成就系统] 收到成就授予请求: 用户ID={user_id}, 成就ID={achievement_id}, 允许重复={allow_duplicate}")
    
    # 检查成就是否存在
    achievement = await execute_sql(
        "SELECT * FROM achievements WHERE id = ?",
        (achievement_id,),
        fetchone=True
    )
    
    if not achievement:
        logger.warning(f"[成就系统] 成就不存在，授予失败: 成就ID={achievement_id}")
        return False, None, None
    
    # 初始化用户
    await init_user(user_id)
    
    # 检查用户是否已拥有该成就（仅当不允许重复时）
    if not allow_duplicate:
        existing = await execute_sql(
            "SELECT user_id FROM user_achievements WHERE user_id = ? AND achievement_id = ?",
            (user_id, achievement_id),
            fetchone=True
        )
        
        if existing:
            logger.info(f"[成就系统] 用户已拥有该成就，授予失败: 用户ID={user_id}, 成就ID={achievement_id}")
            return False, None, None
    
    # 给予成就
    await execute_sql(
        "INSERT INTO user_achievements (user_id, achievement_id, obtained_at) VALUES (?, ?, ?)",
        (user_id, achievement_id, datetime.now().isoformat())
    )
    
    logger.info(f"[成就系统] 成就授予成功: 用户ID={user_id}, 成就ID={achievement_id}, 成就名称={achievement[1]}")
    
    # 构建成就信息字典
    achievement_info = {
        "id": achievement[0],
        "name": achievement[1],
        "description": achievement[2],
        "icon": achievement[3],
        "hidden": bool(achievement[4])
    }
    
    # 生成成就图片
    logger.info(f"[成就系统] 生成成就图片: 成就ID={achievement_id}")
    image_bytes = await generate_achievement_image(achievement_info)
    
    # 发送成就通知（如果提供了事件或matcher）
    if event or matcher:
        logger.info(f"[成就系统] 发送成就通知: 成就ID={achievement_id}, 用户ID={user_id}")
        try:
            from nonebot.adapters.onebot.v11 import MessageSegment, Message
            
            # 构建文本消息（作为降级方案）
            text_msg = f"🏆 恭喜获得成就！\n【{achievement_info['name']}】\n{achievement_info['description']}"
            
            if matcher:
                # 使用matcher发送
                if image_bytes:
                    try:
                        image_bytes.seek(0)
                        await matcher.send(MessageSegment.image(image_bytes))
                        logger.info(f"[成就系统] 成就图片发送成功 (matcher): 用户ID={user_id}")
                    except Exception as e:
                        logger.warning(f"[成就系统] 成就图片发送失败，降级为文本: {e}")
                        await matcher.send(text_msg)
                else:
                    await matcher.send(text_msg)
            elif event:
                # 使用事件发送
                from nonebot import get_bot
                bot = get_bot()
                
                if hasattr(event, 'group_id'):
                    # 群聊事件
                    if image_bytes:
                        try:
                            image_bytes.seek(0)
                            msg = Message([
                                MessageSegment.at(int(user_id)),
                                MessageSegment.text("\n"),
                                MessageSegment.image(image_bytes)
                            ])
                            await send_group_msg(
                                bot,
                                group_id=event.group_id,
                                message=msg
                            )
                            logger.info(f"[成就系统] 成就图片发送成功 (群聊): 用户ID={user_id}, 群ID={event.group_id}")
                        except Exception as e:
                            logger.warning(f"[成就系统] 成就图片发送失败，降级为文本: {e}")
                            await send_group_msg(
                                bot,
                                group_id=event.group_id,
                                message=Message([
                                    MessageSegment.at(int(user_id)),
                                    MessageSegment.text(f"\n{text_msg}")
                                ])
                            )
                    else:
                        await send_group_msg(
                            bot,
                            group_id=event.group_id,
                            message=Message([
                                MessageSegment.at(int(user_id)),
                                MessageSegment.text(f"\n{text_msg}")
                            ])
                        )
                        logger.info(f"[成就系统] 成就文本通知发送成功 (群聊): 用户ID={user_id}, 群ID={event.group_id}")
                else:
                    # 私聊事件
                    if image_bytes:
                        try:
                            image_bytes.seek(0)
                            await send_private_msg(
                                bot,
                                user_id=int(user_id),
                                message=MessageSegment.image(image_bytes)
                            )
                            logger.info(f"[成就系统] 成就图片发送成功 (私聊): 用户ID={user_id}")
                        except Exception as e:
                            logger.warning(f"[成就系统] 成就图片发送失败，降级为文本: {e}")
                            await send_private_msg(
                                bot,
                                user_id=int(user_id),
                                message=text_msg
                            )
                    else:
                        await send_private_msg(
                            bot,
                            user_id=int(user_id),
                            message=text_msg
                        )
                        logger.info(f"[成就系统] 成就文本通知发送成功 (私聊): 用户ID={user_id}")
        except Exception as e:
            logger.error(f"[成就系统] 发送成就通知失败: {e}")
    
    return True, achievement_info, image_bytes

# 检查用户是否拥有成就
async def has_achievement(user_id: str, achievement_id: str) -> bool:
    """
    检查用户是否拥有特定成就
    
    参数:
        user_id: 用户唯一标识符
        achievement_id: 成就唯一标识符
    
    返回:
        bool: 拥有返回True，否则返回False
    """
    result = await execute_sql(
        "SELECT user_id FROM user_achievements WHERE user_id = ? AND achievement_id = ?",
        (user_id, achievement_id),
        fetchone=True
    )
    
    return result is not None

# 获取用户所有成就
async def get_user_achievements(user_id: str) -> List[Dict]:
    """
    获取用户所有成就
    
    参数:
        user_id: 用户唯一标识符
    
    返回:
        List[Dict]: 用户成就列表
    """
    results = await execute_sql(
        '''
        SELECT a.* FROM achievements a
        JOIN user_achievements ua ON a.id = ua.achievement_id
        WHERE ua.user_id = ?
        ''',
        (user_id,),
        fetchall=True
    )
    
    achievements = []
    for result in results:
        achievements.append({
            "id": result[0],
            "name": result[1],
            "description": result[2],
            "icon": result[3],
            "hidden": bool(result[4]),
            "created_at": result[5]
        })
    
    return achievements

# 获取所有已注册成就
async def get_all_achievements(show_hidden: bool = False) -> Dict[str, Dict]:
    """
    获取所有已注册成就
    
    参数:
        show_hidden: 是否显示隐藏成就（可选，默认False）
    
    返回:
        Dict[str, Dict]: 成就字典，键为成就ID，值为成就详情
    """
    if show_hidden:
        results = await execute_sql(
            "SELECT * FROM achievements",
            fetchall=True
        )
    else:
        results = await execute_sql(
            "SELECT * FROM achievements WHERE hidden = 0",
            fetchall=True
        )
    
    achievements = {}
    for result in results:
        achievements[result[0]] = {
            "id": result[0],
            "name": result[1],
            "description": result[2],
            "icon": result[3],
            "hidden": bool(result[4]),
            "created_at": result[5]
        }
    
    return achievements

# 初始化数据
@get_driver().on_startup
async def init_achievement_data():
    await init_db()

# 关闭数据库连接
@get_driver().on_shutdown
async def on_shutdown():
    await close_db()

# 命令：查询用户成就
achievements_cmd = cmdbuild("achievements.list", "成就").alias("我的成就").priority(10).build()

@achievements_cmd.handle()
async def handle_achievements(event: MessageEvent):
    """处理用户成就查询命令"""
    user_id = str(event.user_id)
    user_achievements = await get_user_achievements(user_id)
    all_achievements = await get_all_achievements()
    
    if not user_achievements:
        await achievements_cmd.finish("你还没有获得任何成就，继续努力吧！")
    
    message = f"📋 你的成就列表 ({len(user_achievements)}/{len(all_achievements)})\n\n"
    for achievement in user_achievements:
        # 在文本消息中，使用默认的🏆图标
        message += f"🏆 {achievement['name']}\n"
        message += f"   {achievement['description']}\n\n"
    
    await achievements_cmd.finish(message.strip())

# 命令：查询所有成就
achievements_list_cmd = cmdbuild("achievements.all", "成就列表").alias("所有成就").priority(10).build()

@achievements_list_cmd.handle()
async def handle_achievements_list(event: MessageEvent):
    """处理所有成就查询命令"""
    all_achievements = await get_all_achievements()
    
    if not all_achievements:
        await achievements_list_cmd.finish("目前还没有任何成就，敬请期待！")
    
    message = f"📋 所有成就列表 ({len(all_achievements)})\n\n"
    for achievement_id, achievement in all_achievements.items():
        # 在文本消息中，使用默认的🏆图标
        message += f"🏆 {achievement['name']}\n"
        message += f"   {achievement['description']}\n\n"
    
    await achievements_list_cmd.finish(message.strip())

# 签到功能
async def checkin(user_id: str, event=None) -> Tuple[bool, str, int, int]:
    """
    用户签到
    
    参数:
        user_id: 用户唯一标识符
        event: 事件对象，用于发送成就通知（可选）
    
    返回:
        Tuple[bool, str, int, int]: (是否签到成功, 签到消息, 获得货币, 连续签到天数)
    """
    # 初始化用户
    await init_user(user_id)
    
    # 获取当前日期
    today = date.today().isoformat()
    
    # 获取用户信息
    user_info = await execute_sql(
        "SELECT last_checkin, checkin_streak, coins FROM users WHERE user_id = ?",
        (user_id,),
        fetchone=True
    )
    
    if not user_info:
        return False, "用户不存在", 0, 0
    
    last_checkin, checkin_streak, coins = user_info
    
    # 检查是否已经签到
    if last_checkin == today:
        return False, "你今天已经签到过了", 0, checkin_streak
    
    # 计算连续签到天数
    new_streak = 1
    if last_checkin:
        yesterday = (date.fromisoformat(last_checkin) + timedelta(days=1)).isoformat()
        if yesterday == today:
            new_streak = checkin_streak + 1
    
    # 计算签到奖励
    base_reward = 10
    streak_bonus = new_streak * 2
    total_reward = base_reward + streak_bonus
    
    # 更新用户信息
    await execute_sql(
        '''
        UPDATE users SET 
            coins = coins + ?, 
            last_checkin = ?, 
            checkin_streak = ? 
        WHERE user_id = ?
        ''',
        (total_reward, today, new_streak, user_id)
    )
    
    # 检查并给予签到成就
    # 首次签到成就
    await grant_achievement(user_id, "first_checkin", event=event)
    
    # 连续签到成就
    if new_streak == 7:
        await grant_achievement(user_id, "checkin_7_days", event=event)
    elif new_streak == 30:
        await grant_achievement(user_id, "checkin_30_days", event=event)
    elif new_streak == 100:
        await grant_achievement(user_id, "checkin_100_days", event=event)
    
    return True, f"签到成功！", total_reward, new_streak

# 货币管理功能
async def add_coins(user_id: str, amount: int) -> bool:
    """
    增加用户货币
    
    参数:
        user_id: 用户唯一标识符
        amount: 增加的货币数量
    
    返回:
        bool: 操作成功返回True，否则返回False
    """
    if amount <= 0:
        return False
    
    # 初始化用户
    await init_user(user_id)
    
    await execute_sql(
        "UPDATE users SET coins = coins + ? WHERE user_id = ?",
        (amount, user_id)
    )
    
    return True

async def reduce_coins(user_id: str, amount: int) -> bool:
    """
    减少用户货币
    
    参数:
        user_id: 用户唯一标识符
        amount: 减少的货币数量
    
    返回:
        bool: 操作成功返回True，否则返回False
    """
    if amount <= 0:
        return False
    
    # 检查用户是否有足够的货币
    user_info = await execute_sql(
        "SELECT coins FROM users WHERE user_id = ?",
        (user_id,),
        fetchone=True
    )
    
    if not user_info or user_info[0] < amount:
        return False
    
    await execute_sql(
        "UPDATE users SET coins = coins - ? WHERE user_id = ?",
        (amount, user_id)
    )
    
    return True

async def get_coins(user_id: str) -> int:
    """
    获取用户货币数量
    
    参数:
        user_id: 用户唯一标识符
    
    返回:
        int: 用户货币数量
    """
    # 初始化用户
    await init_user(user_id)
    
    result = await execute_sql(
        "SELECT coins FROM users WHERE user_id = ?",
        (user_id,),
        fetchone=True
    )
    
    return result[0] if result else 0

# 命令：签到
checkin_cmd = cmdbuild("achievements.checkin", "签到").priority(10).build()

@checkin_cmd.handle()
async def handle_checkin(event: MessageEvent):
    """处理用户签到命令"""
    user_id = str(event.user_id)
    success, message, reward, streak = await checkin(user_id, event)
    
    if success:
        coins = await get_coins(user_id)
        await checkin_cmd.finish(
            f"🎉 {message}\n" \
            f"💰 获得货币：{reward}\n" \
            f"🔥 连续签到：{streak} 天\n" \
            f"💎 当前余额：{coins}"
        )
    else:
        await checkin_cmd.finish(message)

# 命令：查询货币
coins_cmd = cmdbuild("achievements.coins", "我的货币").alias("货币", "余额").priority(10).build()

@coins_cmd.handle()
async def handle_coins(event: MessageEvent):
    """处理用户货币查询命令"""
    user_id = str(event.user_id)
    coins = await get_coins(user_id)
    await coins_cmd.finish(f"💎 你的货币余额：{coins}")

# 命令：测试成就（可持续授予）
test_achievement_cmd = cmdbuild("achievements.test", "测试成就").priority(10).build()

@test_achievement_cmd.handle()
async def handle_test_achievement(event: MessageEvent):
    """处理测试成就命令，可重复获得"""
    user_id = str(event.user_id)
    
    # 注册测试成就（如果不存在）
    await register_achievement(
        achievement_id="test_repeatable",
        name="测试成就",
        description="这是一个可以重复获得的测试成就",
        hidden=False
    )
    
    # 授予测试成就，允许重复获得
    success, _, _ = await grant_achievement(
        user_id=user_id,
        achievement_id="test_repeatable",
        allow_duplicate=True,
        event=event
    )
    
    if success:
        await test_achievement_cmd.finish("🎉 测试成就授予成功！你可以重复使用这个命令获得更多成就")
    else:
        await test_achievement_cmd.finish("❌ 成就授予失败，请稍后重试")

# 导出API
export = {
    "register_achievement": register_achievement,
    "grant_achievement": grant_achievement,
    "has_achievement": has_achievement,
    "get_user_achievements": get_user_achievements,
    "get_all_achievements": get_all_achievements,
    "checkin": checkin,
    "add_coins": add_coins,
    "reduce_coins": reduce_coins,
    "get_coins": get_coins
}
