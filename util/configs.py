
"""
配置系统模块
提供通用的配置和状态管理功能，支持热重载
"""
import os
import yaml
import logging
from typing import Dict, List, Optional, Any, Generic, TypeVar, Callable, Set, Iterable, Tuple
from pathlib import Path
from dataclasses import dataclass
from abc import ABC, abstractmethod

from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar('T')


class BaseConfig(ABC, Generic[T]):
    """配置基类"""

    def __init__(self, model, reloadable=True):
        self.model = model
        self.reloadable = reloadable
        self._cache = {}
        self._load_hooks = []

    @abstractmethod
    def get_file(self, *params):
        """获取配置文件路径"""
        pass

    @abstractmethod
    def get_all(self):
        """获取所有参数组合"""
        pass

    def onload(self):
        """
        注册配置加载钩子
        
        使用示例:
            @my_config.onload()
            def on_load(old, new, ...):
                pass
        """
        def decorator(func):
            self._load_hooks.append(func)
            return func
        return decorator

    def _load_file(self, file):
        """加载配置文件"""
        if not os.path.exists(file):
            return None
        
        try:
            with open(file, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
            return self.model(**data)
        except Exception as e:
            logger.error(f"加载配置文件失败 {file}: {e}")
            return None

    def _save_file(self, file, config):
        """保存配置文件"""
        try:
            # 确保目录存在
            Path(file).parent.mkdir(parents=True, exist_ok=True)
            
            data = config.model_dump()
            with open(file, 'w', encoding='utf-8') as f:
                yaml.dump(data, f, allow_unicode=True)
        except Exception as e:
            logger.error(f"保存配置文件失败 {file}: {e}")

    def __call__(self, *params):
        """
        获取配置
        
        Args:
            *params: 参数，用于确定具体的配置文件
        
        Returns:
            配置对象
        """
        cache_key = tuple(params)
        
        # 检查缓存
        if cache_key not in self._cache or not self.reloadable:
            file = self.get_file(*params)
            config = self._load_file(file)
            
            if config is None:
                config = self.model()
                self._save_file(file, config)
            
            self._cache[cache_key] = config
        
        return self._cache[cache_key]

    def dump(self, *params, config=None):
        """
        保存配置
        
        Args:
            *params: 参数
            config: 配置对象，None 表示保存当前缓存的
        """
        cache_key = tuple(params)
        
        if config is None and cache_key in self._cache:
            config = self._cache[cache_key]
        
        if config is not None:
            file = self.get_file(*params)
            self._save_file(file, config)
            
            # 更新缓存
            self._cache[cache_key] = config

    def reload(self):
        """重新加载所有配置"""
        if not self.reloadable:
            return
        
        for params in self.get_all():
            cache_key = tuple(params)
            old_config = self._cache.get(cache_key)
            
            file = self.get_file(*params)
            new_config = self._load_file(file)
            
            if new_config is None:
                new_config = self.model()
            
            # 调用钩子
            for hook in self._load_hooks:
                try:
                    hook(old_config, new_config, *params)
                except Exception as e:
                    logger.error(f"配置钩子执行失败: {e}")
            
            self._cache[cache_key] = new_config


class SharedConfig(BaseConfig[T]):
    """共享配置（所有群聊共用一份）"""

    base_dir = "src/data/configs"

    def __init__(self, name, model, reloadable=True):
        super().__init__(model, reloadable)
        self.name = name

    def get_file(self):
        return f"{self.base_dir}/{self.name}.yaml"

    def get_all(self):
        yield ()


class GroupConfig(BaseConfig[T]):
    """群组配置（每个群聊单独一份）"""

    base_dir = "src/data/configs"

    def __init__(self, name, model, reloadable=True):
        super().__init__(model, reloadable)
        self.name = name
        self.name_dir = f"{self.base_dir}/{self.name}"

    def get_file(self, group):
        file = f"{self.name_dir}/{group}.yaml"
        default_file = f"{self.name_dir}/default.yaml"
        
        # 如果文件不存在但默认文件存在，复制默认文件
        if not os.path.exists(file) and os.path.exists(default_file):
            try:
                import shutil
                shutil.copy(default_file, file)
            except Exception as e:
                logger.error(f"复制默认配置失败: {e}")
        
        return file

    def get_all(self):
        from .context import get_context_manager
        context_manager = get_context_manager()
        
        for group_id in context_manager.groups.keys():
            yield (group_id,)


class SharedState(BaseConfig[T]):
    """共享状态（所有群聊共用一份）"""

    base_dir = "src/data/states"

    def __init__(self, name, model, reloadable=True):
        super().__init__(model, reloadable)
        self.name = name

    def get_file(self):
        return f"{self.base_dir}/{self.name}.yaml"

    def get_all(self):
        yield ()


class GroupState(BaseConfig[T]):
    """群组状态（每个群聊单独一份）"""

    base_dir = "src/data/states"

    def __init__(self, name, model, reloadable=True):
        super().__init__(model, reloadable)
        self.name = name
        self.name_dir = f"{self.base_dir}/{self.name}"

    def get_file(self, group):
        return f"{self.name_dir}/{group}.yaml"

    def get_all(self):
        from .context import get_context_manager
        context_manager = get_context_manager()
        
        for group_id in context_manager.groups.keys():
            yield (group_id,)


def reload_all():
    """重新加载所有配置（需要手动维护列表）"""
    # 注意：这个函数需要手动维护需要重载的配置列表
    logger.warning("reload_all 未完全实现，需要手动维护配置列表")


# 便捷函数
def create_shared_config(name, model, reloadable=True):
    """创建共享配置"""
    return SharedConfig(name, model, reloadable)


def create_group_config(name, model, reloadable=True):
    """创建群组配置"""
    return GroupConfig(name, model, reloadable)


def create_shared_state(name, model, reloadable=True):
    """创建共享状态"""
    return SharedState(name, model, reloadable)


def create_group_state(name, model, reloadable=True):
    """创建群组状态"""
    return GroupState(name, model, reloadable)

