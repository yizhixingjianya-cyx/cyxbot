from nonebot import get_plugin_config, get_driver
from pydantic import BaseModel
from pathlib import Path
from nonebot import require

require("nonebot_plugin_apscheduler")

# 尝试导入 localstore 插件
localstore_available = False
try:
    from nonebot import require
    require("nonebot_plugin_localstore")
    from nonebot_plugin_localstore import get_plugin_cache_dir
    localstore_available = True
except Exception:
    localstore_available = False

from nonebot.plugin import PluginMetadata
from .config import FurtvConfig
from .ftvhelp import *
__plugin_meta__ = PluginMetadata(
    # 基本信息（必填）
    name="nonebot_plugin_fursuit_tv",  # 插件名称
    description="实现 VDS DEVELOPER 所支持的fursuit tv 端点",  # 插件介绍
    usage="发送【ftv帮助】获取帮助",  # 插件用法

    # 发布额外信息
    type="application",  # 插件分类
    # 发布必填，当前有效类型有：`library`（为其他插件编写提供功能），`application`（向机器人用户提供功能）。

    homepage="https://github.com/yizhixingjianya-cyx/nonebot-plugin-fursuit_tv",
    # 发布必填。

    config=FurtvConfig,
    # 插件配置项类，如果有配置类则必须填写。

    supported_adapters={"~onebot.v11"},
    # 支持的适配器集合，其中 `~` 在此处代表前缀 `nonebot.adapters.`，其余适配器亦按此格式填写。
    # 若插件只使用了 NoneBot 基本抽象，应显式填写 None，否则应该列出插件支持的适配器。
)
from .config import ftv_config, FurtvConfig, get_ftv_config, clean_config
from .token_manager import token_manager
from .cache_manager import cache_manager, CacheManager

class Config(BaseModel):
    ftv_base_url: str = "https://open-cn1.vdsentnet.com"
    ftv_app_id: str = ""
    ftv_client_secret: str = ""
    ftv_use_localstore: bool = True
    ftv_no_cache: bool = False


# 延迟初始化配置
def init_config():
    plugin_config = get_plugin_config(Config)
    # .env 里手滑带上反引号 / 引号时，这里先洗一遍再往下传
    clean_config(plugin_config)

    # 更新全局配置
    config = get_ftv_config()
    config.ftv_base_url = plugin_config.ftv_base_url
    config.ftv_app_id = plugin_config.ftv_app_id
    config.ftv_client_secret = plugin_config.ftv_client_secret
    config.ftv_use_localstore = plugin_config.ftv_use_localstore
    config.ftv_no_cache = plugin_config.ftv_no_cache

# 在 NoneBot 启动时初始化令牌和缓存
@get_driver().on_startup
async def init_token():
    from nonebot.log import logger
    logger.info("开始初始化 Furtv 令牌...")
    success = await token_manager.initialize()
    if success:
        logger.success("Furtv 令牌初始化完成")
        # 启动时检查权限
        _check_startup_permissions()
    else:
        logger.error("Furtv 令牌初始化失败")


def _check_startup_permissions():
    """启动时检查权限并记录"""
    from nonebot.log import logger
    
    grants = token_manager.get_grants()
    if not grants:
        logger.warning("当前令牌无任何权限，请检查 ftv_app_id 和 ftv_client_secret 配置")
        return
    
    logger.info(f" 当前令牌权限组（共 {len(grants)} 个）:")
    for i, grant in enumerate(grants, 1):
        logger.info(f"  {i}. {grant}")
    
    # 检查关键权限
    critical_grants = ['furtv.discovery', 'furtv.users', 'furtv.gatherings']
    missing_critical = [g for g in critical_grants if not token_manager.has_grant(g)]
    
    if missing_critical:
        logger.warning(f"缺少关键权限：{missing_critical}，部分功能可能无法使用")
    else:
        logger.success("权限检查通过")

