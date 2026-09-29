
"""
工具模块
提供项目中可复用的通用功能
"""

# 尽早把 .env 灌进 os.environ（放在最前面，早于下面任何子模块的 import）。
# NoneBot 只把 .env 读进自己的 pydantic 配置对象，不写 os.environ，
# 不补这一步的话所有用 os.getenv 读的配置（IMAGE_HOST_* / GUANLI_*）都只会拿到默认值：
# 面板悄悄绑到 127.0.0.1、图床服务不启动，日志里却没有任何报错。
from .env_value import load_env_file

load_env_file()

from .image_utils import (
    DrawText,
    find_font_path,
    load_font_with_fallback,
    wrap_text,
    draw_wrapped_text,
    tricolor_gradient,
    rounded_corners,
    create_rounded_rectangle,
    load_image_from_url,
    async_load_image_from_url,
    resize_image_with_ratio,
    create_text_image,
    paste_image_with_alpha,
)

from .api_client import (
    APIResponse,
    BaseAPIClient,
    SyncAPIClient,
    AsyncAPIClient,
    APIClientWithAuth,
    create_api_client,
)

from .database import (
    DBConfig,
    SQLiteDB,
    SimpleKVStore,
    create_sqlite_db,
    AsyncSQLiteDB,
    AsyncSimpleKVStore,
    create_async_sqlite_db,
)

from .font_manager import (
    get_font_path,
    load_font,
    get_all_fonts,
    register_font,
    Fonts,
    get_cubic_font,
    get_hanyi_font,
    get_mi_font,
)

from .config_manager import (
    ConfigItem,
    ConfigManager,
    PluginConfigManager,
    get_config_manager,
    get_plugin_config,
    async_init_config,
    async_get_config_manager,
    DEFAULT_CONFIG_SCHEMA,
)

from .hook import (
    Hook,
    hook,
)

from .context import (
    Context,
    context,
    UserContext,
    get_event_context,
    reload_context,
    get_context_manager,
)

from .permission import (
    PermissionLevel,
    permission,
    PermissionEntry,
    Role,
    UserPermission,
    PermissionManager,
    PermissionChecker,
    permission_manager,
    reload_permission,
    get_permission_manager,
)

from .command import (
    CommandBuilder,
    EventBuilder,
    on_group_increase,
    on_group_decrease,
    on_poke,
    on_friend_add,
    on_friend_request,
    on_group_request,
    cmdbuild,
)

from .configs import (
    BaseConfig,
    SharedConfig,
    GroupConfig,
    SharedState,
    GroupState,
    create_shared_config,
    create_group_config,
    create_shared_state,
    create_group_state,
    reload_all,
)

from .plugin_config import (
    PluginConfig,
    get_plugin_config,
    save_plugin_config,
    get_config_path,
    get_data_path,
    get_plugin_data_path,
)

from .misc import (
    binomial_sample,
    to_thread,
)

from . import gobot_api as _gobot_api
from .gobot_api import *  # noqa: F401,F403 — goqbot / 标准 OneBot 平台能力抽象层
from .gobot_api import PlatformNotSupportedError  # 平台不支持该专有能力时的异常

# 图片托管：把图片写进静态目录换成公网 URL，供插件实现「图片 + 文字同一条消息」。
# ftv 的图是本机画的；每日鉴毛的图在源站，但链接带签名会过期、QQ 抓不到，
# 所以它也要先下载转存（见 src/plugins/pyfurbot/command/furry_will.py::_publish_image）。
from . import image_host as _image_host
from .image_host import *  # noqa: F401,F403 — 图片托管（本机生成图 → 公网 URL）
from .image_host import image_base_url  # noqa: F401 — 对外基地址，便于日志排查

# 图床静态服务（默认 11451，取代 nginx）：只读地把上面的静态目录对外提供。
# 注意：bot.py 顶部就 import 了 util（早于 nonebot.init()），此处拿不到 driver 属正常，
# setup() 会直接返回；插件加载阶段 ftv / guanli 会再调一次，那时才真正挂上启动钩子。
from . import static_server as _static_server

try:
    _static_server.setup()
except Exception:  # 图床服务绝不能拖累 util 的导入
    pass

# 尽早包装 nonebot.on_command，记录“主指令 vs 别名”，供 util.command_sync 选主名
from .command_registry import install as _install_command_primary_tracking

_install_command_primary_tracking()

from .colorutil import (
    NAMES,
    parse,
    split_rgb,
    blend,
)

from .imutil import (
    colorize,
    to_segment,
)

__all__ = [
    # Image utils
    "DrawText",
    "find_font_path",
    "load_font_with_fallback",
    "wrap_text",
    "draw_wrapped_text",
    "tricolor_gradient",
    "rounded_corners",
    "create_rounded_rectangle",
    "load_image_from_url",
    "async_load_image_from_url",
    "resize_image_with_ratio",
    "create_text_image",
    "paste_image_with_alpha",

    # API client
    "APIResponse",
    "BaseAPIClient",
    "SyncAPIClient",
    "AsyncAPIClient",
    "APIClientWithAuth",
    "create_api_client",

    # Database
    "DBConfig",
    "SQLiteDB",
    "SimpleKVStore",
    "create_sqlite_db",
    "AsyncSQLiteDB",
    "AsyncSimpleKVStore",
    "create_async_sqlite_db",

    # Font manager
    "get_font_path",
    "load_font",
    "get_all_fonts",
    "register_font",
    "Fonts",
    "get_cubic_font",
    "get_hanyi_font",
    "get_mi_font",

    # Config manager
    "ConfigItem",
    "ConfigManager",
    "PluginConfigManager",
    "get_config_manager",
    "async_init_config",
    "async_get_config_manager",
    "DEFAULT_CONFIG_SCHEMA",

    # Hook
    "Hook",
    "hook",
    
    # Context
    "Context",
    "context",
    "UserContext",
    "get_event_context",
    "reload_context",
    "get_context_manager",
    
    # Permission
    "PermissionLevel",
    "permission",
    "PermissionEntry",
    "Role",
    "UserPermission",
    "PermissionManager",
    "PermissionChecker",
    "permission_manager",
    "reload_permission",
    "get_permission_manager",
    
    # Command
    "CommandBuilder",
    "EventBuilder",
    "on_group_increase",
    "on_group_decrease",
    "on_poke",
    "on_friend_add",
    "on_friend_request",
    "on_group_request",
    "cmdbuild",
    
    # Configs
    "BaseConfig",
    "SharedConfig",
    "GroupConfig",
    "SharedState",
    "GroupState",
    "create_shared_config",
    "create_group_config",
    "create_shared_state",
    "create_group_state",
    "reload_all",
    
    # Plugin config
    "PluginConfig",
    "save_plugin_config",
    "get_config_path",
    "get_data_path",
    "get_plugin_data_path",
    
    # Misc
    "binomial_sample",
    "to_thread",
    
    # Color utils
    "NAMES",
    "parse",
    "split_rgb",
    "blend",
    
    # Image utils (additional)
    "colorize",
    "to_segment",

    # goqbot / 标准 OneBot 平台能力抽象层
    "PlatformNotSupportedError",
] + _gobot_api.__all__ + _image_host.__all__

