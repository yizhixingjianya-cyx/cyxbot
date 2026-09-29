"""独立静态图床服务（占 11451 端口，取代 nginx）

端口规划：面板走 ``28788``、图床走 ``11451``，两个端口都直通对外，不需要 nginx。
本模块用标准库 ``ThreadingHTTPServer`` 起一个**只读**静态文件服务，把
``util/image_host.py`` 落盘的图片对外提供：

    http://<服务器地址>:11451/ftv/tmp/<md5>.png    （临时图，24 小时 TTL）
    http://<服务器地址>:11451/ftv/keep/<md5>.png   （每日鉴毛的图，永久保留）

每次成功回图都会刷新该文件的 mtime，等于标记「这张图刚被用过」，
``util/image_host.py`` 的回收逻辑据此判断临时图是否超过 24 小时无人问津。

安全约定（端口是直接暴露在公网上的，所以这里从严）：

- 只服务 ``GET`` / ``HEAD``，其它方法一律 ``405``；
- 只暴露 URL 前缀（默认 ``ftv``）下的**文件**：不列目录、不跟随符号链接，
  路径解析后必须仍落在图床目录内（挡掉 ``../`` 穿越）；
- 图片是用户内容，不执行任何脚本，响应统一带 ``X-Content-Type-Options: nosniff``，
  按后缀给 ``Content-Type``。

配置（.env，图片相关变量整体说明见 ``util/image_host.py`` 顶部）：

- ``IMAGE_HOST_URL``    对外基地址；**不配置则图片托管关闭**，本服务也不再启动
- ``IMAGE_HOST_PORT``   本服务监听端口，默认 ``11451``
- ``IMAGE_HOST_BIND``   监听地址，默认 ``0.0.0.0``（端口直通必须全网卡，绑 127.0.0.1 公网拿不到图）
- ``IMAGE_HOST_SERVE``  置 ``false`` / ``0`` / ``off`` 可「只写图不起服务」（改由 nginx 之类提供）
- ``IMAGE_TTL_HOURS``   临时图保留小时数，默认 ``24``

启动方式：插件加载阶段调用一次 ``setup()`` 注册 nonebot 启动 / 关闭钩子，
钩子只会注册一次（标记挂在 ``nonebot`` 模块上），插件热重载不会重复占用端口。
启动钩子里先 ``init_hosting()``（建目录 + 清过期图），再起服务。
"""

from __future__ import annotations

import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlparse

from nonebot.log import logger

from . import image_index
from .env_value import text as env_text
from .image_host import (
    hosting_enabled,
    image_base_url,
    init_hosting,
    static_dir,
    touch,
    url_prefix,
)

__all__ = [
    "setup",
    "start_server",
    "stop_server",
    "is_running",
    "env_port",
    "env_bind",
    "serve_enabled",
]

_DEFAULT_PORT = 11451
_DEFAULT_BIND = "0.0.0.0"
# 句柄挂在 nonebot 模块上：插件热重载后依然拿得到同一个服务，不会重复占端口
_SERVER_ATTR = "_cangyao_image_server"
# 启动钩子只注册一次的标记
_HOOK_FLAG = "_cangyao_image_server_hooked"
# 单文件上限：图床里的图都在几百 KB 级，超限直接当不存在处理
_MAX_BYTES = 32 * 1024 * 1024


def _env_bool(name: str, default: bool) -> bool:
    raw = env_text(name).lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off", "disable", "disabled")


def env_port() -> int:
    """图床监听端口：``IMAGE_HOST_PORT`` > 从 ``IMAGE_HOST_URL`` 里取 > 默认 11451"""
    raw = env_text("IMAGE_HOST_PORT")
    if raw:
        try:
            port = int(raw)
            if 1 <= port <= 65535:
                return port
        except ValueError:
            logger.warning(f"IMAGE_HOST_PORT 不是合法端口（{raw}），回落到默认 {_DEFAULT_PORT}")
    base = image_base_url()
    if base:
        try:
            netloc = urlparse(base).netloc or urlparse(f"//{base}").netloc
            if ":" in netloc:
                return int(netloc.rsplit(":", 1)[1])
        except ValueError:
            pass
    return _DEFAULT_PORT


