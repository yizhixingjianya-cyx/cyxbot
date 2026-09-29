
"""
上下文系统模块
提供上下文管理功能，支持私聊中设置群聊上下文
"""
import yaml
import os
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, Set
from pathlib import Path
from dataclasses import dataclass

from nonebot.adapters.onebot.v11 import MessageEvent, GroupMessageEvent, PrivateMessageEvent
from nonebot.permission import Permission

from .permission import PermissionLevel
from .config_manager import get_config_manager

logger = logging.getLogger(__name__)


@dataclass
class UserContext:
    """用户上下文"""
    user_id: int
    group_id: Optional[int] = None
    expire: Optional[datetime] = None


class Context:
    """上下文管理器"""

    def __init__(self, config_path="configs/context.yaml", state_path="states/context.yaml"):
        self.config_path = Path(config_path)
        self.state_path = Path(state_path)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 配置
        self.groups: Dict[int, Set[str]] = {}
        self.private_limit: Set[int] = set()
        self.private_limit_whitelist: bool = False
        self.timeout: int = 600
        
        # 状态
        self.contexts: Dict[int, UserContext] = {}
        
        self._load_config()
        self._load_state()

    def _load_config(self):
        """加载配置"""
        if not self.config_path.exists():
            logger.warning(f"上下文配置文件不存在: {self.config_path}")
            return
        
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
            
            # 加载群组配置
            groups_data = data.get('groups', {})
            for group_id_str, aliases in groups_data.items():
                group_id = int(group_id_str)
                self.groups[group_id] = set(aliases)
            
            # 加载私聊限制
            self.private_limit = set(data.get('private_limit', []))
            self.private_limit_whitelist = data.get('private_limit_whitelist', False)
            self.timeout = data.get('timeout', 600)
            
            logger.info(f"上下文配置已加载: {self.config_path}")
        except Exception as e:
            logger.error(f"加载上下文配置失败: {e}")

    def _load_state(self):
        """加载状态"""
        if not self.state_path.exists():
            return
        
        try:
            with open(self.state_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
            
            # 加载上下文
            contexts_data = data.get('contexts', {})
            for user_id_str, context_data in contexts_data.items():
                user_id = int(user_id_str)
                expire = datetime.fromisoformat(context_data['expire']) if context_data.get('expire') else None
                
                # 检查是否过期
                if expire and datetime.now() > expire:
                    continue
                
                self.contexts[user_id] = UserContext(
                    user_id=user_id,
                    group_id=context_data.get('group_id'),
                    expire=expire,
                )
            
            logger.info(f"上下文状态已加载: {self.state_path}")
        except Exception as e:
            logger.error(f"加载上下文状态失败: {e}")

    def _save_state(self):
        """保存状态"""
        data = {
            'contexts': {},
        }
        
        for user_id, ctx in self.contexts.items():
            # 只保存未过期的上下文
            if ctx.expire and datetime.now() > ctx.expire:
                continue
            
            data['contexts'][str(user_id)] = {
                'group_id': ctx.group_id,
                'expire': ctx.expire.isoformat() if ctx.expire else None,
            }
        
        try:
            with open(self.state_path, 'w', encoding='utf-8') as f:
                yaml.dump(data, f, allow_unicode=True)
        except Exception as e:
            logger.error(f"保存上下文状态失败: {e}")

    def set_context(self, user_id, group_id=None):
        """
        设置用户上下文
        
        Args:
            user_id: 用户 QQ 号
            group_id: 群号，None 表示清除上下文
        
        Returns:
            是否成功
        """
        if group_id is None:
            if user_id in self.contexts:
                del self.contexts[user_id]
                self._save_state()
            return True
        
        # 检查群是否在配置中
        if group_id not in self.groups:
            return False
        
        # 设置上下文
        expire = datetime.now() + timedelta(seconds=self.timeout)
        self.contexts[user_id] = UserContext(
            user_id=user_id,
            group_id=group_id,
            expire=expire,
        )
        self._save_state()
        return True

    def get_context(self, user_id):
        """
        获取用户上下文
        
        Args:
            user_id: 用户 QQ 号
        
        Returns:
            群号，None 表示没有上下文
        """
        if user_id not in self.contexts:
            return None
        
        ctx = self.contexts[user_id]
        
        # 检查是否过期
        if ctx.expire and datetime.now() > ctx.expire:
            del self.contexts[user_id]
            self._save_state()
            return None
        
        return ctx.group_id

    def get_event_context(self, event):
        """
        获取事件的上下文
        
        Args:
            event: 消息事件
        
        Returns:
            群号
        """
        if isinstance(event, GroupMessageEvent):
            return event.group_id
        
        if isinstance(event, PrivateMessageEvent):
            return self.get_context(event.user_id)
        
        return None

    def get_group_aliases(self, group_id):
        """
        获取群的别名
        
        Args:
            group_id: 群号
        
        Returns:
            别名集合
        """
        return self.groups.get(group_id, set())

    def find_group_by_alias(self, alias):
        """
        通过别名查找群号
        
        Args:
            alias: 别名
        
        Returns:
            群号，None 表示未找到
        """
        alias = alias.lower()
        for group_id, aliases in self.groups.items():
            if alias in {a.lower() for a in aliases}:
                return group_id
        return None

    def is_private_allowed(self, user_id):
        """
        检查私聊是否被允许
        
        Args:
            user_id: 用户 QQ 号
        
        Returns:
            是否允许
        """
        if not self.private_limit:
            return True
        
        if self.private_limit_whitelist:
            return user_id in self.private_limit
        else:
            return user_id not in self.private_limit

    def build_permission(self, permission_path, level=PermissionLevel.MEMBER):
        """
        构建权限（兼容性方法）
        
        Args:
            permission_path: 权限路径
            level: 权限级别
        
        Returns:
            Permission 对象
        """
        # 从 permission 模块导入
        from .permission import PermissionChecker
        permission_node = '.'.join(permission_path) if isinstance(permission_path, (list, tuple)) else str(permission_path)
        return Permission(PermissionChecker(permission_node, level))


# 全局上下文管理器实例
context = Context()


def get_event_context(event):
    """
    快捷函数：获取事件的上下文
    
    Args:
        event: 消息事件
    
    Returns:
        群号
    """
    return context.get_event_context(event)


def reload_context():
    """重新加载上下文配置和状态"""
    context._load_config()
    context._load_state()


def get_context_manager():
    """获取全局上下文管理器"""
    return context

