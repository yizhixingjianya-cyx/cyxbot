import os
import sys
# 获取当前脚本的绝对路径
current_dir = os.path.dirname(os.path.abspath(__file__))

# 将当前目录添加到模块搜索路径的最前面
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

import nonebot
import yaml
from pathlib import Path
from nonebot.adapters.onebot.v11 import Adapter as ONEBOT_V11Adapter
from util import get_config_manager


# 初始化统一配置管理器
config_manager = get_config_manager()

# 直接配置 NoneBot
nonebot.init(
    command_start=config_manager.get("bot.command_start", ["!"]),
    command_sep=config_manager.get("bot.command_sep", []),
    superusers=set(config_manager.get("bot.superusers", [])),
    nickname={config_manager.get("bot.nickname", "Sakura")},
    session_expire_timeout=config_manager.get("bot.session_expire_timeout", 3600),
    session_expire_message=config_manager.get("bot.session_expire_message", True),
    session_expire_warning_time=config_manager.get("bot.session_expire_warning_time", 300),
    log_level=config_manager.get("bot.log_level", "INFO"),
    debug=config_manager.get("bot.debug", False),
    single_session=config_manager.get("bot.single_session", False),
    api_timeout=config_manager.get("bot.api_timeout", 10),
    max_retry_times=config_manager.get("bot.max_retry_times", 3),
    hot_reload=config_manager.get("bot.hot_reload", False),
    plugin_dirs=config_manager.get("bot.plugin_dirs", ["src/plugins"]),
    plugin_ignore=config_manager.get("bot.plugin_ignore", []),
    plugin_auto_install=config_manager.get("bot.plugin_auto_install", False),
    plugin_auto_update=config_manager.get("bot.plugin_auto_update", False),
    plugin_auto_uninstall=config_manager.get("bot.plugin_auto_uninstall", False),
    plugin_auto_clean=config_manager.get("bot.plugin_auto_clean", False),
)

driver = nonebot.get_driver()
driver.register_adapter(ONEBOT_V11Adapter)

# 先加载apscheduler插件
nonebot.load_plugin('nonebot_plugin_apscheduler')

# 批量加载插件目录
nonebot.load_plugins("src/plugins")
nonebot.load_builtin_plugins('echo', 'single_session')



if __name__ == "__main__":
    nonebot.run()