# 初始化缓存管理器（立即执行）
def init_cache():
    from nonebot.log import logger
    try:
        # 获取配置
        config = get_ftv_config()
        
        # 声明全局变量
        global cache_manager
        
        # 检查是否禁用缓存
        if config.ftv_no_cache:
            logger.warning("缓存已禁用")
            cache_manager = None
            return True
        
        if config.ftv_use_localstore and localstore_available:
            # 使用 localstore 插件提供的 get_plugin_cache_dir
            try:
                # 尝试不带参数调用
                cache_dir = get_plugin_cache_dir()
                logger.success(f"使用 localstore 缓存目录 | 目录：{cache_dir}")
            except TypeError:
                # 如果需要参数，使用插件名称
                cache_dir = get_plugin_cache_dir('nonebot_plugin_fursuit_tv')
                logger.success(f"使用 localstore 缓存目录 | 目录：{cache_dir}")
        else:
            # 使用原来的缓存模式
            ftv_dir = Path(__file__).parent
            cache_dir = ftv_dir / "cache"
            if config.ftv_use_localstore and not localstore_available:
                logger.warning("localstore 插件未安装，使用原缓存模式")
            else:
                logger.success(f"使用原缓存模式 | 目录：{cache_dir}")
        
        cache_manager = CacheManager(cache_dir)
        logger.success(f"缓存管理器已初始化 | 目录：{cache_dir}")
        return True
    except Exception as e:
        logger.error(f"缓存管理器初始化失败：{e}")
        return False

# 注册定时刷新任务（每分钟检查一次）
from nonebot_plugin_apscheduler import scheduler

@scheduler.scheduled_job("interval", minutes=1, id="ftv_token_refresh", name="Furtv 令牌定时刷新", misfire_grace_time=10)
async def token_refresh_job():
    from nonebot.log import logger
    logger.info("执行定时令牌刷新任务...")
    await token_manager.check_and_refresh()

# 注册定时清理缓存任务（每小时执行一次）
@scheduler.scheduled_job("interval", hours=1, id="ftv_cache_cleanup", name="Furtv 缓存定时清理", misfire_grace_time=10)
async def cache_cleanup_job():
    from nonebot.log import logger
    logger.info("执行定时缓存清理任务...")
    if cache_manager:
        await cache_manager.cleanup_expired()

# 启动时打印定时任务信息
@get_driver().on_startup
async def print_scheduler_info():
    from nonebot.log import logger
    import asyncio
    await asyncio.sleep(0.5)  # 等待 scheduler 完全启动
    logger.info(f"Furtv 定时任务已注册")
    for job in scheduler.get_jobs():
        logger.info(f"  - {job.name}: {job.trigger}")

# 在模块加载时初始化配置和缓存
try:
    init_config()
    if not init_cache():
        from nonebot.log import logger
        logger.warning("缓存管理器未初始化，缓存功能将不可用")
except Exception as e:
    from nonebot.log import logger
    logger.warning(f"注意！注意！ftv 配置初始化失败：{e}，请检查 env 文件配置是否正常！")

from .commands import *
from .today_commands import *
from .user_select import *
from .gathering_select import *
from .menu import *

from nonebot.adapters import Bot, Event
from nonebot.matcher import Matcher
from nonebot.message import run_preprocessor
from nonebot.typing import T_State
from .hint import arm_hint


@run_preprocessor
async def _ftv_searching_hint(bot: Bot, event: Event, state: T_State, matcher: Matcher):
    """ftv 指令命中后登记会话，等真正访问 API 时才发「查询中，请稍后」

    菜单等不访问接口的指令、以及参数缺失提前返回的情况都不会发提示。
    """
    node = getattr(matcher, "permission_node", "")
    if not isinstance(node, str) or not node.startswith("ftv."):
        return
    arm_hint(bot, event)


# 图床静态服务（默认 11451，取代 nginx）：ftv 的档案图 / 统计图是本机画的，
# 要先换成公网 URL 才能和文字挤在同一条消息里。setup() 内部保证只注册一次。
try:
    from util.static_server import setup as setup_image_host

    setup_image_host()
except Exception:
    pass
