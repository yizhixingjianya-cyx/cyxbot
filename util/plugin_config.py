
"""
插件独立配置文件读取模块
为插件提供独立的配置文件读取功能
"""
import yaml
import os
from pathlib import Path
from typing import Dict, Any, Optional, TypeVar, Type
from dataclasses import dataclass
from pydantic import BaseModel
import logging

logger = logging.getLogger(__name__)

T = TypeVar('T', bound=BaseModel)


class PluginConfig:
    """
    插件独立配置管理器
    
    使用示例:
        from util.plugin_config import get_plugin_config
        from pydantic import BaseModel
        
        class MyPluginConfig(BaseModel):
            enable: bool = True
            message: str = "Hello"
            
        config = get_plugin_config("my_plugin", MyPluginConfig)
        print(config.message)
    """
    
    # 配置文件基础目录
    BASE_CONFIG_DIR = Path("src/data/configs")
    
    @classmethod
    def get(cls, plugin_name: str, config_class: Type[T]) -> Optional[T]:
        """
        读取插件配置
        
        Args:
            plugin_name: 插件名称
            config_class: 配置数据类（pydantic BaseModel 类）
        
        Returns:
            配置对象
        """
        config_path = cls.BASE_CONFIG_DIR / f"{plugin_name}.yaml"
        
        if not config_path.exists():
            logger.warning(f"插件配置文件不存在: {config_path}")
            # 返回默认配置
            try:
                config = config_class()
                # 自动保存默认配置
                cls.save(plugin_name, config)
                return config
            except Exception as e:
                logger.error(f"创建默认配置失败: {e}")
                return None
        
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
            
            config = config_class(**data)
            return config
        except Exception as e:
            logger.error(f"读取插件配置失败 {plugin_name}: {e}")
            return None
    
    @classmethod
    def save(cls, plugin_name: str, config: BaseModel) -> bool:
        """
        保存插件配置
        
        Args:
            plugin_name: 插件名称
            config: 配置对象
        
        Returns:
            是否保存成功
        """
        try:
            config_path = cls.BASE_CONFIG_DIR / f"{plugin_name}.yaml"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            
            data = config.model_dump()
            with open(config_path, 'w', encoding='utf-8') as f:
                yaml.dump(data, f, allow_unicode=True)
            
            return True
        except Exception as e:
            logger.error(f"保存插件配置失败 {plugin_name}: {e}")
            return False


# 便捷函数
def get_plugin_config(plugin_name: str, config_class: Type[T]) -> Optional[T]:
    """获取插件配置"""
    return PluginConfig.get(plugin_name, config_class)


def save_plugin_config(plugin_name: str, config: BaseModel) -> bool:
    """保存插件配置"""
    return PluginConfig.save(plugin_name, config)


def get_config_path(plugin_name: str) -> Path:
    """获取插件配置文件路径"""
    return PluginConfig.BASE_CONFIG_DIR / f"{plugin_name}.yaml"


def get_data_path() -> Path:
    """获取数据目录路径"""
    return Path("src/data")


def get_plugin_data_path(plugin_name: str) -> Path:
    """获取插件专用数据目录路径"""
    path = Path("src/data") / plugin_name
    path.mkdir(parents=True, exist_ok=True)
    return path
