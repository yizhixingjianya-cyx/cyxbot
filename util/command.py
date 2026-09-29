
"""
命令构建器模块
提供 CommandBuilder 类用于构建命令，集成权限和上下文系统
"""
import re
from typing import Any, Callable, Optional, Sequence, Set, Union, TypeVar, Tuple
from dataclasses import dataclass, field
from functools import wraps

import nonebot
from nonebot import on_command, on_message, on_notice, on_request
from nonebot.matcher import Matcher
from nonebot.permission import Permission
from nonebot.typing import T_State, T_RuleChecker, T_PermissionChecker, T_Handler
from nonebot.adapters.onebot.v11 import (
    MessageEvent,
    GroupMessageEvent,
    PrivateMessageEvent,
    Message,
    MessageSegment,
    GroupIncreaseNoticeEvent,
    GroupDecreaseNoticeEvent,
    PokeNotifyEvent,
    FriendAddNoticeEvent,
    FriendRequestEvent,
    GroupRequestEvent,
)
from nonebot.params import CommandArg, Arg, ArgPlainText, CommandStart

from .context import Context, context, get_event_context
from .permission import PermissionLevel, permission, PermissionChecker

T = TypeVar("T")


@dataclass
class CommandBuilder:
    """
    命令构建器
    用于快速构建和配置命令，支持权限、上下文、事件等多种配置
    """
    permission_node: str  # 权限节点名
    command_name: str  # 命令名
    aliases: Set[str] = field(default_factory=set)
    _level: PermissionLevel = PermissionLevel.MEMBER
    _rule: Optional[T_RuleChecker] = None
    _priority: int = 5
    _block: bool = True
    _in_group: bool = False
    _has_group: Sequence[int] = field(default_factory=list)
    _permission: Optional[Permission] = None
    _state: Optional[T_State] = None
    _temp: bool = False
    _expire_time: Optional[float] = None
    _priority_rule: bool = False

    def level(self, level):
        """
        设置命令的权限级别
        
        Args:
            level: 权限级别，支持 PermissionLevel 枚举或字符串
        """
        if isinstance(level, str):
            level_map = {
                "member": PermissionLevel.MEMBER,
                "admin": PermissionLevel.ADMIN,
                "owner": PermissionLevel.OWNER,
                "super": PermissionLevel.SUPER,
            }
            level = level_map.get(level.lower(), PermissionLevel.MEMBER)
        self._level = level
        return self

    def alias(self, *aliases):
        """
        设置命令的别名
        """
        self.aliases.update(aliases)
        return self

    def rule(self, rule):
        """
        设置命令的规则
        """
        self._rule = rule
        return self

    def priority(self, priority):
        """
        设置命令的优先级
        """
        self._priority = priority
        return self

    def block(self, block=True):
        """
        设置命令是否阻止后续匹配
        """
        self._block = block
        return self

    def permission(self, perm):
        """
        直接设置权限对象
        """
        self._permission = perm
        return self

    def in_group(self, *groups):
        """
        设置命令仅在指定群聊中可用
        不指定群号则表示任何群聊都可用
        """
        self._in_group = True
        self._has_group = list(groups) if groups else []
        return self

    def has_group(self, *groups):
        """
        设置命令需要用户在指定群聊中
        """
        self._has_group = list(groups)
        return self

    def state(self, state):
        """
        设置命令的初始状态
        """
        self._state = state
        return self

    def temp(self, expire_time=None):
        """
        设置为临时命令
        """
        self._temp = True
        if expire_time is not None:
            self._expire_time = expire_time
        return self

    def _build_permission(self):
        """构建权限检查器"""
        if self._permission:
            return self._permission
        
        return Permission(PermissionChecker(self.permission_node, self._level))

    def _build_rule(self):
        """构建规则检查器"""
        command_name = self.command_name
        permission_node = self.permission_node
        
        async def context_rule(event):
            ctx = get_event_context(event)
            nonebot.logger.debug(f"命令规则检查: {command_name} (权限节点: {permission_node}, 事件类型: {type(event).__name__}, 用户: {event.user_id}, 上下文: {ctx})")
            
            # 检查是否需要在群聊中
            if self._in_group:
                if isinstance(event, PrivateMessageEvent):
                    nonebot.logger.debug(f"命令 {command_name} 要求群聊，但当前是私聊")
                    return False
                if self._has_group and ctx not in self._has_group:
                    nonebot.logger.debug(f"命令 {command_name} 要求在指定群聊中，但当前群聊不在列表中")
                    return False
            
            # 检查是否需要用户在指定群聊中
            elif self._has_group:
                if isinstance(event, PrivateMessageEvent):
                    # 私聊时检查用户是否设置了上下文
                    if ctx not in self._has_group:
                        nonebot.logger.debug(f"命令 {command_name} 要求用户在指定群聊中，但用户未设置上下文")
                        return False
            
            nonebot.logger.debug(f"命令规则检查通过: {command_name}")
            return True
        
        if self._rule:
            # 合并两个规则
            async def combined_rule(event):
                ctx_result = await context_rule(event)
                if not ctx_result:
                    return False
                
                # 检查 self._rule 是否是异步函数
                import inspect
                if inspect.iscoroutinefunction(self._rule):
                    rule_result = await self._rule(event)
                else:
                    rule_result = bool(self._rule(event))
                
                if rule_result:
                    nonebot.logger.debug(f"命令匹配成功: {command_name} (权限节点: {permission_node}, 用户: {event.user_id})")
                
                return rule_result
            
            return combined_rule
        return context_rule

    def build(self):
        """
        构建命令匹配器
        
        Returns:
            Matcher 对象
        """
        rule = self._build_rule()
        perm = self._build_permission()
        
        matcher = on_command(
            cmd=self.command_name,
            aliases=self.aliases,
            rule=rule,
            permission=perm,
            priority=self._priority,
            block=self._block,
            state=self._state or {},
            temp=self._temp,
            expire_time=self._expire_time,
        )
        
        # 保存权限节点信息到 matcher 中
        matcher.permission_node = self.permission_node
        matcher.permission_level = self._level
        
        import inspect
        caller_frame = inspect.stack()[1]
        caller_module = inspect.getmodule(caller_frame[0])
        plugin_name = caller_module.__name__ if caller_module else "unknown"
        
        nonebot.logger.info(f"命令已注册: {self.command_name} (权限节点: {self.permission_node}, 插件: {plugin_name})")
        
        return matcher


