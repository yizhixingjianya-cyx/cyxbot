"""ftv 图床 —— 实现已上移到 ``util/image_host.py``，本文件只做转发。

抽到 ``util`` 层是因为「把本机生成的图片换成公网 URL」是通用能力，不该埋在 ftv 里。
（「每日鉴毛」用不上它：那个插件的图在源站上，直接推源站链接即可。）

这里保留旧的导入路径（``from .image_host import host_image, image_markdown``）不动，
并继续兼容 ``FTV_IMAGE_*`` 环境变量 —— 新模块同时接受 ``IMAGE_HOST_*`` 通用名，
两套名字任填其一即可（通用名优先）。

新代码请直接 ``from util import host_image, image_markdown``。
"""

from __future__ import annotations

from util.image_host import (
    host_bytes,
    host_image,
    hosting_enabled,
    image_base_url,
    image_markdown,
    md_display_size,
    sniff_ext,
)

__all__ = [
    "host_image",
    "host_bytes",
    "image_markdown",
    "md_display_size",
    "hosting_enabled",
    "sniff_ext",
    "image_base_url",
]
