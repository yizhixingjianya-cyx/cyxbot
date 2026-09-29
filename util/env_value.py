"""环境变量取值清洗（防「手滑把反引号抄进 .env」）

`.env` 是手写的，最常见的一种事故是把文档 / 聊天里的强调符号一起抄进值里：

.. code-block:: ini

    FTV_BASE_URL=`https://open-global.vdsentnet.com`
    GUANLI_BASE_URL=`https://1.2.3.4:28788/`

``python-dotenv`` 只会去掉**成对的单双引号**，对反引号无能为力。于是拿到的字符串
带着反引号，接着就是一连串莫名其妙的失败：

- 地址被当成非法 URL，回落成 ``127.0.0.1``，公网打不开面板；
- 自签证书的 SAN 抠不出主机名，浏览器多报一个「证书名称不匹配」；
- 请求发往 ``` `https://... ``` 直接抛异常，令牌 / 接口全部获取失败。

所以凡是「人手工填、程序当字面量用」的环境变量，取值时都过一遍 :func:`text`。

本模块另外补了一件事：:func:`load_env_file` 会把 ``.env`` 灌进 ``os.environ``。
NoneBot 自己只用 pydantic-settings「读文件」，不动 ``os.environ``，所以不补这一步的话，
所有用 ``os.getenv`` 读的配置（``IMAGE_HOST_*`` / ``GUANLI_*``）拿到的永远都是默认值。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

__all__ = ["clean", "text", "load_env_file"]

logger = logging.getLogger("util.env_value")

# None = 还没找过；"" = 找过但没找到；其余 = 已加载的文件路径
_ENV_FILE_LOADED: Optional[str] = None

# 成对包裹就剥掉；只剥成对的，避免把值里有意义的单个符号吃掉
_WRAPPERS = (
    ("`", "`"),
    ('"', '"'),
    ("'", "'"),
    ("“", "”"),
    ("‘", "’"),
    ("《", "》"),
)

# 同一个变量只提醒一次，避免每次读都刷日志
_WARNED: set = set()


def load_env_file(path: Optional[str] = None) -> str:
    """把 ``.env`` 灌进 ``os.environ``，返回实际加载的文件路径（没找到返回空串）

    **为什么必须补这一步**：NoneBot 的 ``Config`` 走的是 pydantic-settings，
    它是「直接读 .env 文件」的，**不会**把内容写进 ``os.environ``。于是同一个
    ``.env`` 会出现两种截然不同的命运：

    - 经 ``get_plugin_config()`` 读的配置（例如 ftv 的 ``FTV_*``）能拿到值；
    - 直接 ``os.getenv`` 读的配置（``IMAGE_HOST_*`` / ``GUANLI_*``）**什么都拿不到**，
      只能吃默认值 —— 表现就是面板悄悄绑到 ``127.0.0.1``、图床服务干脆不启动，
      而日志里看不出任何报错。

    所以在 ``util`` 包被首次 import 时（``bot.py`` 里早于 ``nonebot.init()``）补一次
    ``load_dotenv``，让两条路看到同一份配置。

    ``override=False``：进程里已有的真实环境变量（systemd ``Environment=``、
    docker ``-e`` 等）优先，不会被文件里的值盖掉。
    """
    global _ENV_FILE_LOADED

    if path is None and _ENV_FILE_LOADED is not None:
        return _ENV_FILE_LOADED

    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:  # pragma: no cover - 依赖缺失时只降级，不能拦住启动
        logger.warning(
            "没装 python-dotenv，.env 不会进 os.environ："
            "IMAGE_HOST_* / GUANLI_* 等只能吃默认值"
        )
        _ENV_FILE_LOADED = ""
        return ""

    target = path or find_dotenv(usecwd=True)
    if not target:
        # 兜底：项目根目录（util/env_value.py 的上一级）下的 .env
        candidate = Path(__file__).resolve().parents[1] / ".env"
        if candidate.is_file():
            target = str(candidate)

    if not target:
        logger.warning(
            "没找到 .env 文件：IMAGE_HOST_* / GUANLI_* 等只能吃默认值（面板会绑 127.0.0.1）"
        )
        _ENV_FILE_LOADED = ""
        return ""

    load_dotenv(target, override=False)
    _ENV_FILE_LOADED = target
    logger.info(f"已加载环境变量文件：{target}")
    return target


def clean(value: Optional[str]) -> str:
    """去掉首尾空白，以及成对包裹的反引号 / 引号（可嵌套，如 ``"`x`"``）"""
    text = (value or "").strip()
    while len(text) >= 2:
        for left, right in _WRAPPERS:
            if text.startswith(left) and text.endswith(right):
                text = text[len(left) : len(text) - len(right)].strip()
                break
        else:
            break
    return text


def text(name: str, default: str = "") -> str:
    """读环境变量并清洗；未设置 / 清洗后为空则返回 ``default``

    清洗掉了东西就提醒一次 —— 这类值多半是从文档里连强调符号一起抄进 ``.env`` 的，
    说得出来才能改掉（程序这边照常能用，但 ``.env`` 里始终是错的）。
    """
    raw = os.getenv(name)
    value = clean(raw)
    if raw and raw.strip() != value and name not in _WARNED:
        _WARNED.add(name)
        logger.warning(
            f"环境变量 {name} 首尾带了引号 / 反引号，已自动忽略："
            f"{raw.strip()!r} -> {value!r}（建议直接改 .env 去掉这些符号）"
        )
    return value or default
