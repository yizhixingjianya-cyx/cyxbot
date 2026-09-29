"""guanli 管理面板 Web 后端（FastAPI + uvicorn）

- 监听 ``GUANLI_HOST:GUANLI_PORT``（默认 ``127.0.0.1:28788``，端口直通对外时在 .env
  里设 ``GUANLI_HOST=0.0.0.0``）。默认走端口直通，不再需要 nginx；
  若确实要挂在 nginx ``/gl/`` 反代下，保持 ``127.0.0.1`` 即可（两条路都支持）。
- **默认开 HTTPS**（自签证书，见 ``tls.py``）：端口直通等于把面板摆在公网上，
  登录口令与 Cookie 不该走明文。自签证书首次访问浏览器会提示不受信任，点继续即可；
  关掉用 ``GUANLI_SSL=false``，也支持用 ``GUANLI_SSL_CERTFILE`` / ``GUANLI_SSL_KEYFILE``
  换成正式证书。
- 前端 ``static/index.html`` **每次请求都从磁盘重新读取**，替换文件后刷新浏览器即可生效
  （这就是面板自身的热重载）；``/api/reload`` 则用于让机器人重载配置/插件。

登录方式（三种渠道，可同时启用，前端按 ``/api/auth/config`` 渲染）：

1. **账号密码**：账号存在 ``users`` 表，口令用 scrypt 哈希，角色分 ``admin`` / ``viewer``；
   首个管理员由 ``GUANLI_ADMIN_USER`` / ``GUANLI_ADMIN_PASSWORD`` 决定，没配就随机生成并打日志。
   口令强度由 ``store.password_policy_error`` 把关（默认至少 12 位、两类字符、非弱口令）。
2. **OIDC**：标准授权码 + PKCE 流程，配好 ``GUANLI_OIDC_ISSUER`` / ``GUANLI_OIDC_CLIENT_ID``
   即启用；回调地址默认按 ``<面板对外地址>/api/oidc/callback`` 拼。
3. **访问口令**（应急兜底）：沿用 ``GUANLI_TOKEN``，用于 OIDC 挂了或忘密码时进后台。

会话：登录成功后签发随机 ``guanli_sid`` 写进 HttpOnly Cookie（HTTPS 下同时带 ``Secure``），
会话记录存在 ``sessions`` 表，所以插件热重载不会把大家踢下线。
同一来源 + 账号连续失败 8 次会被临时锁 10 分钟（见 ``login_block_seconds``）。
写操作（POST/PUT/DELETE）要求 ``admin`` 角色。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set
from urllib.parse import quote, urlencode

from nonebot.log import logger

from .config import (
    PROJECT_ROOT,
    STATIC_DIR,
    env_admin_password,
    env_admin_user,
    env_base_url,
    env_host,
    env_port,
    env_token,
    get_config,
    oidc_settings,
    save_config,
    session_hours,
    GuanliConfig,
)
from . import store
from . import tls as panel_tls

# ------------------------------------------------------------------ 常量
_RUNTIME_DIR = PROJECT_ROOT / "src" / "data" / "guanli"
_PANEL_STATE = _RUNTIME_DIR / "panel.json"
# 应急访问口令用的 Cookie（旧渠道，保留兼容）
_COOKIE_NAME = "guanli_token"
# 登录会话 Cookie：值是 sessions.sid，登录态的真正凭证
_SID_COOKIE = "guanli_sid"
# OIDC 流程中的临时 Cookie：state / nonce / PKCE verifier，回调后即删
_OIDC_COOKIE = "guanli_oidc"
_OIDC_TTL = 600
_STARTED_AT = time.time()

# 面板当前用的 HTTPS 证书（启动时探测一次，见 start_server）
_TLS_STATE: Dict[str, str] = {"certfile": "", "keyfile": ""}

# 登录失败限速：端口直通把面板摆在公网上，不挡一下就是给撞库字典开门
_LOGIN_FAILS: Dict[str, List[float]] = {}
_LOGIN_BLOCKED: Dict[str, float] = {}  # key -> 解锁时间戳（严格锁满 _LOGIN_BLOCK）
_LOGIN_WINDOW = 300.0  # 统计窗口：5 分钟
_LOGIN_MAX_FAILS = 8  # 窗口内允许的失败次数
_LOGIN_BLOCK = 600.0  # 超限后的锁定时长：10 分钟
_LOGIN_TRACK_MAX = 5000  # 记录表上限，防止被随机用户名刷爆内存
_login_lock = threading.Lock()

# 面板可读写的「代码文件」白名单扩展名
_TEXT_EXT = {
    ".py", ".json", ".yaml", ".yml", ".txt", ".md", ".html", ".htm",
    ".css", ".js", ".sh", ".toml", ".cfg", ".ini", ".env", ".csv", ".log",
}
# 不允许在面板里改写的路径片段
_DENY_PARTS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".idea", ".vscode"}
_DENY_SUFFIX = {".db", ".db-wal", ".db-shm", ".pyc", ".pyo", ".pyd", ".so", ".dll", ".exe", ".zip", ".egg-info"}

_log_buffer: "deque[str]" = deque(maxlen=500)
_state_lock = threading.Lock()

# 面板服务句柄挂在 nonebot 模块上而不是本模块：插件热重载会重建本模块的全局变量，
# 句柄放这里才能保证「同一个面板进程」不被重复启动、端口不被重复占用。
_SERVER_ATTR = "_guanli_panel_handle"

# 本包的导入路径（面板自身热重载时按它清理 sys.modules）
_PKG = "util.guanli_panel"

# 机器人事件循环句柄：同样挂在 nonebot 模块上，面板模块被重载后依然拿得到
_LOOP_ATTR = "_guanli_panel_loop"


# ------------------------------------------------------------------ 机器人事件循环
def bind_loop(loop: Any = None) -> None:
    """登记机器人所在的事件循环（QQ 侧处理器里调用一次即可）"""
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
    import nonebot

    setattr(nonebot, _LOOP_ATTR, loop)


def bot_loop() -> Optional[Any]:
    """取机器人事件循环：优先登记值，其次问驱动要"""
    import nonebot

    loop = getattr(nonebot, _LOOP_ATTR, None)
    if loop is not None and not getattr(loop, "is_closed", lambda: False)():
        return loop
    try:  # 兜底：驱动内部持有的循环
        candidate = getattr(nonebot.get_driver(), "_loop", None)
        if candidate is not None and not candidate.is_closed():
            return candidate
    except Exception:
        pass
    return None


def pick_bot(self_id: Optional[str] = None) -> Optional[Any]:
    """挑一个在线机器人实例（多账号时按 self_id 指定）"""
    try:
        from nonebot import get_bots

        bots = get_bots()
    except Exception:
        return None
    if not bots:
        return None
    if self_id and str(self_id) in bots:
        return bots[str(self_id)]
    return next(iter(bots.values()))


async def run_on_bot(coro: Any) -> Any:
    """把协程丢回机器人事件循环执行并等结果。

    面板跑在 uvicorn 自己的循环里，而 ``bot.call_api`` 必须在适配器所属的那个
    循环上等待响应，直接 await 会拿不到回包，所以这里用 run_coroutine_threadsafe 转交。
    """
    loop = bot_loop()
    if loop is None:
        raise RuntimeError("机器人事件循环尚未就绪：先在群里发一条消息让机器人收一次事件，再重试")
    if loop is asyncio.get_running_loop():
        return await coro
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return await asyncio.wrap_future(future)


async def call_bot(self_id: Optional[str], action: Callable[[Any], Any]) -> Any:
    """对指定机器人实例执行一个「bot -> 协程」的动作，返回其结果"""
    bot = pick_bot(self_id)
    if bot is None:
        raise RuntimeError("当前没有在线的机器人实例，请检查协议端连接")
    return await run_on_bot(action(bot))


# ------------------------------------------------------------------ 口令
def _load_state() -> Dict[str, Any]:
    if not _PANEL_STATE.exists():
        return {}
    try:
        return json.loads(_PANEL_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    try:
        _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        _PANEL_STATE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as e:
        logger.warning(f"guanli 面板状态写入失败：{e}")


def panel_token() -> str:
    """面板访问口令：环境变量 > 本地状态文件 > 新生成"""
    explicit = env_token()
    if explicit:
        return explicit
    with _state_lock:
        state = _load_state()
        token = state.get("token")
        if not token:
            token = secrets.token_urlsafe(18)
            state["token"] = token
            _save_state(state)
            logger.warning(f"guanli 管理面板已生成访问口令（也可用 .env 的 GUANLI_TOKEN 固定）：{token}")
        return token


def reset_token() -> str:
    """重新生成口令（面板内「重置口令」/私聊指令使用）"""
    with _state_lock:
        state = _load_state()
        token = secrets.token_urlsafe(18)
        state["token"] = token
        _save_state(state)
    return token


def panel_uses_https() -> bool:
    """面板当前是否在用 HTTPS（启动时探测到证书才算）"""
    return bool(_TLS_STATE.get("certfile"))


def tls_files() -> Dict[str, str]:
    """当前生效的证书 / 私钥路径（未启用 HTTPS 时为空串）"""
    return dict(_TLS_STATE)


def _as_https(url: str) -> str:
    """把 http:// 开头的外链升级成 https://，避免跳到没有监听的明文端口"""
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


