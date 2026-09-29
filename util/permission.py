
"""
权限系统模块
提供树形权限系统，支持角色、权限节点、过期时间、上下文等功能
"""
import yaml
import os
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Set, Union
from pathlib import Path
from dataclasses import dataclass, field
from enum import IntEnum

from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent, MessageEvent
from nonebot.permission import Permission
from nonebot import get_driver

from .config_manager import get_config_manager

logger = logging.getLogger(__name__)


class PermissionLevel(IntEnum):
    """权限级别枚举"""
    MEMBER = 0
    ADMIN = 1
    OWNER = 2
    SUPER = 3


@dataclass
class PermissionEntry:
    """权限节点条目"""
    node: str
    value: bool
    expire: Optional[datetime] = None
    context: Optional[Dict[str, Any]] = None

    def is_valid(self, ctx=None):
        """检查权限是否有效"""
        if self.expire and datetime.now() > self.expire:
            return False
        if self.context and ctx:
            # 检查上下文是否匹配
            for key, value in self.context.items():
                if key in ctx and ctx[key] != value:
                    return False
        return True


@dataclass
class Role:
    """角色类"""
    name: str
    priority: int = 0
    parents: List[str] = field(default_factory=list)
    entries: List[PermissionEntry] = field(default_factory=list)


@dataclass
class UserPermission:
    """用户权限类"""
    qq: int
    roles: List[str] = field(default_factory=list)
    entries: List[PermissionEntry] = field(default_factory=list)
    level: Optional[PermissionLevel] = None


class PermissionManager:
    """权限管理器"""

    def __init__(self, config_path="configs/permission.yaml"):
        self.config_path = Path(config_path)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        
        self.roles: Dict[str, Role] = {}
        self.users: Dict[int, UserPermission] = {}
        self.command_levels: Dict[str, PermissionLevel] = {}
        
        self._load_config()

    def _load_config(self):
        """加载权限配置"""
        if not self.config_path.exists():
            logger.warning(f"权限配置文件不存在: {self.config_path}")
            return
        
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
            
            # 加载角色
            roles_data = data.get('roles', {})
            for role_name, role_data in roles_data.items():
                role = Role(
                    name=role_name,
                    priority=role_data.get('priority', 0),
                    parents=role_data.get('parents', []),
                )
                
                # 加载权限条目
                entries = role_data.get('entries', [])
                for entry_data in entries:
                    entry = PermissionEntry(
                        node=entry_data['node'],
                        value=entry_data['value'],
                        expire=datetime.fromisoformat(entry_data['expire']) if entry_data.get('expire') else None,
                        context=entry_data.get('context'),
                    )
                    role.entries.append(entry)
                
                self.roles[role_name] = role
            
            # 加载用户
            users_data = data.get('users', {})
            for qq_str, user_data in users_data.items():
                qq = int(qq_str)
                user = UserPermission(
                    qq=qq,
                    roles=user_data.get('roles', []),
                    level=PermissionLevel[user_data['level']] if user_data.get('level') else None,
                )
                
                # 加载权限条目
                entries = user_data.get('entries', [])
                for entry_data in entries:
                    entry = PermissionEntry(
                        node=entry_data['node'],
                        value=entry_data['value'],
                        expire=datetime.fromisoformat(entry_data['expire']) if entry_data.get('expire') else None,
                        context=entry_data.get('context'),
                    )
                    user.entries.append(entry)
                
                self.users[qq] = user
            
            # 加载命令权限级别
            self.command_levels = {
                node: PermissionLevel[level_str]
                for node, level_str in data.get('commands', {}).items()
            }
            
            logger.info(f"权限配置已加载: {self.config_path}")
        except Exception as e:
            logger.error(f"加载权限配置失败: {e}")

    def _save_config(self):
        """保存权限配置"""
        data = {
            'roles': {},
            'users': {},
            'commands': {},
        }
        
        # 保存角色
        for role_name, role in self.roles.items():
            data['roles'][role_name] = {
                'priority': role.priority,
                'parents': role.parents,
                'entries': [
                    {
                        'node': entry.node,
                        'value': entry.value,
                        'expire': entry.expire.isoformat() if entry.expire else None,
                        'context': entry.context,
                    }
                    for entry in role.entries
                ],
            }
        
        # 保存用户
        for qq, user in self.users.items():
            data['users'][str(qq)] = {
                'roles': user.roles,
                'level': user.level.name if user.level else None,
                'entries': [
                    {
                        'node': entry.node,
                        'value': entry.value,
                        'expire': entry.expire.isoformat() if entry.expire else None,
                        'context': entry.context,
                    }
                    for entry in user.entries
                ],
            }
        
        # 保存命令级别
        data['commands'] = {
            node: level.name
            for node, level in self.command_levels.items()
        }
        
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                yaml.dump(data, f, allow_unicode=True)
            logger.info(f"权限配置已保存: {self.config_path}")
        except Exception as e:
            logger.error(f"保存权限配置失败: {e}")

    def _check_node_hierarchy(self, node, entries, ctx=None):
        """
        检查权限节点层级
        
        Returns:
            True: 有明确权限允许
            False: 有明确权限禁止
            None: 无明确权限，需要继续检查父节点
        """
        # 优先检查精确匹配的节点
        for entry in entries:
            if entry.node == node and entry.is_valid(ctx):
                return entry.value
        
        # 检查父节点（树形结构）
        parts = node.split('.')
        for i in range(len(parts) - 1, 0, -1):
            parent_node = '.'.join(parts[:i])
            for entry in entries:
                if entry.node == parent_node and entry.is_valid(ctx):
                    return entry.value
        
        # 检查根节点
        for entry in entries:
            if entry.node == '.' and entry.is_valid(ctx):
                return entry.value
        
        return None

    def _get_all_entries(self, qq):
        """获取用户所有有效权限条目"""
        all_entries = []
        
        # 获取用户角色及其继承的角色的权限
        if qq in self.users:
            user = self.users[qq]
            
            # 收集所有角色（包括继承的）
            roles = set(user.roles + ['default'])
            changed = True
            while changed:
                changed = False
                for role_name in list(roles):
                    if role_name in self.roles:
                        role = self.roles[role_name]
                        for parent in role.parents:
                            if parent not in roles:
                                roles.add(parent)
                                changed = True
            
            # 按优先级排序角色
            sorted_roles = sorted(
                [self.roles[r] for r in roles if r in self.roles],
                key=lambda r: -r.priority,  # 优先级高的在前
            )
            
            # 添加角色的权限条目
            for role in sorted_roles:
                all_entries.extend(role.entries)
            
            # 添加用户自己的权限条目（优先级最高）
            all_entries.extend(user.entries)
        
        return all_entries

    def has_permission(self, qq, node, ctx=None):
        """
        检查用户是否有指定权限
        
        Args:
            qq: 用户 QQ 号
            node: 权限节点
            ctx: 上下文
        
        Returns:
            是否有此权限
        """
        # 检查是否是超级用户
        config_manager = get_config_manager()
        superusers = config_manager.get("bot.superusers", [])
        if str(qq) in superusers:
            return True
        
        entries = self._get_all_entries(qq)
        result = self._check_node_hierarchy(node, entries, ctx)
        
        if result is not None:
            return result
        
        # 默认权限
        return False

    def get_user_level(self, qq):
        """
        获取用户的权限级别
        
        Args:
            qq: 用户 QQ 号
        
        Returns:
            权限级别
        """
        # 检查是否是超级用户
        config_manager = get_config_manager()
        superusers = config_manager.get("bot.superusers", [])
        if str(qq) in superusers:
            return PermissionLevel.SUPER
        
        if qq in self.users and self.users[qq].level:
            return self.users[qq].level
        
        return PermissionLevel.MEMBER

    def get_command_level(self, node):
        """
        获取命令的权限级别
        
        Args:
            node: 权限节点
        
        Returns:
            权限级别
        """
        return self.command_levels.get(node, PermissionLevel.MEMBER)


