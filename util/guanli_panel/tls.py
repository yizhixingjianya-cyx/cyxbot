"""面板自签证书管理（端口直通暴露公网时，别让口令 / Cookie 走明文）

面板默认开启 HTTPS，用**自签证书**即可：

- 首次启动自动生成到 ``src/data/guanli/tls/panel.crt`` / ``panel.key``，
  带 SAN（``GUANLI_BASE_URL`` 的主机名 + 面板设置里登记的对外地址 + 本机所有 IPv4
  + 主机名 + ``localhost`` / ``127.0.0.1``），有效期 10 年，到期前 30 天自动重新生成；
  **换了访问地址（比如原来填的是内网 IP，后来改公网 IP）也会自动重签**，
  免得浏览器多报一个「证书名称不匹配」；
- 浏览器第一次访问会提示「证书不受信任 / 不安全」，这是自签证书的正常现象，
  点「继续访问」即可（内网 / 自用面板完全够用）；
- 想换成正式证书，把 ``GUANLI_SSL_CERTFILE`` / ``GUANLI_SSL_KEYFILE`` 指向
  证书与私钥文件即可（两者必须成对提供，配了就优先用）。

配置（.env）：

- ``GUANLI_SSL``          ``false`` / ``0`` / ``off`` 关闭 HTTPS 回落明文（默认开启）
- ``GUANLI_SSL_AUTO``     自签自动生成开关，默认 ``true``；``false`` 且没给证书就不启用 HTTPS
- ``GUANLI_SSL_CERTFILE`` 自定义证书（PEM 全链）
- ``GUANLI_SSL_KEYFILE``  自定义私钥（PEM）

生成依赖 ``cryptography``（项目已装，OIDC 的 JWT 也用它）；缺库时自动回落明文 HTTP
并在日志里说明，不会影响机器人本体启动。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from nonebot.log import logger

from .config import PROJECT_ROOT, env_base_url
from ..env_value import text as env_text

__all__ = [
    "tls_settings",
    "ensure_certificates",
    "enabled",
    "tls_dir",
    "cert_sans",
]

_VALID_DAYS = 3650  # 自签证书有效期（10 年）
_RENEW_DAYS = 30  # 剩余不足 30 天就重新生成
_RUNTIME_DIR = PROJECT_ROOT / "src" / "data" / "guanli"


def _env_bool(name: str, default: bool) -> bool:
    raw = env_text(name).lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off", "none", "disable", "disabled")


def tls_dir() -> Path:
    """自签证书存放目录"""
    return _RUNTIME_DIR / "tls"


def enabled() -> bool:
    """是否启用 HTTPS（``GUANLI_SSL`` 默认开启）"""
    return _env_bool("GUANLI_SSL", True)


def tls_settings() -> Dict[str, Any]:
    """汇总 HTTPS 相关配置（便于日志 / 面板展示）"""
    return {
        "enabled": enabled(),
        "auto": _env_bool("GUANLI_SSL_AUTO", True),
        "certfile": env_text("GUANLI_SSL_CERTFILE"),
        "keyfile": env_text("GUANLI_SSL_KEYFILE"),
    }


def _host_only(raw: str) -> str:
    """从 ``IP:端口`` / ``https://域名/`` 里抠出主机名（证书 CN / SAN 用）"""
    from urllib.parse import urlparse

    text = (raw or "").strip()
    if not text:
        return ""
    try:
        url = text if "://" in text else f"//{text}"
        return (urlparse(url).hostname or "").strip()
    except Exception:
        return ""


def _host_from_base() -> str:
    """面板对外地址（``GUANLI_BASE_URL``）里的主机名"""
    return _host_only(env_base_url() or "")


def cert_sans(extra_hosts: Iterable[str] = ()) -> List[str]:
    """证书要覆盖的地址集合：显式给的地址 + 对外地址 + 本机名 + 本机 IPv4 + 回环

    证书里没有访问地址时浏览器会额外报「名称不匹配」，所以把「用户可能用哪个地址
    打开面板」都放进来：``GUANLI_BASE_URL``、面板设置里登记的对外地址（由调用方
    以 ``extra_hosts`` 传入）、本机主机名与全部 IPv4。
    """
    import socket

    hosts: List[str] = []
    for raw in (*extra_hosts, _host_from_base()):
        item = _host_only(raw)
        if item and item not in hosts:
            hosts.append(item)
    for item in ("localhost", "127.0.0.1", "::1"):
        if item not in hosts:
            hosts.append(item)
    try:
        name = socket.gethostname()
        if name and name not in hosts:
            hosts.append(name)
        for ip in socket.gethostbyname_ex(name)[2]:
            if ip and ip not in hosts:
                hosts.append(ip)
    except Exception:
        pass
    return hosts