def panel_base_url() -> str:
    """用户可见的面板地址：GUANLI_BASE_URL > 面板设置里登记的对外地址 > 本机兜底

    面板走端口直通（默认 28788）对外，所以登记 ``IP:端口`` 时直接补 ``/``，
    不再拼 nginx 的 ``/gl/`` 子路径（该反代已降级为可选的应急入口）。若面板开了 HTTPS（自签证书），
    这里会把 ``http://`` 自动改成 ``https://``，否则链接会指到一个没人监听的端口。
    """
    explicit = env_base_url()
    if explicit:
        url = explicit.rstrip("/") + "/"
        return _as_https(url) if panel_uses_https() else url
    state = _load_state()
    host = (state.get("public_host") or "").strip()
    if host:
        if "://" not in host:
            host = f"http://{host}"
        url = host.rstrip("/") + "/"
        return _as_https(url) if panel_uses_https() else url
    scheme = "https" if panel_uses_https() else "http"
    return f"{scheme}://127.0.0.1:{env_port()}/"


def set_public_host(host: str) -> str:
    """登记面板对外地址（供 QQ 指令把可点击链接发给用户）"""
    with _state_lock:
        state = _load_state()
        state["public_host"] = (host or "").strip()
        _save_state(state)
    return panel_base_url()


# ------------------------------------------------------------------ 登录会话
def session_ttl() -> int:
    return int(session_hours()) * 3600


def cookie_path() -> str:
    """Cookie 作用域取面板的挂载路径（端口直通时是 /），避免污染同域其它应用"""
    from urllib.parse import urlparse

    try:
        path = urlparse(panel_base_url()).path or "/"
    except Exception:
        path = "/"
    if not path.endswith("/"):
        path += "/"
    return path or "/"


def client_ip(request: Any) -> str:
    """真实来源 IP：外层若挂了反代会带 X-Forwarded-For"""
    try:
        fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if fwd:
            return fwd
        return request.client.host if request.client else ""
    except Exception:
        return ""


# ------------------------------------------------------------------ 登录失败限速
def _login_key(request: Any, username: Any) -> str:
    return f"{client_ip(request)}|{str(username or '').lower()}"


def _prune_fails(now: float) -> None:
    """清掉过期记录；记录表过大时整表丢弃（当作一次全局重置）"""
    if len(_LOGIN_FAILS) > _LOGIN_TRACK_MAX:
        _LOGIN_FAILS.clear()
        _LOGIN_BLOCKED.clear()
        return
    for key in [k for k, v in _LOGIN_FAILS.items() if not v or now - v[-1] > _LOGIN_WINDOW]:
        # 仍在锁定期内的 key 不能清（否则锁会被拖短）
        if _LOGIN_BLOCKED.get(key, 0.0) > now:
            continue
        _LOGIN_FAILS.pop(key, None)
    for key, until in list(_LOGIN_BLOCKED.items()):
        if until <= now:
            _LOGIN_BLOCKED.pop(key, None)


def login_block_seconds(request: Any, username: Any) -> int:
    """还需等待多少秒才能再试；``0`` 表示未被限制

    锁定是从「第 N 次失败」起**连续**算 _LOGIN_BLOCK 秒，中途不再尝试也不会提前解锁；
    未触发锁定时按 5 分钟滑动窗口计数。
    """
    now = time.time()
    key = _login_key(request, username)
    with _login_lock:
        until = _LOGIN_BLOCKED.get(key, 0.0)
        if until > now:
            return int(until - now)
        if until:
            _LOGIN_BLOCKED.pop(key, None)
        fails = [t for t in _LOGIN_FAILS.get(key, []) if now - t < _LOGIN_WINDOW]
        if fails:
            _LOGIN_FAILS[key] = fails
        else:
            _LOGIN_FAILS.pop(key, None)
        return 0


def login_record_fail(request: Any, username: Any) -> None:
    """记一次失败，用于后续限速"""
    now = time.time()
    key = _login_key(request, username)
    locked = False
    with _login_lock:
        _prune_fails(now)
        fails = [t for t in _LOGIN_FAILS.get(key, []) if now - t < _LOGIN_WINDOW]
        fails.append(now)
        _LOGIN_FAILS[key] = fails
        if len(fails) >= _LOGIN_MAX_FAILS:
            _LOGIN_BLOCKED[key] = now + _LOGIN_BLOCK
            locked = True

    if locked:
        try:
            store.add_audit(
                "面板登录限速",
                actor=str(username or ""),
                detail=f"来源 {client_ip(request)} 连续失败，已临时锁定 {int(_LOGIN_BLOCK)} 秒",
                source="panel",
            )
        except Exception:
            pass


def login_clear_fails(request: Any, username: Any) -> None:
    """登录成功后清掉失败记录与锁定"""
    key = _login_key(request, username)
    with _login_lock:
        _LOGIN_FAILS.pop(key, None)
        _LOGIN_BLOCKED.pop(key, None)


def issue_session(
    username: Any, role: str, *, provider: str = "local", request: Any = None
) -> str:
    """签发登录会话，返回要写进 Cookie 的 sid"""
    return store.create_session(
        username,
        role,
        provider=provider,
        ttl=session_ttl(),
        ip=client_ip(request) if request is not None else "",
        ua=(request.headers.get("user-agent") or "") if request is not None else "",
    )


def current_user(request: Any) -> Optional[Dict[str, Any]]:
    """解析请求身份：先看会话 Cookie（正常登录），再回退到应急访问口令"""
    sid = request.cookies.get(_SID_COOKIE)
    if sid:
        sess = store.get_session(sid)
        if sess:
            record = store.get_user(sess.get("username")) or {}
            return {
                "username": sess.get("username"),
                "role": sess.get("role") or record.get("role") or "viewer",
                "role_desc": store.role_desc(sess.get("role") or record.get("role")),
                "provider": sess.get("provider") or "local",
                "display_name": record.get("display_name") or sess.get("username"),
                "sid": sid,
                "via": "session",
            }
    supplied = (
        request.cookies.get(_COOKIE_NAME)
        or request.headers.get("x-guanli-token")
        or request.query_params.get("token")
        or ""
    )
    if supplied and secrets.compare_digest(str(supplied), panel_token()):
        return {
            "username": "token",
            "role": "admin",
            "role_desc": store.role_desc("admin"),
            "provider": "token",
            "display_name": "访问口令",
            "sid": "",
            "via": "token",
        }
    return None


def auth_config() -> Dict[str, Any]:
    """登录页需要的渠道信息（不含任何机密）"""
    oidc = oidc_settings()
    try:
        has_user = store.count_users() > 0
    except Exception:
        has_user = False
    return {
        "password": has_user,
        "token": True,
        "oidc": {
            "enabled": bool(oidc["enabled"]),
            "name": oidc["name"],
            "start": "/api/oidc/start",
            "auto_create": bool(oidc["auto_create"]),
            "default_role": oidc["default_role"],
            "restricted": bool(oidc["allowed_emails"] or oidc["allowed_domains"]),
        },
        "session_hours": int(session_hours()),
    }


def bootstrap_admin() -> None:
    """首次启动建一个管理员账号，避免面板变成没人能进的死角"""
    try:
        if store.count_users() > 0:
            return
    except Exception as e:
        logger.warning(f"guanli 面板读取账号失败（已跳过初始化）：{e!r}")
        return
    user = env_admin_user()
    configured = env_admin_password()
    password = configured or secrets.token_urlsafe(12)
    if configured:
        weak = store.password_policy_error(configured, user)
        if weak:
            logger.warning(
                f"GUANLI_ADMIN_PASSWORD 强度不足（{weak}）。面板端口直接暴露在公网上，"
                "仍按原样创建初始账号，请登录后立刻改掉"
            )
    try:
        # enforce_policy=False：弱口令也先把账号建出来，否则面板就成了没人能进的死角
        store.create_user(
            user, password, role="admin", display_name="初始管理员", enforce_policy=False
        )
    except Exception as e:
        logger.warning(f"guanli 面板创建初始管理员失败：{e!r}")
        return
    if configured:
        logger.success(f"guanli 管理面板已创建初始账号 {user}（密码取自 GUANLI_ADMIN_PASSWORD）")
    else:
        logger.warning(
            f"guanli 管理面板已创建初始账号 {user}，随机密码请在登录后立刻修改：{password}"
        )


def warn_oidc_open() -> None:
    """OIDC 没配允许名单时给出醒目提示"""
    cfg = oidc_settings()
    if cfg["enabled"] and not (cfg["allowed_emails"] or cfg["allowed_domains"]):
        logger.warning(
            "guanli 面板已启用 OIDC，但未配置 GUANLI_OIDC_ALLOWED_EMAILS / "
            "GUANLI_OIDC_ALLOWED_DOMAINS：该 IdP 下的任何用户都能登录面板，建议补上允许名单"
        )


# ------------------------------------------------------------------ OIDC（授权码 + PKCE）
_discovery_cache: Dict[str, Any] = {}
_DISCOVERY_TTL = 600


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


async def oidc_discovery() -> Dict[str, Any]:
    """拉取并缓存 IdP 的 ``.well-known/openid-configuration``"""
    cfg = oidc_settings()
    issuer = cfg["issuer"]
    if not issuer:
        raise RuntimeError("未配置 GUANLI_OIDC_ISSUER")
    cached = _discovery_cache.get(issuer)
    if cached and time.time() - cached["at"] < _DISCOVERY_TTL:
        return cached["doc"]
    import httpx

    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        resp = await client.get(issuer + "/.well-known/openid-configuration")
        resp.raise_for_status()
        doc = resp.json()
    if not isinstance(doc, dict) or not doc.get("authorization_endpoint"):
        raise RuntimeError("IdP 发现文档不完整（缺 authorization_endpoint）")
    _discovery_cache[issuer] = {"at": time.time(), "doc": doc}
    return doc


