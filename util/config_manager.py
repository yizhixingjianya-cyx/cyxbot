"""
统一配置管理模块
使用YAML格式存储配置，支持热加载和动态更新
"""

import os
import yaml
import logging
from typing import Dict, Any, Optional, Union
from pathlib import Path
from dataclasses import dataclass, asdict
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class ConfigItem:
    """配置项数据类"""
    key: str
    value: Any = None
    description: str = ""
    type: str = ""
    required: bool = False
    default: Any = None


class ConfigManager:
    """统一配置管理器"""

    def __init__(
        self,
        config_path: Union[str, Path] = "config.yaml",
        auto_save: bool = True,
        auto_reload: bool = False,
        reload_interval: float = 60.0
    ):
        self.config_path = Path(config_path)
        self.auto_save = auto_save
        self.auto_reload = auto_reload
        self.reload_interval = reload_interval
        self._last_reload = 0.0
        self._config: Dict[str, Any] = {}
        self._schema: Dict[str, ConfigItem] = {}

        # 确保配置目录存在
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 加载配置
        self.load_config()

    def load_config(self):
        """加载配置文件"""
        try:
            if self.config_path.exists():
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    self._config = yaml.safe_load(f) or {}
                logger.info(f"配置文件已加载: {self.config_path}")
            else:
                self._config = {}
                logger.warning(f"配置文件不存在，将创建新配置: {self.config_path}")
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            self._config = {}

    async def async_load_config(self):
        """异步加载配置文件"""
        try:
            if self.config_path.exists():
                import aiofiles
                async with aiofiles.open(self.config_path, 'r', encoding='utf-8') as f:
                    content = await f.read()
                    self._config = yaml.safe_load(content) or {}
                logger.info(f"配置文件已异步加载: {self.config_path}")
            else:
                self._config = {}
                logger.warning(f"配置文件不存在，将创建新配置: {self.config_path}")
        except ImportError:
            logger.warning("aiofiles library not available, falling back to sync")
            self.load_config()
        except Exception as e:
            logger.error(f"异步加载配置文件失败: {e}")
            self._config = {}

    def save_config(self):
        """保存配置文件"""
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                yaml.dump(self._config, f, indent=2, allow_unicode=True)
            logger.info(f"配置文件已保存: {self.config_path}")
        except Exception as e:
            logger.error(f"保存配置文件失败: {e}")

    async def async_save_config(self):
        """异步保存配置文件"""
        try:
            import aiofiles
            async with aiofiles.open(self.config_path, 'w', encoding='utf-8') as f:
                await f.write(yaml.dump(self._config, indent=2, allow_unicode=True))
            logger.info(f"配置文件已异步保存: {self.config_path}")
        except ImportError:
            logger.warning("aiofiles library not available, falling back to sync")
            self.save_config()
        except Exception as e:
            logger.error(f"异步保存配置文件失败: {e}")

    async def async_set(self, key: str, value: Any, save: Optional[bool] = None):
        """异步设置配置值"""
        self.set(key, value, save=False)
        if (save is None and self.auto_save) or save:
            await self.async_save_config()

    async def async_update(self, config: Dict[str, Any], save: Optional[bool] = None):
        """异步批量更新配置"""
        self.update(config, save=False)
        if (save is None and self.auto_save) or save:
            await self.async_save_config()

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值"""
        # 支持点路径访问，如 "database.host"
        keys = key.split('.')
        current = self._config
        
        for k in keys:
            if isinstance(current, dict) and k in current:
                current = current[k]
            else:
                return default
        
        return current

    def set(self, key: str, value: Any, save: Optional[bool] = None):
        """设置配置值"""
        # 支持点路径访问
        keys = key.split('.')
        current = self._config
        
        for i, k in enumerate(keys[:-1]):
            if k not in current or not isinstance(current[k], dict):
                current[k] = {}
            current = current[k]
        
        current[keys[-1]] = value
        
        # 自动保存
        if (save is None and self.auto_save) or save:
            self.save_config()

    def update(self, config: Dict[str, Any], save: Optional[bool] = None):
        """批量更新配置"""
        self._config.update(config)
        if (save is None and self.auto_save) or save:
            self.save_config()

    def get_all(self) -> Dict[str, Any]:
        """获取所有配置"""
        return self._config.copy()

    def register_schema(self, schema: Dict[str, ConfigItem]):
        """注册配置项元数据"""
        self._schema.update(schema)

    def validate_config(self) -> Dict[str, Any]:
        """验证配置完整性"""
        errors = {}
        warnings = {}
        
        for key, item in self._schema.items():
            value = self.get(key)
            if item.required and value is None:
                errors[key] = f"必填配置项缺失"
            elif value is None and item.default is not None:
                warnings[key] = f"使用默认值: {item.default}"
                self.set(key, item.default, save=False)
        
        if errors:
            logger.error(f"配置验证失败: {errors}")
        if warnings:
            logger.warning(f"配置警告: {warnings}")
        
        return {"errors": errors, "warnings": warnings}

    def get_config_schema(self) -> Dict[str, ConfigItem]:
        """获取配置项元数据"""
        return self._schema.copy()

    def reload_if_needed(self):
        """根据需要自动重新加载配置"""
        if not self.auto_reload:
            return
        
        import time
        current_time = time.time()
        if current_time - self._last_reload >= self.reload_interval:
            self.load_config()
            self._last_reload = current_time


class PluginConfigManager:
    """插件配置管理器"""

    def __init__(self, plugin_name: str, config_manager: ConfigManager):
        self.plugin_name = plugin_name
        self.config_manager = config_manager
        self._prefix = f"plugins.{plugin_name}"

    def get(self, key: str, default: Any = None) -> Any:
        """获取插件配置"""
        full_key = f"{self._prefix}.{key}"
        return self.config_manager.get(full_key, default)

    def set(self, key: str, value: Any, save: Optional[bool] = None):
        """设置插件配置"""
        full_key = f"{self._prefix}.{key}"
        self.config_manager.set(full_key, value, save)

    async def async_set(self, key: str, value: Any, save: Optional[bool] = None):
        """异步设置插件配置"""
        full_key = f"{self._prefix}.{key}"
        await self.config_manager.async_set(full_key, value, save)

    def get_all(self) -> Dict[str, Any]:
        """获取插件所有配置"""
        plugin_config = self.config_manager.get(self._prefix, {})
        return plugin_config.copy()

    def update(self, config: Dict[str, Any], save: Optional[bool] = None):
        """批量更新插件配置"""
        current_config = self.get_all()
        current_config.update(config)
        self.config_manager.set(self._prefix, current_config, save)

    async def async_update(self, config: Dict[str, Any], save: Optional[bool] = None):
        """异步批量更新插件配置"""
        current_config = self.get_all()
        current_config.update(config)
        await self.config_manager.async_set(self._prefix, current_config, save)


# 全局配置管理器实例
config_manager = ConfigManager()


def get_config_manager() -> ConfigManager:
    """获取全局配置管理器"""
    return config_manager


def get_plugin_config(plugin_name: str) -> PluginConfigManager:
    """获取插件配置管理器"""
    return PluginConfigManager(plugin_name, config_manager)


async def async_init_config():
    """异步初始化全局配置管理器"""
    await config_manager.async_load_config()


async def async_get_config_manager() -> ConfigManager:
    """异步获取全局配置管理器（并确保已异步加载）"""
    if not config_manager._config:
        await config_manager.async_load_config()
    return config_manager


# 常用配置示例
DEFAULT_CONFIG_SCHEMA = {
    "database": ConfigItem(
        key="database",
        description="数据库配置",
        type="dict",
        required=True,
        default={"path": "data/"}
    ),
    "bot": ConfigItem(
        key="bot",
        description="机器人配置",
        type="dict",
        required=True,
        default={"command_prefix": "!", "superusers": []}
    ),
    "api": ConfigItem(
        key="api",
        description="API配置",
        type="dict",
        required=False,
        default={"timeout": 10, "retries": 2}
    )
}


# 初始化时注册默认配置
config_manager.register_schema(DEFAULT_CONFIG_SCHEMA)