class PermissionChecker:
    """权限检查器"""

    def __init__(self, permission_node, default_level=PermissionLevel.MEMBER):
        self.permission_node = permission_node
        self.default_level = default_level

    async def __call__(self, event):
        """
        检查权限
        
        Args:
            event: 消息事件
        
        Returns:
            是否有权限
        """
        qq = event.user_id
        ctx = None
        
        # 构建上下文
        if isinstance(event, GroupMessageEvent):
            ctx = {
                'group': event.group_id,
                'level': 'admin' if event.sender.role in ['admin', 'owner'] else 'member',
                'admin': event.sender.role in ['admin', 'owner'],
            }
        
        # 检查权限节点
        if permission_manager.has_permission(qq, self.permission_node, ctx):
            return True
        
        # 检查权限级别
        user_level = permission_manager.get_user_level(qq)
        command_level = permission_manager.get_command_level(self.permission_node)
        command_level = max(command_level, self.default_level)
        
        # 检查用户级别
        if user_level >= command_level:
            return True
        
        # 群聊中检查管理员身份
        if isinstance(event, GroupMessageEvent):
            if event.sender.role == 'owner' and command_level <= PermissionLevel.OWNER:
                return True
            if event.sender.role == 'admin' and command_level <= PermissionLevel.ADMIN:
                return True
        
        return False


# 全局权限管理器实例
permission_manager = PermissionManager()
permission = PermissionLevel


def reload_permission():
    """重新加载权限配置"""
    permission_manager._load_config()


def get_permission_manager():
    """获取全局权限管理器"""
    return permission_manager