def oidc_redirect_uri() -> str:
    """回调地址：优先 GUANLI_OIDC_REDIRECT_URI，否则按面板对外地址拼"""
    configured = oidc_settings()["redirect_uri"]
    if configured:
        return configured
    return panel_base_url().rstrip("/") + "/api/oidc/callback"


async def oidc_authorize_url() -> Any:
    """生成授权跳转地址，以及要暂存到临时 Cookie 的 state / nonce / PKCE verifier"""
    cfg = oidc_settings()
    doc = await oidc_discovery()
    endpoint = str(doc.get("authorization_endpoint") or "")
    if not endpoint:
        raise RuntimeError("IdP 未提供 authorization_endpoint")
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    ctx = {
        "state": secrets.token_urlsafe(24),
        "nonce": secrets.token_urlsafe(24),
        "verifier": verifier,
    }
    params = {
        "response_type": "code",
        "client_id": cfg["client_id"],
        "redirect_uri": oidc_redirect_uri(),
        "scope": cfg["scopes"],
        "state": ctx["state"],
        "nonce": ctx["nonce"],
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return endpoint + "?" + urlencode(params), ctx


def _normalize_claims(claims: Dict[str, Any]) -> Dict[str, Any]:
    """把各 IdP 五花八门的 claim 收敛成固定结构"""
    claims = claims or {}
    email = str(claims.get("email") or "").strip()
    preferred = (
        claims.get("preferred_username")
        or claims.get("nickname")
        or claims.get("name")
        or (email.split("@")[0] if email else "")
    )
    return {
        "subject": str(claims.get("sub") or ""),
        "username": str(preferred or "").strip(),
        "email": email,
        "name": str(claims.get("name") or preferred or "").strip(),
        "email_verified": claims.get("email_verified"),
    }


async def _verify_id_token(
    id_token: str, doc: Dict[str, Any], cfg: Dict[str, Any], nonce: Optional[str]
) -> Optional[Dict[str, Any]]:
    """按 IdP 的 JWKS 验签并校验 id_token 声明"""
    if not id_token:
        return None
    jwks_uri = str(doc.get("jwks_uri") or "")
    if not jwks_uri:
        raise RuntimeError("IdP 未提供 jwks_uri，无法校验 id_token")
    import jwt as pyjwt
    from jwt import PyJWKClient

    algorithms = doc.get("id_token_signing_alg_values_supported") or ["RS256", "ES256"]
    try:
        # PyJWKClient 取密钥是同步网络请求，丢到线程里别堵事件循环
        def _decode() -> Dict[str, Any]:
            key = PyJWKClient(jwks_uri).get_signing_key_from_jwt(id_token)
            return pyjwt.decode(
                id_token,
                key.key,
                algorithms=list(algorithms),
                audience=cfg["client_id"],
                options={"require": ["exp"]},
            )

        claims = await asyncio.to_thread(_decode)
    except Exception as e:
        raise RuntimeError(f"id_token 校验失败：{e}")
    # issuer 各家带不带结尾斜杠不统一，这里手动归一化比对
    expected = str(cfg["issuer"]).rstrip("/")
    got = str(claims.get("iss") or "").rstrip("/")
    if got and got != expected:
        raise RuntimeError(f"id_token 签发方不匹配（iss={got}）")
    if nonce and claims.get("nonce") and not secrets.compare_digest(str(claims["nonce"]), str(nonce)):
        raise RuntimeError("id_token 的 nonce 与本次请求不一致")
    return claims


async def _fetch_userinfo(
    doc: Dict[str, Any], access_token: Optional[str]
) -> Dict[str, Any]:
    """id_token 缺失时退到 userinfo 端点拿身份"""
    endpoint = str(doc.get("userinfo_endpoint") or "")
    if not endpoint or not access_token:
        raise RuntimeError("既没有 id_token，IdP 也没提供 userinfo 端点")
    import httpx

    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        resp = await client.get(
            endpoint, headers={"Authorization": f"Bearer {access_token}"}
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"userinfo 调用失败（HTTP {resp.status_code}）")
        return resp.json()


async def oidc_exchange(code: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """用授权码换 token，校验身份并归一化返回"""
    cfg = oidc_settings()
    doc = await oidc_discovery()
    endpoint = str(doc.get("token_endpoint") or "")
    if not endpoint:
        raise RuntimeError("IdP 未提供 token_endpoint")
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": oidc_redirect_uri(),
        "client_id": cfg["client_id"],
        "code_verifier": str(ctx.get("verifier") or ""),
    }
    if cfg["client_secret"]:
        data["client_secret"] = cfg["client_secret"]
    import httpx

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.post(
            endpoint, data=data, headers={"Accept": "application/json"}
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"换取令牌失败（HTTP {resp.status_code}）：{resp.text[:180]}")
        payload = resp.json()

    claims = await _verify_id_token(
        payload.get("id_token"), doc, cfg, ctx.get("nonce")
    )
    if claims is None:
        claims = await _fetch_userinfo(doc, payload.get("access_token"))
    info = _normalize_claims(claims)
    if not info["subject"]:
        raise RuntimeError("IdP 返回的身份缺少 sub")
    return info


def resolve_oidc_user(info: Dict[str, Any]) -> Dict[str, Any]:
    """把 OIDC 身份映射成面板账号：按 sub 复用，其次绑定同名账号，最后按需自动建号"""
    cfg = oidc_settings()
    subject = str(info.get("subject") or "")
    email = str(info.get("email") or "").lower()
    username = str(info.get("username") or "").strip() or (
        email.split("@")[0] if email else subject[:12]
    )

    # 允许名单：邮箱与域名都配时命中任一即可；都没配等于不限制
    emails = cfg["allowed_emails"]
    domains = cfg["allowed_domains"]
    if emails or domains:
        hit = bool(email and email in emails)
        if not hit and domains and "@" in email:
            hit = email.rsplit("@", 1)[1] in domains
        if not hit:
            raise RuntimeError(f"账号 {email or username} 不在 OIDC 允许名单内")
    if cfg["require_verified_email"] and info.get("email_verified") is False:
        raise RuntimeError("邮箱未经 IdP 校验，已拒绝登录")

    existing = store.get_user_by_subject("oidc", subject)
    if existing:
        if not existing.get("enabled"):
            raise RuntimeError("该账号已在面板中被停用")
        return existing

    local = store.get_user(username)
    if local:
        # 同名账号只有在「本来就是 OIDC 账号」或「没设本地口令」时才认作本人，
        # 否则 IdP 里任何人只要 preferred_username 撞上 admin 就能接管本地管理员，太危险
        bindable = local.get("provider") == "oidc" or not local.get("has_password")
        if not bindable:
            raise RuntimeError(
                f"面板里已有同名的本地账号 {username}，出于安全不自动绑定；"
                "请管理员改掉该账号的用户名，或在面板里直接给该账号绑定 OIDC"
            )
        if not local.get("enabled"):
            raise RuntimeError("该账号已在面板中被停用")
        store.update_user(username, subject=subject)
        return local

    if not cfg["auto_create"]:
        raise RuntimeError(f"面板里没有 {username} 这个账号，且已关闭 OIDC 自动建号")

    return store.create_user(
        username,
        None,
        role=cfg["default_role"],
        display_name=str(info.get("name") or username),
        provider="oidc",
        subject=subject,
    )


def oidc_fail(message: str) -> Any:
    """OIDC 出错时把人送回登录页，并带上原因给前端提示"""
    from fastapi.responses import RedirectResponse

    return RedirectResponse(
        panel_base_url() + "?auth_error=" + quote(message), status_code=302
    )


# ------------------------------------------------------------------ 路径守卫
def _resolve(rel: str) -> Path:
    """把面板传来的相对路径解析为项目内绝对路径，拒绝越权与敏感文件"""
    raw = (rel or "").strip().replace("\\", "/").lstrip("/")
    if not raw:
        raise ValueError("路径不能为空")
    target = (PROJECT_ROOT / raw).resolve()
    try:
        target.relative_to(PROJECT_ROOT)
    except ValueError:
        raise ValueError("越权路径")
    parts = set(target.relative_to(PROJECT_ROOT).parts)
    if parts & _DENY_PARTS:
        raise ValueError("该目录不允许在面板中操作")
    if target.suffix.lower() in _DENY_SUFFIX:
        raise ValueError("该类型文件不允许在面板中操作")
    return target


def _rel(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT).as_posix()


def _list_dir(rel: str) -> List[Dict[str, Any]]:
    target = _resolve(rel or ".")
    if not target.is_dir():
        raise ValueError("不是目录")
    items: List[Dict[str, Any]] = []
    for child in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if child.name in _DENY_PARTS or child.suffix.lower() in _DENY_SUFFIX:
            continue
        try:
            stat = child.stat()
        except OSError:
            continue
        items.append(
            {
                "name": child.name,
                "path": _rel(child),
                "is_dir": child.is_dir(),
                "size": 0 if child.is_dir() else stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "editable": child.is_file() and child.suffix.lower() in _TEXT_EXT,
            }
        )
    return items


def _read_file(rel: str) -> Dict[str, Any]:
    target = _resolve(rel)
    if not target.is_file():
        raise ValueError("文件不存在")
    if target.suffix.lower() not in _TEXT_EXT:
        raise ValueError("该类型文件不支持在面板中编辑")
    if target.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("文件过大（>2MB），请在服务器上直接编辑")
    return {
        "path": _rel(target),
        "content": target.read_text(encoding="utf-8", errors="replace"),
        "size": target.stat().st_size,
        "mtime": datetime.fromtimestamp(target.stat().st_mtime).isoformat(timespec="seconds"),
    }


def _write_file(rel: str, content: str) -> Dict[str, Any]:
    target = _resolve(rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_suffix(target.suffix + ".bak")
        try:
            backup.write_bytes(target.read_bytes())
        except OSError:
            pass
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)
    return {"path": _rel(target), "size": target.stat().st_size}


# ------------------------------------------------------------------ 指令清单
def _commands_json() -> Path:
    from util.command_sync import commands_json_path

    return commands_json_path()


def read_commands() -> Dict[str, Any]:
    path = _commands_json()
    if not path.exists():
        return {"commands": [], "summary": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError(f"commands.json 解析失败：{e}")


def write_commands(doc: Dict[str, Any]) -> Dict[str, Any]:
    from util.command_sync import save_commands_doc

    save_commands_doc(doc)
    return doc


# ------------------------------------------------------------------ 系统概览
def _overview() -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "uptime": int(time.time() - _STARTED_AT),
        "python": _py_version(),
        "platform": _platform(),
        "panel": {
            "host": env_host(),
            "port": env_port(),
            "url": panel_base_url(),
            "https": panel_uses_https(),
        },
    }
    try:
        from nonebot import get_loaded_plugins

        plugins = list(get_loaded_plugins())
        info["plugin_count"] = len(plugins)
        info["plugins"] = sorted(p.name for p in plugins)
    except Exception:
        info["plugin_count"] = 0
        info["plugins"] = []
    try:
        from util.config_manager import get_config_manager

        info["nickname"] = get_config_manager().get("bot.nickname", "cangyao")
        info["superusers"] = get_config_manager().get("bot.superusers", [])
        info["hot_reload"] = bool(get_config_manager().get("bot.hot_reload", False))
    except Exception:
        pass
    try:
        import psutil

        proc = psutil.Process(os.getpid())
        info["memory"] = round(proc.memory_info().rss / 1024 / 1024, 1)
        info["cpu"] = psutil.cpu_percent(interval=None)
    except Exception:
        pass
    return info


def _py_version() -> str:
    import sys

    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def _platform() -> str:
    import platform

    return f"{platform.system()} {platform.release()}"


def _tail_logs(lines: int = 200) -> List[str]:
    """优先返回日志文件尾部，其次返回启动缓冲区的日志"""
    try:
        from util.config_manager import get_config_manager

        raw = get_config_manager().get("logging.file_path", "logs/bot.log")
    except Exception:
        raw = "logs/bot.log"
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if path.exists():
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                buf: "deque[str]" = deque(f, maxlen=max(10, int(lines)))
            return list(buf)
        except OSError:
            pass
    return list(_log_buffer)


def buffer_log(line: str) -> None:
    """供插件把日志同时塞进面板缓冲区"""
    _log_buffer.append(line)


# ------------------------------------------------------------------ 入参规整
def _digits(value: Any) -> str:
    """把输入规整成纯数字字符串（QQ 号 / 群号），非纯数字返回空串"""
    text = str(value or "").strip()
    return text if text.isdigit() else ""


def parse_join_requests(group_id: Any, data: Any) -> List[Dict[str, Any]]:
    """把两端结构不同的入群申请统一成 ``{flag, group_id, user_id, comment, sub_type}``。

    - goqbot：``data.list[]`` / ``data.requests[]``，凭证字段是 ``flag`` 或 ``join_request_id``
    - 标准 go-cqhttp：``data.join_requests[]``（主动加群）/ ``data.invite_requests[]``（被邀请）
    """
    if not isinstance(data, dict):
        return []
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    def _push(flag: Any, uid: Any, comment: Any, sub_type: str) -> None:
        key = str(flag or "")
        if not key or key in seen:
            return
        seen.add(key)
        out.append(
            {
                "flag": key,
                "group_id": str(group_id),
                "user_id": str(uid or ""),
                "comment": str(comment or ""),
                "sub_type": sub_type or "add",
            }
        )

    for item in data.get("list") or data.get("requests") or []:
        if isinstance(item, dict):
            _push(
                item.get("flag") or item.get("join_request_id") or item.get("request_id"),
                item.get("user_id") or item.get("uid"),
                item.get("comment") or item.get("message"),
                str(item.get("sub_type") or item.get("type") or "add"),
            )
    for key, sub in (("join_requests", "add"), ("invite_requests", "invite")):
        for item in data.get(key) or []:
            if isinstance(item, dict):
                _push(
                    item.get("flag") or item.get("request_id"),
                    item.get("user_id"),
                    item.get("comment"),
                    sub,
                )
    return out


# ------------------------------------------------------------------ FastAPI 应用
def build_app():
    from fastapi import Body, FastAPI, Request
    from fastapi.responses import (
        FileResponse,
        HTMLResponse,
        JSONResponse,
        RedirectResponse,
        Response,
    )

    app = FastAPI(title="cangyao 管理面板", docs_url=None, redoc_url=None, openapi_url=None)

    # 账号表为空时兜底建一个管理员；OIDC 没配允许名单时给出告警
    try:
        bootstrap_admin()
        warn_oidc_open()
    except Exception as e:
        logger.warning(f"guanli 面板登录初始化异常：{e!r}")

    def _guard(request: Request, *, write: bool = False) -> Optional[JSONResponse]:
        """读接口只要求登录；写接口要求 admin 角色"""
        user = current_user(request)
        if user is None:
            return JSONResponse({"ok": False, "error": "未授权，请先登录"}, status_code=401)
        if write and user.get("role") != "admin":
            return JSONResponse(
                {"ok": False, "error": "当前账号是只读权限，无法执行该操作"}, status_code=403
            )
        return None

    def _admin_only(request: Request) -> Optional[JSONResponse]:
        """账号 / 会话管理这类敏感读接口也要求 admin"""
        user = current_user(request)
        if user is None:
            return JSONResponse({"ok": False, "error": "未授权，请先登录"}, status_code=401)
        if user.get("role") != "admin":
            return JSONResponse(
                {"ok": False, "error": "只有管理员可以查看和修改面板账号"}, status_code=403
            )
        return None

    # 写权限集中在一处判断，省得四十多个接口各写一遍（登录相关接口除外）
    _OPEN_WRITE = {"/api/login", "/api/logout", "/api/oidc/start", "/api/oidc/callback"}

    @app.middleware("http")
    async def _write_role_guard(request: Request, call_next: Any) -> Any:
        path = request.url.path
        probe = path[path.find("/api/") :] if "/api/" in path else path
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and probe not in _OPEN_WRITE:
            if probe.startswith("/api/"):
                denied = _guard(request, write=True)
                if denied is not None:
                    return denied
        return await call_next(request)

    # ---------------- 页面
    @app.get("/")
    async def index() -> Any:
        page = STATIC_DIR / "index.html"
        if not page.exists():
            return HTMLResponse("<h1>管理面板前端缺失</h1><p>未找到 static/index.html</p>", status_code=500)
        return FileResponse(
            page,
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    @app.get("/favicon.ico")
    async def favicon() -> Any:
        icon = STATIC_DIR / "favicon.ico"
        if icon.exists():
            return FileResponse(icon)
        return Response(status_code=204)

    @app.get("/static/{path:path}")
    async def static_file(path: str) -> Any:
        target = (STATIC_DIR / path).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            return JSONResponse({"ok": False, "error": "非法路径"}, status_code=400)
        if not target.is_file():
            return JSONResponse({"ok": False, "error": "文件不存在"}, status_code=404)
        return FileResponse(target, headers={"Cache-Control": "no-store"})

    # ---------------- 登录
    def _public_user(user: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """给前端的身份信息，去掉 sid 等内部字段"""
        if not user:
            return None
        return {
            "username": user.get("username"),
            "role": user.get("role"),
            "role_desc": store.role_desc(user.get("role")),
            "display_name": user.get("display_name") or user.get("username"),
            "provider": user.get("provider"),
            "via": user.get("via"),
        }

    def _set_session_cookie(resp: Any, user: Dict[str, Any], request: Request) -> Any:
        sid = issue_session(
            user.get("username") or "",
            str(user.get("role") or "viewer"),
            provider=str(user.get("provider") or "local"),
            request=request,
        )
        try:
            store.touch_user_login(user.get("username"))
        except Exception:
            pass
        resp.set_cookie(
            _SID_COOKIE,
            sid,
            httponly=True,
            samesite="lax",
            secure=panel_uses_https(),
            path=cookie_path(),
            max_age=session_ttl(),
        )
        return resp

    @app.get("/api/auth/config")
    async def auth_config_api() -> Any:
        """登录页得先知道有哪些渠道可用，所以本接口不要求登录"""
        return auth_config()

    @app.post("/api/login")
    async def login(request: Request, payload: Dict[str, Any] = Body(default={})) -> Any:
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        supplied = str(payload.get("token") or "").strip()
        # 限速用「来源 IP + 账号」做键，挡住针对同一账号的暴力撞库
        throttle_key = username or ("token" if supplied else "")

        wait = login_block_seconds(request, throttle_key)
        if wait:
            return JSONResponse(
                {"ok": False, "error": f"登录失败次数过多，请 {max(1, wait // 60)} 分钟后再试"},
                status_code=429,
            )

        # 渠道三：只给了访问口令（应急入口）
        if not username and supplied:
            if not secrets.compare_digest(supplied, panel_token()):
                login_record_fail(request, throttle_key)
                return JSONResponse({"ok": False, "error": "访问口令不正确"}, status_code=401)
            login_clear_fails(request, throttle_key)
            resp = JSONResponse(
                {
                    "ok": True,
                    "user": {
                        "username": "token",
                        "role": "admin",
                        "role_desc": store.role_desc("admin"),
                        "display_name": "访问口令",
                        "provider": "token",
                        "via": "token",
                    },
                }
            )
            resp.set_cookie(
                _COOKIE_NAME,
                panel_token(),
                httponly=True,
                samesite="lax",
                path=cookie_path(),
                max_age=30 * 24 * 3600,
            )
            return resp

        if not username:
            return JSONResponse({"ok": False, "error": "请填写用户名与密码"}, status_code=400)

        # 渠道一：账号密码
        status, user = store.authenticate(username, password)
        if status == "ok" and user:
            resp = JSONResponse(
                {"ok": True, "user": _public_user({**user, "provider": "local", "via": "session"})}
            )
            _set_session_cookie(resp, {**user, "provider": "local"}, request)
            login_clear_fails(request, throttle_key)
            try:
                store.add_audit("面板登录", actor=username, detail="账号密码", source="panel")
            except Exception:
                pass
            return resp
        message = {
            "missing": "用户名或密码不正确",
            "bad": "用户名或密码不正确",
            "disabled": "该账号已被停用，请联系管理员",
            "nopassword": "该账号未设置密码，请改用 OIDC 登录",
        }.get(status, "登录失败")
        # 只有「口令不对 / 账号不存在」才计入撞库次数；停用、未设口令属于账号状态问题
        if status in ("missing", "bad"):
            login_record_fail(request, throttle_key)
        return JSONResponse({"ok": False, "error": message}, status_code=401)

    @app.post("/api/logout")
    async def logout(request: Request) -> Any:
        sid = request.cookies.get(_SID_COOKIE)
        if sid:
            try:
                store.delete_session(sid)
            except Exception:
                pass
        resp = JSONResponse({"ok": True})
        # 老的部署把口令写在跟路径上，两个作用域都清一遍免得残留
        for scope in {"/", cookie_path()}:
            resp.delete_cookie(_SID_COOKIE, path=scope)
            resp.delete_cookie(_COOKIE_NAME, path=scope)
        return resp

    @app.get("/api/session")
    async def session(request: Request) -> Any:
        user = current_user(request)
        if user and user.get("via") == "session" and user.get("sid"):
            try:
                store.touch_session(user["sid"])
            except Exception:
                pass
        return {"ok": True, "authorized": user is not None, "user": _public_user(user)}

    # ---------------- OIDC 登录
    @app.get("/api/oidc/start")
    async def oidc_start() -> Any:
        if not oidc_settings()["enabled"]:
            return oidc_fail("面板未启用 OIDC 登录")
        try:
            url, ctx = await oidc_authorize_url()
        except Exception as e:
            logger.warning(f"guanli 面板发起 OIDC 授权失败：{e!r}")
            return oidc_fail(f"OIDC 初始化失败：{e}")
        resp = RedirectResponse(url, status_code=302)
        resp.set_cookie(
            _OIDC_COOKIE,
            _b64url(json.dumps(ctx, ensure_ascii=False).encode("utf-8")),
            httponly=True,
            samesite="lax",
            secure=panel_uses_https(),
            path=cookie_path(),
            max_age=_OIDC_TTL,
        )
        return resp

    @app.get("/api/oidc/callback")
    async def oidc_callback(request: Request) -> Any:
        if not oidc_settings()["enabled"]:
            return oidc_fail("面板未启用 OIDC 登录")
        error = request.query_params.get("error")
        if error:
            return oidc_fail(
                "IdP 拒绝了本次登录：" + str(request.query_params.get("error_description") or error)
            )
        code = str(request.query_params.get("code") or "")
        state = str(request.query_params.get("state") or "")
        if not code:
            return oidc_fail("回调缺少授权码")
        ctx: Dict[str, Any] = {}
        raw = request.cookies.get(_OIDC_COOKIE)
        if raw:
            try:
                ctx = json.loads(_b64url_decode(raw).decode("utf-8"))
            except Exception:
                ctx = {}
        if not ctx.get("state") or not secrets.compare_digest(str(ctx["state"]), state):
            return oidc_fail("OIDC 状态校验失败，请重新发起登录")
        try:
            info = await oidc_exchange(code, ctx)
            user = resolve_oidc_user(info)
        except Exception as e:
            logger.warning(f"guanli 面板 OIDC 登录失败：{e!r}")
            return oidc_fail(str(e))
        resp = RedirectResponse(panel_base_url(), status_code=302)
        _set_session_cookie(resp, {**user, "provider": "oidc"}, request)
        resp.delete_cookie(_OIDC_COOKIE, path=cookie_path())
        try:
            store.add_audit(
                "面板登录", actor=str(user.get("username")), detail="OIDC", source="panel"
            )
        except Exception:
            pass
        return resp

    # ---------------- 面板账号 / 会话
    @app.get("/api/users")
    async def list_users_api(request: Request) -> Any:
        denied = _admin_only(request)
        if denied:
            return denied
        me = current_user(request) or {}
        users = store.list_users()
        for item in users:
            item["is_self"] = item.get("username") == me.get("username")
            item["role_desc"] = store.role_desc(item.get("role"))
        return {
            "ok": True,
            "users": users,
            "roles": [
                {"value": "admin", "label": "管理员 · 可读写"},
                {"value": "viewer", "label": "只读 · 仅查看"},
            ],
            "current": me.get("username"),
        }

    @app.post("/api/users")
    async def create_user_api(request: Request, payload: Dict[str, Any] = Body(default={})) -> Any:
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        role = str(payload.get("role") or "admin")
        if not username:
            return JSONResponse({"ok": False, "error": "用户名不能为空"}, status_code=400)
        if not password:
            return JSONResponse({"ok": False, "error": "请为新账号设置密码"}, status_code=400)
        try:
            user = store.create_user(
                username,
                password,
                role=role,
                display_name=str(payload.get("display_name") or "").strip(),
            )
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        me = current_user(request) or {}
        store.add_audit(
            "新建面板账号",
            actor=me.get("username", ""),
            target=username,
            detail=f"角色={store.role_desc(role)}",
            source="panel",
        )
        return {"ok": True, "user": user}

    @app.put("/api/users/{username}")
    async def update_user_api(
        username: str, request: Request, payload: Dict[str, Any] = Body(default={})
    ) -> Any:
        me = current_user(request) or {}
        target = store.get_user(username)
        if not target:
            return JSONResponse({"ok": False, "error": "账号不存在"}, status_code=404)
        role = str(payload.get("role") or target.get("role") or "admin")
        enabled = payload.get("enabled")
        enabled = (int(target.get("enabled", 1)) == 1) if enabled is None else bool(enabled)
        is_self = target.get("username") == me.get("username")
        # 别把后台锁死：不能自降权限，也不能把最后一个启用管理员改掉
        if is_self and (role != "admin" or not enabled):
            return JSONResponse({"ok": False, "error": "不能把自己降级或停用"}, status_code=400)
        if target.get("role") == "admin" and (role != "admin" or not enabled):
            if store.count_admins(exclude=str(target.get("username"))) == 0:
                return JSONResponse(
                    {"ok": False, "error": "至少要保留一个启用状态的管理员"}, status_code=400
                )
        # password 字段「存在」才动口令：空串 = 清除口令（改成只能走 OIDC）
        # 先校验强度再落库，避免「角色已改、口令被拒」的半截更新
        changed_password = "password" in payload
        new_password = str(payload.get("password") or "")
        if changed_password and new_password:
            problem = store.password_policy_error(new_password, str(target.get("username")))
            if problem:
                return JSONResponse({"ok": False, "error": problem}, status_code=400)

        fields: Dict[str, Any] = {"role": role, "enabled": enabled}
        if payload.get("display_name") is not None:
            fields["display_name"] = str(payload.get("display_name"))
        store.update_user(str(target.get("username")), **fields)
        if changed_password:
            store.set_user_password(str(target.get("username")), new_password)
            store.delete_user_sessions(str(target.get("username")))
        if not enabled:
            store.delete_user_sessions(str(target.get("username")))
        store.add_audit(
            "修改面板账号",
            actor=me.get("username", ""),
            target=str(target.get("username")),
            detail=f"角色={store.role_desc(role)} · 启用={enabled}"
            + (" · 已清除口令（仅 OIDC）" if changed_password and not str(payload.get("password") or "")
               else " · 已重置密码" if changed_password else ""),
            source="panel",
        )
        return {"ok": True, "user": store.get_user(str(target.get("username")))}

    @app.delete("/api/users/{username}")
    async def delete_user_api(username: str, request: Request) -> Any:
        me = current_user(request) or {}
        target = store.get_user(username)
        if not target:
            return JSONResponse({"ok": False, "error": "账号不存在"}, status_code=404)
        name = str(target.get("username"))
        if name == me.get("username"):
            return JSONResponse({"ok": False, "error": "不能删除当前登录的账号"}, status_code=400)
        if target.get("role") == "admin" and store.count_admins(exclude=name) == 0:
            return JSONResponse(
                {"ok": False, "error": "至少要保留一个启用状态的管理员"}, status_code=400
            )
        store.delete_user(name)
        store.add_audit("删除面板账号", actor=me.get("username", ""), target=name, source="panel")
        return {"ok": True, "deleted": name}

    @app.get("/api/sessions")
    async def list_sessions_api(request: Request) -> Any:
        denied = _admin_only(request)
        if denied:
            return denied
        me = current_user(request) or {}
        try:
            store.prune_sessions()
        except Exception:
            pass
        items = []
        for row in store.list_sessions():
            items.append(
                {
                    "username": row.get("username"),
                    "role": row.get("role"),
                    "provider": row.get("provider"),
                    "ip": row.get("ip"),
                    "ua": str(row.get("ua") or "")[:70],
                    "created_at": row.get("created_at"),
                    "seen_at": row.get("seen_at"),
                    "is_self": bool(me.get("sid")) and row.get("sid") == me.get("sid"),
                }
            )
        return {"ok": True, "sessions": items}

    @app.delete("/api/sessions")
    async def revoke_sessions_api(
        request: Request, payload: Dict[str, Any] = Body(default={})
    ) -> Any:
        me = current_user(request) or {}
        username = str(payload.get("username") or "").strip()
        if not username:
            return JSONResponse({"ok": False, "error": "缺少 username"}, status_code=400)
        count = store.delete_user_sessions(username)
        store.add_audit(
            "强制下线",
            actor=me.get("username", ""),
            target=username,
            detail=f"清除 {count} 个会话",
            source="panel",
        )
        return {"ok": True, "revoked": count}

    # ---------------- 概览
    @app.get("/api/overview")
    async def overview(request: Request) -> Any:
        denied = _guard(request)
        if denied:
            return denied
        info = _overview()
        try:
            info["store"] = store.summary()
        except Exception as e:  # 统计失败不影响概览其它字段
            info["store"] = {"error": repr(e)}
        return info

    # ---------------- 配置
    @app.get("/api/config")
    async def get_config_api(request: Request) -> Any:
        denied = _guard(request)
        if denied:
            return denied
        return get_config().model_dump()

    @app.put("/api/config")
    async def put_config_api(request: Request, payload: Dict[str, Any] = Body(default={})) -> Any:
        denied = _guard(request)
        if denied:
            return denied
        try:
            cfg = GuanliConfig(**{**get_config().model_dump(), **payload})
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"配置无效：{e}"}, status_code=400)
        save_config(cfg)
        return {"ok": True, "config": cfg.model_dump()}

    # ---------------- 违禁词
    @app.get("/api/anti-keywords")
    async def get_anti(request: Request) -> Any:
        denied = _guard(request)
        return denied or {"keywords": get_config().anti_keywords}

    @app.put("/api/anti-keywords")
    async def put_anti(request: Request, payload: Dict[str, Any] = Body(default={})) -> Any:
        denied = _guard(request)
        if denied:
            return denied
        raw = payload.get("keywords") or []
        if isinstance(raw, str):
            raw = [w.strip() for w in raw.replace("，", ",").replace("\n", ",").split(",")]
        keywords = [str(w).strip() for w in raw if str(w).strip()]
        cfg = get_config()
        cfg.anti_keywords = sorted(set(keywords))
        save_config(cfg)
        return {"ok": True, "keywords": cfg.anti_keywords}

    # ---------------- 指令
    @app.get("/api/commands")
    async def get_commands(request: Request) -> Any:
        denied = _guard(request)
        if denied:
            return denied
        try:
            return read_commands()
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    @app.put("/api/commands")
    async def put_commands(request: Request, payload: Dict[str, Any] = Body(default={})) -> Any:
        denied = _guard(request)
        if denied:
            return denied
        try:
            doc = payload.get("doc") or payload
            if not isinstance(doc, dict) or not isinstance(doc.get("commands"), list):
                raise ValueError("缺少 commands 数组")
            return {"ok": True, "doc": write_commands(doc)}
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    @app.post("/api/commands/rescan")
    async def rescan_commands(request: Request) -> Any:
        """重新扫描插件 on_command 并回写 commands.json"""
        denied = _guard(request)
        if denied:
            return denied
        try:
            from util.command_sync import load_merge_commands

            commands = load_merge_commands()
            return {"ok": True, "count": len(commands), "doc": read_commands()}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    # ---------------- 群配置
    @app.get("/api/groups")
    async def get_groups(request: Request) -> Any:
        denied = _guard(request)
        return denied or {"groups": store.list_groups()}

    @app.get("/api/groups/{group_id}")
    async def get_group_api(request: Request, group_id: str) -> Any:
        denied = _guard(request)
        return denied or store.get_group(group_id)

    @app.put("/api/groups/{group_id}")
    async def put_group_api(
        request: Request, group_id: str, payload: Dict[str, Any] = Body(default={})
    ) -> Any:
        denied = _guard(request)
        if denied:
            return denied
        data = store.save_group(
            group_id,
            watch_image=payload.get("watch_image"),
            keywords=payload.get("keywords"),
            plugins=payload.get("plugins"),
            welcome=payload.get("welcome"),
            leave_notice=payload.get("leave_notice"),
            extra=payload.get("extra"),
        )
        return {"ok": True, "group": data}

    @app.get("/api/warns")
    async def get_warns(request: Request, group_id: Optional[str] = None) -> Any:
        denied = _guard(request)
        return denied or {"warns": store.list_warns(group_id)}

    @app.delete("/api/warns")
    async def del_warns(request: Request, group_id: str, user_id: str) -> Any:
        denied = _guard(request)
        if denied:
            return denied
        store.clear_warn(group_id, user_id)
        return {"ok": True}

    # ---------------- 发言统计
    @app.get("/api/stats")
    async def get_stats(
        request: Request, group_id: Optional[str] = None, limit: int = 100
    ) -> Any:
        denied = _guard(request)
        return denied or {"stats": store.list_stats(group_id, limit)}

    @app.delete("/api/stats")
    async def clear_stats_api(request: Request, group_id: Optional[str] = None) -> Any:
        denied = _guard(request)
        if denied:
            return denied
        removed = store.clear_stats(group_id)
        store.add_audit(
            "清空发言统计",
            actor="面板",
            group_id=group_id or "",
            detail=f"删除 {removed} 条",
            source="panel",
        )
        return {"ok": True, "removed": removed}

    # ---------------- 黑白名单
    @app.get("/api/lists")
    async def get_lists(
        request: Request, group_id: Optional[str] = None, kind: Optional[str] = None
    ) -> Any:
        denied = _guard(request)
        return denied or {"entries": store.list_entries(group_id, kind)}

    @app.post("/api/lists")
    async def add_list(request: Request, payload: Dict[str, Any] = Body(default={})) -> Any:
        denied = _guard(request)
        if denied:
            return denied
        group_id = _digits(payload.get("group_id"))
        user_id = _digits(payload.get("user_id"))
        if not group_id or not user_id:
            return JSONResponse({"ok": False, "error": "群号与 QQ 号都必须填写"}, status_code=400)
        kind = "white" if str(payload.get("kind")) == "white" else "black"
        entry = store.add_to_list(group_id, user_id, kind, str(payload.get("note") or ""))
        store.add_audit(
            "加入名单",
            actor="面板",
            group_id=group_id,
            target=user_id,
            detail="白名单（豁免）" if kind == "white" else "黑名单（拦截）",
            source="panel",
        )
        return {"ok": True, "entry": entry}

    @app.delete("/api/lists")
    async def del_list(
        request: Request, group_id: str, user_id: str, kind: Optional[str] = None
    ) -> Any:
        denied = _guard(request)
        if denied:
            return denied
        removed = store.remove_from_list(group_id, user_id, kind)
        store.add_audit(
            "移出名单", actor="面板", group_id=group_id, target=user_id, detail=kind or "全部",
            source="panel",
        )
        return {"ok": True, "removed": removed}

    # ---------------- 关键词自动回复
    @app.get("/api/autoreplies")
    async def get_autoreplies(request: Request, group_id: Optional[str] = None) -> Any:
        denied = _guard(request)
        return denied or {"items": store.list_autoreplies(group_id)}

    @app.post("/api/autoreplies")
    async def add_autoreply(request: Request, payload: Dict[str, Any] = Body(default={})) -> Any:
        denied = _guard(request)
        if denied:
            return denied
        try:
            item = store.add_autoreply(
                payload.get("group_id"),
                str(payload.get("pattern") or ""),
                str(payload.get("reply") or ""),
                str(payload.get("mode") or "contains"),
            )
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        store.add_audit(
            "设置自动回复",
            actor="面板",
            group_id=str(payload.get("group_id") or "全局"),
            detail=f"{item.get('pattern')} → {item.get('reply')}",
            source="panel",
        )
        return {"ok": True, "item": item}

    @app.post("/api/autoreplies/toggle")
    async def toggle_autoreply(
        request: Request, payload: Dict[str, Any] = Body(default={})
    ) -> Any:
        denied = _guard(request)
        if denied:
            return denied
        try:
            enabled = store.toggle_autoreply(payload.get("id"), bool(payload.get("enabled")))
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "缺少合法的 id"}, status_code=400)
        return {"ok": True, "enabled": enabled}

    @app.delete("/api/autoreplies/{reply_id}")
    async def del_autoreply(request: Request, reply_id: int) -> Any:
        denied = _guard(request)
        if denied:
            return denied
        removed = store.remove_autoreply(reply_id)
        store.add_audit("删除自动回复", actor="面板", detail=f"id={reply_id}", source="panel")
        return {"ok": True, "removed": removed}

    # ---------------- 入群申请
    @app.get("/api/approvals")
    async def get_approvals(
        request: Request, group_id: Optional[str] = None, status: Optional[str] = None
    ) -> Any:
        denied = _guard(request)
        return denied or {"items": store.list_approvals(group_id, status)}

    @app.post("/api/approvals/refresh")
    async def refresh_approvals(
        request: Request, payload: Dict[str, Any] = Body(default={})
    ) -> Any:
        """向协议端拉一次入群申请列表并入库（goqbot 与标准端结构不同，这里统一解析）"""
        denied = _guard(request)
        if denied:
            return denied
        group_id = _digits(payload.get("group_id"))
        if not group_id:
            return JSONResponse({"ok": False, "error": "需要指定群号"}, status_code=400)

        def _pull(bot: Any) -> Any:
            from util import gobot_api

            return gobot_api.get_group_system_msg(bot, int(group_id))

        try:
            data = await call_bot(payload.get("self_id"), _pull)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"拉取失败：{e}"}, status_code=502)

        items = parse_join_requests(group_id, data)
        for item in items:
            store.save_approval(
                item["flag"], item["group_id"], item["user_id"], item["comment"], item["sub_type"]
            )
        return {"ok": True, "count": len(items), "items": store.list_approvals(group_id)}

    @app.post("/api/approvals/handle")
    async def handle_approval(
        request: Request, payload: Dict[str, Any] = Body(default={})
    ) -> Any:
        """审批入群申请：两端都走 set_group_add_request"""
        denied = _guard(request)
        if denied:
            return denied
        flag = str(payload.get("flag") or "").strip()
        if not flag:
            return JSONResponse({"ok": False, "error": "缺少申请凭证 flag"}, status_code=400)
        approve = bool(payload.get("approve", True))
        reason = str(payload.get("reason") or "")
        record = store.get_approval(flag) or {}

        def _do(bot: Any) -> Any:
            from util import gobot_api

            group_id = record.get("group_id")
            user_id = record.get("user_id")
            return gobot_api.set_group_add_request(
                bot,
                flag,
                approve,
                reason or None,
                group_id=int(group_id) if _digits(group_id) else None,
                user_id=int(user_id) if _digits(user_id) else None,
                sub_type=str(record.get("sub_type") or "add"),
            )

        try:
            await call_bot(payload.get("self_id"), _do)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"审批失败：{e}"}, status_code=502)

        store.set_approval_status(flag, "approved" if approve else "rejected")
        store.add_audit(
            "入群审批",
            actor="面板",
            group_id=record.get("group_id") or "",
            target=record.get("user_id") or "",
            detail=("同意" if approve else "拒绝") + (f"（{reason}）" if reason else ""),
            source="panel",
        )
        return {"ok": True, "status": "approved" if approve else "rejected"}

    # ---------------- 审计日志
    @app.get("/api/audit")
    async def get_audit(
        request: Request, limit: int = 200, group_id: Optional[str] = None
    ) -> Any:
        denied = _guard(request)
        return denied or {"items": store.list_audit(limit, group_id)}

    @app.delete("/api/audit")
    async def clear_audit_api(request: Request) -> Any:
        denied = _guard(request)
        if denied:
            return denied
        removed = store.clear_audit()
        return {"ok": True, "removed": removed}

    # ---------------- 在线机器人实例
    @app.get("/api/bots")
    async def list_bots(request: Request) -> Any:
        denied = _guard(request)
        if denied:
            return denied
        try:
            from nonebot import get_bots

            bots = get_bots()
        except Exception as e:
            return {"ok": True, "bots": [], "loop_ready": False, "error": repr(e)}
        items: List[Dict[str, Any]] = []
        for self_id, bot in bots.items():
            item: Dict[str, Any] = {"self_id": str(self_id), "type": getattr(bot, "type", "")}
            try:
                from util import gobot_api

                item["platform"] = "goqbot" if await run_on_bot(gobot_api.is_goqbot(bot)) else "onebot"
            except Exception:
                item["platform"] = "unknown"
            items.append(item)
        return {"ok": True, "bots": items, "loop_ready": bot_loop() is not None}

    # ---------------- 实时动作（禁言 / 全员禁言 / 撤回 / 通知）
    @app.post("/api/actions/ban")
    async def action_ban(request: Request, payload: Dict[str, Any] = Body(default={})) -> Any:
        """禁言 / 解禁单成员：两端 action 与参数一致"""
        denied = _guard(request)
        if denied:
            return denied
        group_id = _digits(payload.get("group_id"))
        user_id = _digits(payload.get("user_id"))
        if not group_id or not user_id:
            return JSONResponse({"ok": False, "error": "群号与 QQ 号都必须填写"}, status_code=400)
        try:
            duration = max(0, int(payload.get("duration") or 0))
        except (TypeError, ValueError):
            duration = 0

        def _do(bot: Any) -> Any:
            from util import gobot_api

            return gobot_api.set_group_ban(bot, int(group_id), int(user_id), duration)

        try:
            await call_bot(payload.get("self_id"), _do)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"操作失败：{e}"}, status_code=502)

        if duration > 0:
            warns = store.add_warn(group_id, user_id)
        else:
            store.clear_warn(group_id, user_id)
            warns = 0
        store.add_audit(
            "禁言" if duration > 0 else "解除禁言",
            actor="面板",
            group_id=group_id,
            target=user_id,
            detail=f"{duration} 秒" if duration > 0 else "解除",
            source="panel",
        )
        return {"ok": True, "warns": warns}

    @app.post("/api/actions/whole-ban")
    async def action_whole_ban(
        request: Request, payload: Dict[str, Any] = Body(default={})
    ) -> Any:
        """全员禁言：标准 OneBot 支持；goqbot（官方群 v2）无此能力，会明确报错"""
        denied = _guard(request)
        if denied:
            return denied
        group_id = _digits(payload.get("group_id"))
        if not group_id:
            return JSONResponse({"ok": False, "error": "需要指定群号"}, status_code=400)
        enable = bool(payload.get("enable", True))

        def _do(bot: Any) -> Any:
            from util import gobot_api

            return gobot_api.set_group_whole_ban(bot, int(group_id), enable)

        try:
            await call_bot(payload.get("self_id"), _do)
        except Exception as e:
            return JSONResponse(
                {"ok": False, "error": f"当前平台不支持全员禁言或调用失败：{e}"}, status_code=502
            )
        store.add_audit(
            "全员禁言" if enable else "解除全员禁言",
            actor="面板",
            group_id=group_id,
            source="panel",
        )
        return {"ok": True}

    @app.post("/api/actions/recall")
    async def action_recall(request: Request, payload: Dict[str, Any] = Body(default={})) -> Any:
        """撤回消息：两端都支持 delete_msg（goqbot 只能撤回它自己发出的消息）"""
        denied = _guard(request)
        if denied:
            return denied
        raw = str(payload.get("message_id") or "").strip()
        if not raw.isdigit():
            return JSONResponse({"ok": False, "error": "message_id 必须是数字"}, status_code=400)

        def _do(bot: Any) -> Any:
            from util import gobot_api

            return gobot_api.delete_msg(bot, int(raw))

        try:
            await call_bot(payload.get("self_id"), _do)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"撤回失败：{e}"}, status_code=502)
        store.add_audit(
            "撤回消息",
            actor="面板",
            group_id=str(payload.get("group_id") or ""),
            detail=f"message_id={raw}",
            source="panel",
        )
        return {"ok": True}

    @app.post("/api/actions/notify")
    async def action_notify(request: Request, payload: Dict[str, Any] = Body(default={})) -> Any:
        """往指定群发一条通知（两端都支持发群消息）"""
        denied = _guard(request)
        if denied:
            return denied
        group_id = _digits(payload.get("group_id"))
        message = str(payload.get("message") or "").strip()
        if not group_id or not message:
            return JSONResponse({"ok": False, "error": "群号与内容都不能为空"}, status_code=400)

        def _do(bot: Any) -> Any:
            from util import gobot_api

            return gobot_api.send_group_msg(bot, int(group_id), message)

        try:
            await call_bot(payload.get("self_id"), _do)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"发送失败：{e}"}, status_code=502)
        store.add_audit(
            "发送通知", actor="面板", group_id=group_id, detail=message[:100], source="panel"
        )
        return {"ok": True}

    # ---------------- 代码文件（替换原文件 + 热重载）
    @app.get("/api/files")
    async def list_files(request: Request, path: str = ".") -> Any:
        denied = _guard(request)
        if denied:
            return denied
        try:
            return {
                "ok": True,
                "path": path,
                "parent": str(Path(path).parent.as_posix()) if path not in (".", "", "/") else "",
                "items": _list_dir(path),
            }
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    @app.get("/api/file")
    async def get_file(request: Request, path: str) -> Any:
        denied = _guard(request)
        if denied:
            return denied
        try:
            return {"ok": True, **_read_file(path)}
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    @app.put("/api/file")
    async def put_file(request: Request, payload: Dict[str, Any] = Body(default={})) -> Any:
        denied = _guard(request)
        if denied:
            return denied
        try:
            result = _write_file(str(payload.get("path") or ""), str(payload.get("content") or ""))
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except OSError as e:
            return JSONResponse({"ok": False, "error": f"写入失败：{e}"}, status_code=500)
        logger.info(f"guanli 面板写入代码文件：{result['path']}")
        return {"ok": True, **result, "hot_reload": _hot_reload_hint(result["path"])}

    def _hot_reload_hint(rel_path: str) -> str:
        if rel_path.startswith(f"{_PKG}/"):
            return "面板代码已更新，点「重载」会丢掉旧模块并用新代码重启面板后端"
        if rel_path.startswith("src/plugins/") and rel_path.endswith(".py"):
            return "NoneBot hot_reload 已开启时会自动重载该插件；未生效可用「重载」按钮手动触发"
        return "已写入磁盘；如为插件代码建议点击「重载」"

    # ---------------- 热重载
    @app.post("/api/reload")
    async def reload_api(request: Request, payload: Dict[str, Any] = Body(default={})) -> Any:
        denied = _guard(request)
        if denied:
            return denied
        plugins = [str(p) for p in (payload.get("plugins") or [])]
        panel = bool(payload.get("panel"))
        if panel:
            # 先丢掉旧模块，再让插件重载流程用新代码重新导入 util.guanli_panel.panel，
            # 否则 reload_plugin 会卸载旧指令却读到缓存的旧模块，导致指令消失
            purge_modules()
            if "guanli" not in plugins:
                plugins.append("guanli")
        result = do_reload(plugins)
        if panel:
            result["panel"] = restart_with_fresh_code()
        return result

    # ---------------- 日志
    @app.get("/api/logs")
    async def get_logs(request: Request, lines: int = 200) -> Any:
        denied = _guard(request)
        if denied:
            return denied
        return {"ok": True, "lines": _tail_logs(lines)}

    # ---------------- 面板自身
    @app.post("/api/panel/host")
    async def set_host(request: Request, payload: Dict[str, Any] = Body(default={})) -> Any:
        denied = _guard(request)
        if denied:
            return denied
        return {"ok": True, "url": set_public_host(str(payload.get("host") or ""))}

    @app.post("/api/panel/reset-token")
    async def reset_token_api(request: Request) -> Any:
        denied = _guard(request)
        if denied:
            return denied
        token = reset_token()
        resp = JSONResponse({"ok": True, "token": token})
        resp.set_cookie(
            _COOKIE_NAME,
            token,
            httponly=True,
            samesite="lax",
            secure=panel_uses_https(),
            path="/",
            max_age=30 * 24 * 3600,
        )
        return resp

    return app


