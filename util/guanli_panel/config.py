"""guanli 管理面板 · 配置

面板实现在 ``util/guanli_panel``，本模块只负责配置读取。

配置文件：``src/data/configs/guanli.yaml``（与 config.yaml 的 ``plugins.guanli`` 同义，
本插件以独立 yaml 为准，缺省时自动生成）。

面板相关参数走环境变量（.env），因为它们属于部署信息，不适合写进会被面板回写的 yaml：

- ``GUANLI_PORT``    面板监听端口，默认 ``28788``
- ``GUANLI_HOST``    面板监听地址，默认 ``127.0.0.1``（只给本机反代用）。
                    端口直通对外时必须设成 ``0.0.0.0``，否则公网「拒绝连接」
- ``GUANLI_TOKEN``   面板访问口令；不设置则首次启动随机生成并写进 ``src/data/guanli/panel.json``
- ``GUANLI_BASE_URL`` 生成给用户看的完整面板地址，例如 ``https://1.2.3.4:28788/``；
                      端口直通场景要带端口，否则按钮会拼出打不开的链接。
                      这个地址里的主机名也会被写进自签证书的 SAN，所以公网 IP 要写在这里

HTTPS（端口直通就把面板摆在公网上，默认开自签证书，详见 ``tls.py``）：

- ``GUANLI_SSL``          ``false`` / ``0`` / ``off`` 关闭 HTTPS 回落明文，默认开启
- ``GUANLI_SSL_AUTO``     自签证书自动生成开关，默认 ``true``
- ``GUANLI_SSL_CERTFILE`` 自定义证书 PEM（与 KEYFILE 成对提供，配了就优先用）
- ``GUANLI_SSL_KEYFILE``  自定义私钥 PEM

口令强度（``store.password_policy_error`` 用）：

- ``GUANLI_PASSWORD_MIN`` 面板账号口令最小长度，默认 ``12``（下限 8）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from ..env_value import text as env_text

# util/guanli_panel/config.py -> 项目根
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

# 面板静态资源目录（前端 index.html 放这里，替换文件后刷新即可生效）
STATIC_DIR: Path = Path(__file__).resolve().parent / "static"


class GuanliConfig(BaseModel):
    """管理插件配置，字段与 src/data/configs/guanli.yaml 一一对应"""

    anti_keywords: List[str] = Field(default_factory=list, description="全局违禁词列表")
    anti_warn_limit: int = Field(3, description="触发禁言的违规次数阈值")
    anti_mute_duration: int = Field(10800, description="禁言时长（秒）")
    database_url: str = Field(
        "sqlite:///src/data/guanli_panel.db", description="数据库 URL"
    )


def _env_int(name: str, default: int) -> int:
    raw = env_text(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_port() -> int:
    """面板端口，默认 28788"""
    return _env_int("GUANLI_PORT", 28788)


def env_host() -> str:
    """面板监听地址，默认仅本机；端口直通对外时在 .env 里设 GUANLI_HOST=0.0.0.0"""
    return env_text("GUANLI_HOST", "127.0.0.1")


def env_token() -> Optional[str]:
    """环境变量里显式配置的面板口令，未配置返回 None"""
    return env_text("GUANLI_TOKEN") or None


def env_base_url() -> Optional[str]:
    """用户可见的面板完整地址；未配置返回 None（由运行时按端口兜底拼接）"""
    return env_text("GUANLI_BASE_URL") or None


def env_password_min() -> int:
    """面板账号口令最小长度，默认 12（下限 8）"""
    return max(8, _env_int("GUANLI_PASSWORD_MIN", 12))


# ------------------------------------------------------------------ 登录：初始管理员
def env_admin_user() -> str:
    """初始管理员用户名，默认 ``admin``"""
    return env_text("GUANLI_ADMIN_USER") or "admin"


def env_admin_password() -> Optional[str]:
    """初始管理员密码；未配置则首次启动随机生成并打进日志"""
    return env_text("GUANLI_ADMIN_PASSWORD") or None


# ------------------------------------------------------------------ 登录：OIDC
def _env_bool(name: str, default: bool) -> bool:
    raw = env_text(name).lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off", "none", "")


def _env_list(name: str) -> List[str]:
    """逗号 / 空格 / 换行分隔的列表，元素统一小写去空"""
    raw = env_text(name).replace("\n", ",").replace(" ", ",")
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def oidc_settings() -> Dict[str, Any]:
    """汇总 OIDC（OpenID Connect 授权码模式）配置。

    面板走标准 authorization code + PKCE 流程，兼容 Keycloak / Authentik /
    Authelia / Auth0 / 飞书 Lark / 阿里云 IDaaS 等只要暴露
    ``/.well-known/openid-configuration`` 的服务端。

    必填：``GUANLI_OIDC_ISSUER`` + ``GUANLI_OIDC_CLIENT_ID``（两者齐全才算启用）。
    """
    issuer = env_text("GUANLI_OIDC_ISSUER").rstrip("/")
    client_id = env_text("GUANLI_OIDC_CLIENT_ID")
    role = env_text("GUANLI_OIDC_DEFAULT_ROLE").lower()
    if role not in ("admin", "viewer"):
        role = "admin"
    return {
        "enabled": bool(issuer and client_id),
        "issuer": issuer,
        "client_id": client_id,
        "client_secret": env_text("GUANLI_OIDC_CLIENT_SECRET"),
        # 留空则运行时按面板对外地址拼 <base>/api/oidc/callback
        "redirect_uri": env_text("GUANLI_OIDC_REDIRECT_URI"),
        "scopes": env_text("GUANLI_OIDC_SCOPES") or "openid profile email",
        "name": env_text("GUANLI_OIDC_NAME") or "统一身份认证",
        # 首次登录是否自动建号；关掉就只能登录已在面板里建好的账号
        "auto_create": _env_bool("GUANLI_OIDC_AUTO_CREATE", True),
        "default_role": role,
        # 这两项留空表示不限制（此时 IdP 里任何用户都能进面板，建议至少配一个）
        "allowed_emails": _env_list("GUANLI_OIDC_ALLOWED_EMAILS"),
        "allowed_domains": _env_list("GUANLI_OIDC_ALLOWED_DOMAINS"),
        "require_verified_email": _env_bool("GUANLI_OIDC_REQUIRE_VERIFIED_EMAIL", False),
        # 会话有效期（小时），默认 7 天
        "session_hours": _env_int("GUANLI_SESSION_HOURS", 7 * 24),
    }


def session_hours() -> int:
    """面板会话有效期（小时）"""
    return max(1, _env_int("GUANLI_SESSION_HOURS", 7 * 24))


def get_config() -> GuanliConfig:
    """读取 guanli.yaml；不存在时用默认值生成一份"""
    from util.plugin_config import get_plugin_config

    cfg = get_plugin_config("guanli", GuanliConfig)
    return cfg or GuanliConfig()


def save_config(cfg: GuanliConfig) -> bool:
    """回写 guanli.yaml"""
    from util.plugin_config import save_plugin_config

    return save_plugin_config("guanli", cfg)