@dataclass
class EventBuilder:
    """
    事件构建器
    用于构建事件处理函数，支持各种事件类型
    """
    event_type: str
    _rule: Optional[T_RuleChecker] = None
    _priority: int = 5
    _block: bool = True

    def rule(self, rule):
        """设置规则"""
        self._rule = rule
        return self

    def priority(self, priority):
        """设置优先级"""
        self._priority = priority
        return self

    def block(self, block=True):
        """设置是否阻止后续匹配"""
        self._block = block
        return self

    def build(self):
        """构建事件匹配器"""
        event_mapping = {
            "group_increase": on_notice,
            "group_decrease": on_notice,
            "poke": on_notice,
            "friend_add": on_notice,
            "friend_request": on_request,
            "group_request": on_request,
            "message": on_message,
        }
        
        if self.event_type not in event_mapping:
            raise ValueError(f"不支持的事件类型: {self.event_type}")
        
        # 构建特定事件的规则
        async def event_rule(event):
            if self.event_type == "group_increase" and isinstance(event, GroupIncreaseNoticeEvent):
                return True
            if self.event_type == "group_decrease" and isinstance(event, GroupDecreaseNoticeEvent):
                return True
            if self.event_type == "poke" and isinstance(event, PokeNotifyEvent):
                return True
            if self.event_type == "friend_add" and isinstance(event, FriendAddNoticeEvent):
                return True
            if self.event_type == "friend_request" and isinstance(event, FriendRequestEvent):
                return True
            if self.event_type == "group_request" and isinstance(event, GroupRequestEvent):
                return True
            if self.event_type == "message":
                return True
            return False
        
        # 合并规则
        final_rule = event_rule
        if self._rule:
            async def combined_rule(event):
                event_result = await event_rule(event)
                if not event_result:
                    return False
                
                # 检查 self._rule 是否是异步函数
                import inspect
                if inspect.iscoroutinefunction(self._rule):
                    return await self._rule(event)
                else:
                    return bool(self._rule(event))
            
            final_rule = combined_rule
        
        return event_mapping[self.event_type](
            rule=final_rule,
            priority=self._priority,
            block=self._block,
        )


def on_group_increase():
    """新成员入群事件构建器"""
    return EventBuilder("group_increase")


def on_group_decrease():
    """成员退群事件构建器"""
    return EventBuilder("group_decrease")


def on_poke():
    """戳一戳事件构建器"""
    return EventBuilder("poke")


def on_friend_add():
    """好友添加事件构建器"""
    return EventBuilder("friend_add")


def on_friend_request():
    """好友请求事件构建器"""
    return EventBuilder("friend_request")


def on_group_request():
    """加群请求事件构建器"""
    return EventBuilder("group_request")


def cmdbuild(permission_node, command_name):
    """
    快捷函数：创建 CommandBuilder
    
    Args:
        permission_node: 权限节点
        command_name: 命令名
    
    Returns:
        CommandBuilder 对象
    """
    return CommandBuilder(permission_node, command_name)

