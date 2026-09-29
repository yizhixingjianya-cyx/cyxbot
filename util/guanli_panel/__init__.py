"""guanli 管理面板 · 基础设施层

放在 ``util`` 下（与 ``permission`` / ``configs`` / ``context`` 同级），由薄插件
``src/plugins/guanli`` 引导加载，指令归属仍记为 guanli，与 ``configs/commands.json`` 一致。

组成：

- ``config.py``  读取 ``src/data/configs/guanli.yaml`` 并处理面板环境变量
- ``store.py``   SQLite 数据层（违规计数 / 群设置 / 键值 / 面板账号 / 登录会话），
  库文件 ``src/data/guanli_panel.db``；首次建库会从旧版 ``src/data/guanli.db`` 只读搬迁一次数据
- ``tls.py``     面板自签证书（默认开 HTTPS，证书生成到 ``src/data/guanli/tls/``）
- ``web.py``     FastAPI 管理面板，默认监听 ``0.0.0.0:28788``（端口直通对外，不需要 nginx）；
  登录支持「账号密码 / OIDC 单点登录 / 应急访问口令」三条渠道
- ``panel.py``   QQ 侧指令 + 违禁词自动监测
- ``static/``    面板前端（暖奶油 + 琥珀配色，取自 cafe.weibazhen.com 设计令牌）

配套的对外服务（都是端口直通，不走 nginx）：

- 面板 ``28788``：本包，默认 HTTPS（自签证书）
- 图床 ``11451``：``util/static_server.py``，只读提供 ``util/image_host.py`` 落盘的图片

热重载：

1. 前端每次请求都从磁盘读，替换 ``static/index.html`` 后刷新浏览器即生效；
2. 后端 ``.py`` 改动由 NoneBot ``hot_reload`` 自动重载，或在面板点「热重载」；
3. 面板自身被重载时靠挂在 ``nonebot`` 模块上的句柄保证不会重复占用 28788 端口。
"""

from __future__ import annotations

__all__ = ["setup_panel", "is_panel_running", "panel_base_url", "panel_token", "get_config", "store"]

# 钩子只注册一次的标记（重复重载插件时避免钩子堆积）
_HOOK_FLAG = "_guanli_panel_hooked"


def setup_panel() -> None:
    """注册面板进程钩子 + 注册 QQ 指令（插件每次加载都会调用本函数）

    分两件事，且都做成可重复调用：

    1. 启动 / 停止钩子只注册一次（记在 ``nonebot`` 模块上，插件重载不会重复挂）；
    2. ``panel`` 指令模块每次都先丢掉再导入 —— NoneBot 重载插件时会先卸载旧处理器，
       若这里复用缓存模块就会「指令消失」，所以必须让它重新执行一遍。
    """
    import importlib
    import sys

    import nonebot
    from nonebot.log import logger

    if not getattr(nonebot, _HOOK_FLAG, False):
        setattr(nonebot, _HOOK_FLAG, True)

        driver = nonebot.get_driver()

        @driver.on_startup
        async def _guanli_panel_start() -> None:
            """Web 面板跑在后台线程里，失败不影响机器人本体"""
            from .web import start_server

            try:
                start_server()
            except Exception as e:
                logger.error(f"guanli 管理面板启动异常：{e!r}")

        @driver.on_shutdown
        async def _guanli_panel_stop() -> None:
            from .web import stop_server

            try:
                stop_server()
            except Exception:
                pass

    sys.modules.pop(f"{__name__}.panel", None)
    importlib.import_module(f"{__name__}.panel")


def is_panel_running() -> bool:
    """面板线程是否在跑（面板「概览」页也会展示）"""
    from .web import is_running

    return is_running()


def panel_base_url() -> str:
    """用户可见的面板地址"""
    from .web import panel_base_url as _impl

    return _impl()


def panel_token() -> str:
    """面板访问口令"""
    from .web import panel_token as _impl

    return _impl()


def get_config():
    """读取 guanli.yaml 得到的配置对象"""
    from .config import get_config as _impl

    return _impl()


def __getattr__(name: str):
    """``store`` 等子模块按需加载，避免 util 被引入时就拉起重依赖"""
    if name in ("store", "web", "config", "panel"):
        import importlib

        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
