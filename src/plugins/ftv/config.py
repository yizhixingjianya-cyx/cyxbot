from pydantic import BaseModel
from typing import Optional
from nonebot import get_plugin_config

from util.env_value import clean


class FurtvConfig(BaseModel):
    """Furtv 插件配置"""
    # 添加 ftv_ 前缀，避免与其他插件冲突
    ftv_base_url: str = "https://open-cn1.vdsentnet.com"
    # 动态令牌获取所需配置
    ftv_app_id: str = ""
    ftv_client_secret: str = ""
    # 缓存配置
    ftv_use_localstore: bool = True  # 是否使用 localstore 插件提供的缓存目录
    ftv_no_cache: bool = False  # 是否禁用缓存


def clean_config(cfg) -> None:
    """洗掉 .env 里手滑带上的反引号 / 引号

    典型写法 ``FTV_BASE_URL=`https://open-global.vdsentnet.com` ``：
    python-dotenv 只处理成对引号，不管反引号，于是 base_url 带着反引号去请求，
    直接抛异常，表现就是日志里的「Furtv 令牌初始化失败」，很难一眼看出原因。
    """
    base = clean(getattr(cfg, "ftv_base_url", "") or "").rstrip("/")
    if base:
        cfg.ftv_base_url = base
    cfg.ftv_app_id = clean(getattr(cfg, "ftv_app_id", ""))
    cfg.ftv_client_secret = clean(getattr(cfg, "ftv_client_secret", ""))


# 全局配置实例
ftv_config: Optional[FurtvConfig] = None


def get_ftv_config() -> FurtvConfig:
    """获取插件配置"""
    global ftv_config
    if ftv_config is None:
        # 使用 get_plugin_config 获取配置
        ftv_config = get_plugin_config(FurtvConfig)
        clean_config(ftv_config)
    return ftv_config