def env_bind() -> str:
    """监听地址，默认 ``0.0.0.0``（端口直通对外）"""
    return env_text("IMAGE_HOST_BIND") or _DEFAULT_BIND


def serve_enabled() -> bool:
    """是否启动静态服务：默认跟随「图片托管是否已配置」，可用 ``IMAGE_HOST_SERVE`` 覆盖"""
    return _env_bool("IMAGE_HOST_SERVE", hosting_enabled())


def _status_text() -> str:
    return f"cangyao image host ok\n{image_base_url()}\n"


def _safe_target(rel: str) -> Optional[Path]:
    """把 URL 相对路径映射成图床目录内的真实文件；越界或非法返回 ``None``"""
    if not rel or rel.startswith(".") or "\x00" in rel:
        return None
    try:
        root = static_dir().resolve()
        target = (root / rel).resolve()
    except OSError:
        return None
    # target 必须严格位于 root 之内（符号链接指到外面也会在这里被拒）
    if target == root or root not in target.parents:
        return None
    return target


class _ImageHandler(BaseHTTPRequestHandler):
    """只读静态处理器：命中 ``/<prefix>/<文件名>`` 就回图，其余一律 404"""

    server_version = "cangyao-image-host/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # 图片请求很碎，访问日志只会刷屏；出错也不打堆栈
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def log_error(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler 约定的方法名
        self._serve(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(head_only=True)

    def do_POST(self) -> None:  # noqa: N802
        self._send(405, b"method not allowed", "text/plain; charset=utf-8", head_only=False)

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_POST

    def _send(
        self,
        status: int,
        body: bytes,
        ctype: str,
        *,
        head_only: bool,
        extra: Optional[Dict[str, str]] = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if head_only or not body:
            return
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _serve(self, *, head_only: bool) -> None:
        try:
            self._serve_inner(head_only=head_only)
        except Exception:  # 单个请求出问题不能把服务线程带走
            self._send(500, b"internal error", "text/plain; charset=utf-8", head_only=head_only)

    def _serve_inner(self, *, head_only: bool) -> None:
        prefix = url_prefix()
        rel = unquote(urlparse(self.path).path).lstrip("/")

        # 首页与 /health 给一行状态文本，方便浏览器里直接确认「服务活着」；不做目录列表
        if rel in ("", "health", prefix):
            self._send(
                200,
                _status_text().encode("utf-8"),
                "text/plain; charset=utf-8",
                head_only=head_only,
            )
            return

        if not rel.startswith(prefix + "/"):
            self._send(404, b"not found", "text/plain; charset=utf-8", head_only=head_only)
            return

        target = _safe_target(rel[len(prefix) + 1:])
        if target is None or not target.is_file():
            self._send(404, b"not found", "text/plain; charset=utf-8", head_only=head_only)
            return

        try:
            if target.stat().st_size > _MAX_BYTES:
                self._send(404, b"not found", "text/plain; charset=utf-8", head_only=head_only)
                return
            data = target.read_bytes()
        except OSError:
            self._send(500, b"read error", "text/plain; charset=utf-8", head_only=head_only)
            return

        # 有人来取 = 这张图「刚被用过」：往索引里记一笔（used_at / hits），
        # 顺便刷新 mtime 作兜底。回收临时图就看这个时间，常看的图不会被误删。
        touch(target)
        image_index.mark_used(target.name, image_index.scope_of(target))

        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(
            200,
            data,
            ctype,
            head_only=head_only,
            # 文件名是内容 md5，内容永不变化 → 放心让浏览器 / 平台侧长期缓存
            extra={"Cache-Control": "public, max-age=604800, immutable"},
        )


def _handle() -> Optional[Dict[str, Any]]:
    """取挂在 nonebot 模块上的图床句柄（插件热重载后依然有效）"""
    import nonebot

    handle = getattr(nonebot, _SERVER_ATTR, None)
    return handle if isinstance(handle, dict) else None


def is_running() -> bool:
    """图床服务线程是否在跑"""
    handle = _handle()
    if not handle:
        return False
    thread = handle.get("thread")
    return bool(thread is not None and thread.is_alive())


def start_server() -> None:
    """在后台线程里跑静态服务；失败只打日志，不影响机器人本体"""
    if is_running():
        return

    host, port = env_bind(), env_port()
    try:
        httpd = ThreadingHTTPServer((host, port), _ImageHandler)
    except OSError as e:
        logger.error(
            f"图床静态服务启动失败（{host}:{port}）：{e!r}。"
            "排查：端口是否被占用（ss -ltnp | grep 11451）；"
            "或在 .env 用 IMAGE_HOST_PORT 换一个端口"
        )
        return

    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, name="image-host", daemon=True)
    thread.start()

    import nonebot

    setattr(
        nonebot,
        _SERVER_ATTR,
        {"server": httpd, "thread": thread, "host": host, "port": port},
    )
    logger.success(
        f"图床静态服务已启动：{image_base_url() or '（未配置 IMAGE_HOST_URL）'} "
        f"→ {host}:{port}（本地目录 {static_dir()}）"
    )
    if host in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            f"图床只监听本机 {host}:{port}，公网拿不到图片。"
            "端口直通请在 .env 里把 IMAGE_HOST_BIND 设为 0.0.0.0"
        )
    if not hosting_enabled():
        logger.warning("未配置 IMAGE_HOST_URL，图片 URL 拼不出来，图床服务形同虚设")


def stop_server(timeout: float = 3.0) -> None:
    """停止图床服务（机器人退出时调用）"""
    handle = _handle()
    if not handle:
        return
    try:
        httpd = handle.get("server")
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        thread = handle.get("thread")
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
    except Exception as e:
        logger.warning(f"图床静态服务停止异常：{e!r}")
    finally:
        import nonebot

        setattr(nonebot, _SERVER_ATTR, None)


def setup() -> None:
    """注册 nonebot 启动 / 关闭钩子（可重复调用，只会注册一次）

    ``util`` 会在 ``bot.py`` 顶部（``nonebot.init()`` 之前）被 import，那时还没有
    driver；所以拿不到 driver 时直接返回，留给插件加载阶段再调一次。
    """
    try:
        import nonebot

        if getattr(nonebot, _HOOK_FLAG, False):
            return
        driver = nonebot.get_driver()
    except Exception:
        return

    setattr(nonebot, _HOOK_FLAG, True)

    @driver.on_startup
    async def _image_host_start() -> None:
        # 先初始化图床：建 tmp/ 与 keep/ 目录、清掉超过 TTL 没被用过的临时图。
        # 放在起服务之前，这样启动日志里「初始化完成」先出现，出问题也一眼看得出。
        try:
            init_hosting()
        except Exception as e:  # 图床初始化绝不能挡住机器人启动
            logger.warning(f"图床初始化异常（不影响启动）：{e!r}")

        if serve_enabled():
            start_server()
        elif not env_text("IMAGE_HOST_SERVE"):
            # 没显式关，是被「没配 IMAGE_HOST_URL」连带的：图 URL 都拼不出来，起服务也没用
            logger.warning(
                "图床静态服务未启动：没配置 IMAGE_HOST_URL，图片公网地址拼不出来"
                "（服务器地址:端口，见 util/image_host.py 顶部说明）"
            )
        else:
            logger.warning(
                f"图床静态服务未启动：.env 里 IMAGE_HOST_SERVE={env_text('IMAGE_HOST_SERVE')}。"
                "想让本机 11451 直接对外提供图片，把这一行注释掉再重启"
            )

    @driver.on_shutdown
    async def _image_host_stop() -> None:
        stop_server()
