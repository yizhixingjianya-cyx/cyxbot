from nonebot import on_message
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import MessageEvent, GroupMessageEvent, Message
from nonebot.typing import T_State
from typing import Callable, Awaitable, Optional

class Hook:
    def on_message_sent(self, func: Callable[[Optional[Event], bool, int, Message, int], Awaitable[None]]) -> None:
        @on_message
        async def _handle_message(event: MessageEvent) -> bool:
            # 模拟API调用参数
            is_group = isinstance(event, GroupMessageEvent)
            target_id = event.group_id if is_group else event.user_id
            message = event.message
            message_id = event.message_id
            await func(event, is_group, target_id, message, message_id)
            return False
    
    def on_message_sending(self, func: Callable[[Optional[Event], bool, int, Message], Awaitable[None]]) -> None:
        @on_message
        async def _handle_message(event: MessageEvent) -> bool:
            # 模拟API调用参数
            is_group = isinstance(event, GroupMessageEvent)
            target_id = event.group_id if is_group else event.user_id
            message = event.message
            await func(event, is_group, target_id, message)
            return False

hook = Hook()