def do_reload(plugins: Optional[List[str]] = None) -> Dict[str, Any]:
    """热重载：刷新配置/权限/上下文，并可按需重载指定插件模块"""
    done: List[str] = []
    failed: Dict[str, str] = {}

    for label, fn in (
        ("config", _reload_config),
        ("permission", _reload_permission),
        ("context", _reload_context),
        ("plugin_config", _reload_all_configs),
    ):
        try:
            fn()
            done.append(label)
        except Exception as e:
            failed[label] = repr(e)

    reloaded_plugins: List[str] = []
    for name in plugins or []:
        try:
            import nonebot

            target = str(name)
            known = {p.name: p for p in nonebot.get_loaded_plugins()}
            plugin = known.get(target)
            if plugin is None:
                failed[f"plugin:{name}"] = "未找到该插件（名称需与插件目录名一致）"
                continue
            # NoneBot 的 reload_plugin 是同步函数，返回 None 表示重载失败
            if nonebot.reload_plugin(plugin.name) is None:
                failed[f"plugin:{name}"] = "reload_plugin 返回 None，重载失败"
            else:
                reloaded_plugins.append(plugin.name)
        except Exception as e:
            failed[f"plugin:{name}"] = repr(e)

    logger.info(f"guanli 面板触发热重载：{done} 插件={reloaded_plugins}")
    return {"ok": not failed, "reloaded": done, "plugins": reloaded_plugins, "failed": failed}


