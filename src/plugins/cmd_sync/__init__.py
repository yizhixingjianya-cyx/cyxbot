"""开机时把本地指令清单导出为 configs/commands.json（仅导出，不上传）

- 自动扫描各插件 on_command（含别名），合并 configs/commands.json 里手工维护的
  desc/scope/block/manual 字段后回写，供「指令上传管理器」读取与上传。
- 上传已改为由桌面端图形化管理器直连 QQ 官方 OpenAPI 完成，机器人只负责导出。
- 可用环境变量 COMMANDS_JSON_PATH 指定 json 位置。
"""
from nonebot import get_driver
from nonebot.log import logger

from util.command_sync import commands_json_path, load_merge_commands

driver = get_driver()


@driver.on_startup
async def _export_commands() -> None:
    """启动时导出一次指令清单，作为图形化管理器的数据源"""
    try:
        commands = load_merge_commands()
        logger.success(f"[指令导出] 已导出 {len(commands)} 条指令 -> {commands_json_path()}")
    except Exception as e:
        logger.error(f"[指令导出] 导出失败：{e!r}")
