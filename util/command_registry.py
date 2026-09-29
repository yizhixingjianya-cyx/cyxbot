"""on_command 主指令名记录（供指令同步选用“主指令名称”而非最短别名）

NoneBot 在把 cmd 与 aliases 合并进匹配规则时会去重成一个无序集合，
事后无法从 CommandRule 还原哪个是主指令。因此在本包导入（早于任何插件导入）
时对 `nonebot.on_command` 做一次透明包装，把注册瞬间的 主指令→别名 关系记下来。

- register(): 记录 每个触发词 → 该次注册的主指令
- resolve_primary(parts): 由触发词元组还原主指令文本（未记录返回 None）
- install(): 替换 nonebot.on_command / nonebot.plugin.on_command /
  nonebot.plugin.on.on_command / util.command.on_command 四处绑定，保证所有插件
  （含 util.cmdbuild 走 command.build）在注册时都被记录。
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple, Union

# 触发词元组(str,...) -> 该触发词归属的主指令元组(str,...)
_PRIMARY: Dict[Tuple[str, ...], Tuple[str, ...]] = {}


def _to_parts(cmd: Union[str, Tuple[str, ...]]) -> Tuple[str, ...]:
    if isinstance(cmd, str):
        return (cmd,)
    if isinstance(cmd, (list, tuple)):
        return tuple(str(p) for p in cmd)
    return (str(cmd),) if cmd else ()


def register(cmd, aliases=None) -> None:
    """记录一次 on_command 注册：cmd 为主指令，aliases 为别名"""
    main = _to_parts(cmd)
    if not main or not any(main):
        return
    _PRIMARY[main] = main
    for a in aliases or ():
        parts = _to_parts(a)
        if parts and any(parts):
            _PRIMARY.setdefault(parts, main)


def resolve_primary(parts: Union[str, Tuple[str, ...]]) -> Optional[str]:
    """触发词 → 主指令文本；未知触发词返回 None"""
    key = (parts,) if isinstance(parts, str) else tuple(parts)
    primary = _PRIMARY.get(key)
    return "".join(primary) if primary else None


def _make_wrapper(real):
    """包装真正的 nonebot.on_command，注册时记录主指令后照常转发"""

    def wrapped_on_command(
        cmd,
        rule=None,
        aliases=None,
        force_whitespace=None,
        _depth=0,
        **kwargs,
    ):
        try:
            if cmd is not None:
                register(cmd, aliases)
        except Exception:
            pass
        # 包装器自身多占一层调用栈，需补偿 1 层，
        # 否则 NoneBot 会把 matcher 归属成 util.command_registry
        # （同 NoneBot CommandGroup 内部 +_depth=1 的做法）
        return real(
            cmd,
            rule=rule,
            aliases=aliases,
            force_whitespace=force_whitespace,
            _depth=_depth + 1,
            **kwargs,
        )

    wrapped_on_command.__name__ = getattr(real, "__name__", "on_command")
    wrapped_on_command.__qualname__ = getattr(real, "__qualname__", "on_command")
    wrapped_on_command.__doc__ = getattr(real, "__doc__", "")
    wrapped_on_command._cmd_primary_registry_wrapped = True  # type: ignore[attr-defined]
    return wrapped_on_command


def install() -> bool:
    """把包装器装到各 on_command 导出点，幂等"""
    try:
        import importlib
        import nonebot
        from nonebot import plugin as _plugin

        # nonebot.plugin.on 名字已被 on.py 导出的 on 装饰器占用，
        # 必须用 importlib 精确拿到 on.py 模块本体
        on_module = importlib.import_module("nonebot.plugin.on")
        real = getattr(on_module, "on_command", None)
    except Exception:
        return False
    if real is None:
        return False
    if getattr(real, "_cmd_primary_registry_wrapped", False):
        return True  # 已包装过

    wrapper = _make_wrapper(real)
    # 各导出点全部换成包装器，保证“不管插件从哪个入口 import”都被记录
    try:
        setattr(on_module, "on_command", wrapper)
    except Exception:
        pass
    try:
        setattr(_plugin, "on_command", wrapper)
    except Exception:
        pass
    try:
        nonebot.on_command = wrapper  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        from . import command as _cmd_module

        _cmd_module.on_command = wrapper  # util.command 里已绑定的全局引用
    except Exception:
        pass
    return True
