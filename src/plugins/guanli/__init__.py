"""guanli · 管理插件（薄引导层）

面板与群管理指令的实现都在 ``util/guanli_panel``（属于基础设施，与 ``permission`` /
``configs`` / ``context`` 同级）。这里只做三件事：

1. 声明插件元信息（``configs/commands.json`` 里的 ``plugin`` 字段仍为 guanli）；
2. 把面板进程挂到 NoneBot 的启动 / 关闭钩子上；
3. 导入 ``util.guanli_panel.panel`` 完成指令注册与违禁词监测。

热重载：

- 前端 ``util/guanli_panel/static/index.html`` 每次请求都从磁盘读取，替换后刷新即生效；
- 后端 ``.py`` 改动由 NoneBot ``hot_reload`` 自动重载，或在面板点「热重载」；
- 面板自身被重载时，句柄挂在 ``nonebot`` 模块上，不会重复占用 28788 端口。
"""

from __future__ import annotations

from nonebot.plugin import PluginMetadata

from util.guanli_panel import setup_panel
from util.guanli_panel.config import GuanliConfig

__plugin_meta__ = PluginMetadata(
    name="guanli",
    description="群管理与 Web 管理面板（违禁词、禁言、群配置、指令清单、代码文件热重载）",
    usage="发送「管理面板」获取面板地址；「更新违禁词 列表」「禁言 @某人 10m」等",
    type="application",
    config=GuanliConfig,
    supported_adapters={"~onebot.v11"},
)

# 引导加载：注册面板进程钩子 + 重新执行 util.guanli_panel.panel 完成指令注册
setup_panel()

# 面板 / 图床都是「端口直通」的对外服务，顺手把图床静态服务（默认 11451）也挂上。
# setup() 内部做了只注册一次的保护，ftv 插件同样会调一次，不会重复占端口。
try:
    from util.static_server import setup as setup_image_host

    setup_image_host()
except Exception:
    pass