def _reload_config() -> None:
    from util.config_manager import get_config_manager

    get_config_manager().load_config()


def _reload_permission() -> None:
    from util.permission import reload_permission

    reload_permission()


def _reload_context() -> None:
    from util.context import reload_context

    reload_context()


def _reload_all_configs() -> None:
    from util.configs import reload_all

    reload_all()


# ------------------------------------------------------------------ 启动 / 停止
def _handle() -> Optional[Dict[str, Any]]:
    """取挂在 nonebot 模块上的面板句柄（插件热重载后依然有效）"""
    import nonebot

    handle = getattr(nonebot, _SERVER_ATTR, None)
    return handle if isinstance(handle, dict) else None


def _set_handle(handle: Optional[Dict[str, Any]]) -> None:
    import nonebot

    setattr(nonebot, _SERVER_ATTR, handle)


def is_running() -> bool:
    handle = _handle()
    if not handle:
        return False
    thread = handle.get("thread")
    return bool(thread is not None and thread.is_alive())


def start_server() -> None:
    """在后台线程里跑 uvicorn，不干扰 NoneBot 自己的服务"""
    if is_running():
        return

    try:
        import uvicorn
    except ImportError:
        logger.error("guanli 管理面板未启动：缺少 uvicorn，请 pip install uvicorn")
        return

    app = build_app()
    host, port = env_host(), env_port()

    # HTTPS：默认用自签证书（见 tls.py）。端口直通把面板摆在公网上，
    # 登录口令与 Cookie 不该走明文；证书不可用时回落 HTTP，不影响面板启动。
    # 证书 SAN 要跟着「用户实际访问的地址」走：GUANLI_BASE_URL 之外，把面板设置里
    # 登记的对外地址也带上，否则换个地址打开就会报证书名称不匹配。
    ssl_kwargs: Dict[str, Any] = {}
    if panel_tls.tls_settings()["enabled"]:
        registered = (_load_state().get("public_host") or "").strip()
        files = panel_tls.ensure_certificates([registered] if registered else [])
        if files:
            _TLS_STATE["certfile"], _TLS_STATE["keyfile"] = files
            ssl_kwargs = {"ssl_certfile": files[0], "ssl_keyfile": files[1]}
        else:
            logger.warning(
                "面板未能启用 HTTPS（证书不可用），登录口令将以明文传输；"
                "请检查 cryptography 是否安装 / 证书文件是否可读"
            )
    else:
        logger.warning("面板已按 GUANLI_SSL=false 关闭 HTTPS，登录口令将以明文传输")

    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning", access_log=False, **ssl_kwargs
    )
    server = uvicorn.Server(config)
    # 子线程里不能注册信号处理器，否则会抛 ValueError
    server.install_signal_handlers = lambda: None  # type: ignore[assignment]

    def _run() -> None:
        try:
            server.run()
        except Exception as e:  # 端口被占用等
            logger.error(f"guanli 管理面板启动失败（{host}:{port}）：{e!r}")

    thread = threading.Thread(target=_run, name="guanli-panel", daemon=True)
    thread.start()
    _set_handle({"server": server, "thread": thread})

    # 等 uvicorn 真正把端口监听起来再报成功。端口被占用时 server.started 会一直是 False，
    # 而以前是无条件打 success —— 出问题时日志会把人往错的方向带，
    # 公网侧的表现就是浏览器「拒绝连接」。
    for _ in range(50):  # 最多等 5 秒
        if getattr(server, "started", False) or not thread.is_alive():
            break
        time.sleep(0.1)

    if getattr(server, "started", False):
        logger.success(
            f"guanli 管理面板已启动：{panel_base_url()}  （监听 {host}:{port}）"
        )
        if panel_uses_https():
            logger.info(
                "面板用的是自签证书：浏览器首次访问会提示「证书不受信任」，"
                "点「继续访问」即可（换成正式证书见 GUANLI_SSL_CERTFILE / GUANLI_SSL_KEYFILE）"
            )
        if host in ("127.0.0.1", "localhost", "::1"):
            logger.warning(
                f"guanli 面板只监听本机 {host}:{port}，公网打不开。"
                "端口直通需要在 .env 里把 GUANLI_HOST 设为 0.0.0.0，"
                "并在云安全组/防火墙放行 GUANLI_PORT"
            )
    else:
        _TLS_STATE["certfile"] = _TLS_STATE["keyfile"] = ""
        logger.error(
            f"guanli 管理面板未能监听 {host}:{port}，公网访问会提示「拒绝连接」。"
            "排查：1) 端口是否被占用 ss -ltnp | grep 28788；"
            "2) GUANLI_HOST 是否已是 0.0.0.0（只绑 127.0.0.1 时公网必然拒绝）；"
            "3) 云服务器安全组/防火墙是否放行 28788"
        )