def _expired(cert_path: Path) -> bool:
    """证书是否已过期 / 快到期（读不了就当坏的，触发重新生成）"""
    try:
        from cryptography import x509
    except Exception:
        # 没装 cryptography 就判断不了，直接沿用现成证书
        return False
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        not_after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after.replace(
            tzinfo=timezone.utc
        )
        return not_after < datetime.now(timezone.utc) + timedelta(days=_RENEW_DAYS)
    except Exception:
        return True


def _cert_covers(cert_path: Path, wanted: Iterable[str]) -> bool:
    """现有证书的 SAN 是否已覆盖 ``wanted`` 里的地址（缺了就重签）

    读不出来（文件损坏 / 缺 cryptography）按「覆盖」处理，交给 ``_expired`` 决定，
    避免因为解析不了就反复重签。
    """
    wanted_list = [str(h).strip().lower() for h in wanted if str(h or "").strip()]
    if not wanted_list:
        return True
    try:
        from cryptography import x509
    except Exception:
        return True
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        have = {str(v).lower() for v in san.get_values_for_type(x509.DNSName)}
        have |= {str(v).lower() for v in san.get_values_for_type(x509.IPAddress)}
    except Exception:
        return False
    return all(host in have for host in wanted_list)


def _write_file(path: Path, data: bytes, *, secret: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    if secret:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _generate(cert_path: Path, key_path: Path, extras: Optional[List[str]] = None) -> bool:
    """生成自签证书（RSA 2048 + SHA256，带 SAN）"""
    try:
        import ipaddress

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except Exception as e:
        logger.warning(
            f"未安装 cryptography（{e!r}），无法生成自签证书，"
            "面板将使用明文 HTTP。可 pip install cryptography 后重启。"
        )
        return False

    try:
        extras = extras or []
        hosts = cert_sans(extras)
        first_extra = next((_host_only(h) for h in extras if _host_only(h)), "")
        common_name = _host_from_base() or first_extra or "cangyao-panel"
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        alt: List[Any] = []
        for host in hosts:
            try:
                alt.append(x509.IPAddress(ipaddress.ip_address(host)))
            except ValueError:
                alt.append(x509.DNSName(host))
        if not alt:
            alt.append(x509.DNSName("localhost"))

        now = datetime.now(timezone.utc)
        subject = x509.Name(
            [
                x509.NameAttribute(NameOID.COMMON_NAME, common_name),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "cangyao panel"),
            ]
        )
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=_VALID_DAYS))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
            )
            .sign(key, hashes.SHA256())
        )
        _write_file(cert_path, cert.public_bytes(serialization.Encoding.PEM))
        _write_file(
            key_path,
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ),
            secret=True,
        )
        return True
    except Exception as e:
        logger.error(f"生成自签证书失败：{e!r}，面板将使用明文 HTTP")
        return False


def ensure_certificates(extra_hosts: Iterable[str] = ()) -> Optional[Tuple[str, str]]:
    """返回可用的 ``(证书路径, 私钥路径)``；不可用返回 ``None``（表示走明文 HTTP）

    ``extra_hosts`` 是「用户可能用来访问面板的其他地址」（例如面板设置里登记的
    对外地址）。自签证书会按它们扩 SAN；现有证书覆盖不全时会自动重签，
    免得换个地址访问就报「证书名称不匹配」。
    """
    if not enabled():
        return None

    extras = [str(h).strip() for h in extra_hosts if str(h or "").strip()]
    settings = tls_settings()
    cert_raw, key_raw = settings["certfile"], settings["keyfile"]
    if cert_raw or key_raw:
        if cert_raw and key_raw and Path(cert_raw).is_file() and Path(key_raw).is_file():
            return cert_raw, key_raw
        logger.error(
            "GUANLI_SSL_CERTFILE / GUANLI_SSL_KEYFILE 不完整或文件不存在，已忽略自定义证书"
        )

    cert_path = tls_dir() / "panel.crt"
    key_path = tls_dir() / "panel.key"
    wanted = cert_sans(extras)
    if cert_path.is_file() and key_path.is_file() and not _expired(cert_path):
        if _cert_covers(cert_path, wanted):
            return str(cert_path), str(key_path)
        logger.info(
            f"自签证书未覆盖 {wanted} 里的某些地址，正在重新签发（换过访问地址就会走到这里）"
        )

    if not settings["auto"]:
        logger.warning("GUANLI_SSL_AUTO=false 且没有可用证书，面板将使用明文 HTTP")
        return None

    if _generate(cert_path, key_path, extras):
        logger.success(
            f"面板已生成自签证书：{cert_path}（覆盖地址 {wanted}，有效期 {_VALID_DAYS // 365} 年）"
        )
        return str(cert_path), str(key_path)
    return None