def stop_server(timeout: float = 5.0) -> None:
    """停止面板线程；句柄清空后再次 start_server 即可换新代码"""
    handle = _handle()
    if not handle:
        return
    try:
        server = handle.get("server")
        if server is not None:
            server.should_exit = True
        thread = handle.get("thread")
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
    except Exception as e:
        logger.warning(f"guanli 管理面板停止异常：{e!r}")
    finally:
        _TLS_STATE["certfile"] = _TLS_STATE["keyfile"] = ""
        _set_handle(None)


def restart_server() -> None:
    """换用新代码重启面板（插件热重载时调用）"""
    stop_server()
    start_server()


def purge_modules() -> List[str]:
    """把 ``util.guanli_panel*`` 从 ``sys.modules`` 里摘掉，下次 import 即读磁盘新代码"""
    import sys

    removed = [n for n in list(sys.modules) if n == _PKG or n.startswith(_PKG + ".")]
    for name in removed:
        sys.modules.pop(name, None)
    return removed


def restart_with_fresh_code(delay: float = 0.8) -> str:
    """用磁盘上的新代码重启面板后端

    先返回响应、再延迟重启，避免把正在处理请求的进程直接杀掉：

    1. 重新 import ``util.guanli_panel.web``（调用方通常已先 ``purge_modules()``）；
    2. 用新模块停掉旧进程（句柄挂在 ``nonebot`` 上，新旧模块都能取到）并重新起一个。
    """
    def _run() -> None:
        import importlib

        try:
            fresh = importlib.import_module(f"{_PKG}.web")
            fresh.stop_server()
            fresh.start_server()
            logger.success("guanli 管理面板已用新代码重启")
        except Exception as e:
            logger.error(f"guanli 管理面板热重载失败：{e!r}")

    threading.Timer(max(0.2, float(delay)), _run).start()
    return "面板将在约 1 秒后用新代码重启，稍后刷新页面即可"

