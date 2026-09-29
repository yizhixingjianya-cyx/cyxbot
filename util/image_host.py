"""把图片落到本机静态目录，换成公网可访问 URL。

适用范围：图片**本机生成**（源站没有，如 ftv 的档案图 / 统计图），或者图片在**源站**
但抓不稳（每日鉴毛的源站链接带签名、几分钟过期、还可能有防盗链）。

为什么不能直接把源站链接交给 QQ：QQ 官方 markdown 里的图片**不是「显示链接」**，
而是由开放平台自己把资源下载转存；它抓不到就只剩 alt 文字，客户端表现成一个
「[每日鉴毛]」方块。转存到我们自己的图床后 URL 干净、无鉴权、长期有效，QQ 一定能拿到。
（每日鉴毛的转存见 src/plugins/pyfurbot/command/furry_will.py 的 ``_publish_image``。）

QQ 官方 markdown 里的图片必须是**公网 URL**（平台会去把资源下载转存），
所以想让「图片 + 文字 + 按钮」落在同一条消息里，就得先把图片落到公网可访问的
静态目录，再用 markdown 图片语法 ``![说明 #宽 #高](url)`` 和 keyboard 一起发出。

静态目录由**独立服务**对外提供，不依赖 nginx：

- ``util/static_server.py``：只读静态服务，默认监听 ``0.0.0.0:11451``，
  图片地址形如 ``http://<服务器地址>:11451/ftv/tmp/<md5>.png``；
- 想换端口 / 监听地址就配 ``IMAGE_HOST_PORT`` / ``IMAGE_HOST_BIND``；
- 若已有自己的 Web 服务器（nginx 等），设 ``IMAGE_HOST_SERVE=false`` 只写图不起服务，
  再把静态目录指给那个服务器即可。

**生命周期**（启动时初始化、跑着的时候自己回收）：

- 机器人启动时调 ``init_hosting()``：建目录、接上索引库、清一遍过期图、打一行状态日志；
- 图床分两个子目录，按用途存放：
  - ``tmp/``：**临时图**（ftv 档案/统计图、awmc 表情包等本机生成的图），
    超过 ``IMAGE_TTL_HOURS``（默认 **24 小时**）没被用过就删掉；
  - ``keep/``：**持久图**（每日鉴毛转存的卡片），永久保留，不参与回收。
- 「用过」记在 **SQLite 索引**里（``util/image_index.py``，默认库
  ``src/data/image_index.db``）：每次发布图片、每次被静态服务取图都刷新那行的
  ``used_at``；回收直接查「``scope='tmp'`` 且 ``used_at`` 早于 TTL」的行。
  文件名是内容 md5，同图复用同一 URL，因此一次发布长期有效，但 24 小时内
  无人访问的临时图会被回收。
- 索引只是主账本，文件 mtime 同步刷新作兜底：索引里没记着的文件（早期版本留在
  根目录的旧图、手工拷进来的副本）退回按 mtime 判断，同样会过期清理。
- 想改保留时长配 ``IMAGE_TTL_HOURS``（``0`` = 不回收临时图，全部永久保留）；
  想挪索引库配 ``IMAGE_INDEX_DB``。

QQ 客户端与开放平台都会缓存图片本身，所以临时图被回收不影响**已经发出去的消息**；
但如果隔了一天才去翻旧消息里那张临时图，可能就看不到了（每日鉴毛的图不受影响）。

配置（.env，均可选）。通用名与旧名等价，旧名 ``FTV_*`` 保留兼容：

- ``IMAGE_HOST_URL`` / ``FTV_IMAGE_HOST``
  对外基地址，指向上述静态服务。**不设置则关闭图片托管**，调用方回退为
  「单图 + 纯文本」一个气泡（不会出现打不开的坏图）。服务器在 NAT 后面时写
  **映射后的公网端口**；共享 NAT 端口场景直接写 ``IP:端口``（自动补 ``http://``），
  例如 ``IMAGE_HOST_URL=1.2.3.4:11451``。
- ``IMAGE_HOST_DIR`` / ``FTV_IMAGE_DIR``
  本机静态目录；默认自动探测：Linux 下 ``/var/www/html/<prefix>``
  （install_ubuntu.sh 会建好这个目录），否则 ``<项目根>/html/<prefix>``
- ``IMAGE_HOST_PREFIX`` / ``FTV_IMAGE_PREFIX``  URL 路径前缀，默认 ``ftv``
- ``IMAGE_HOSTING`` / ``FTV_IMAGE_HOSTING``      置 ``false`` / ``0`` / ``off`` 可整体关闭
- ``IMAGE_TTL_HOURS``   临时图的保留小时数，默认 ``24``；置 ``0`` 关闭回收
- ``IMAGE_INDEX_DB``    索引库位置，默认 ``src/data/image_index.db``（相对项目根）

关闭或写入失败时 ``host_bytes()`` / ``host_image()`` 返回 ``None``，调用方应回落为
「纯文本 + 图片」（一个气泡）或原来的两条消息。
"""

from __future__ import annotations

import hashlib
import os
import time
from io import BytesIO
from pathlib import Path
from typing import Optional

from nonebot.log import logger

from . import image_index
from .env_value import text as env_text

__all__ = [
    "hosting_enabled",
    "host_bytes",
    "host_image",
    "image_markdown",
    "md_display_size",
    "MD_MAX_WIDTH",
    "MD_MAX_HEIGHT",
    "sniff_ext",
    "image_base_url",
    "static_dir",
    "temp_dir",
    "keep_dir",
    "url_prefix",
    "init_hosting",
    "purge_expired",
    "temp_ttl",
    "touch",
    "KEEP_DIRNAME",
    "TEMP_DIRNAME",
]

_DEFAULT_PREFIX = "ftv"
# 临时图与持久图各自占一个子目录：tmp/ 会被回收，keep/ 永久保留
TEMP_DIRNAME = "tmp"
KEEP_DIRNAME = "keep"
_DEFAULT_TTL_HOURS = 24  # 临时图默认保留 24 小时（按最后一次「被用过」算）
_CLEANUP_INTERVAL = 3600  # 清理节流：最多每小时扫一次
_KEEP_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
_last_cleanup = 0.0
_host_warned = False
_host_logged = False

# util/image_host.py -> 项目根
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _env(primary: str, legacy: str = "") -> str:
    """读环境变量：优先通用名，其次旧名（FTV_*），都没有返回空串

    取值统一走 ``env_value.text``，顺手剥掉手写 .env 时常见的反引号 / 引号。
    """
    for name in (primary, legacy):
        if not name:
            continue
        raw = env_text(name)
        if raw:
            return raw
    return ""


def _truthy(name: str, legacy: str, default: bool = True) -> bool:
    raw = _env(name, legacy).lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off", "disable", "disabled")


def hosting_enabled() -> bool:
    """图片托管是否启用：需显式配置对外地址，且未被 IMAGE_HOSTING 关闭"""
    if not _truthy("IMAGE_HOSTING", "FTV_IMAGE_HOSTING", True):
        return False
    return bool(_env("IMAGE_HOST_URL", "FTV_IMAGE_HOST"))


def image_base_url() -> str:
    """对外基地址；允许只写 ``IP:端口``（自动补 http://），末尾斜杠自动去掉"""
    raw = _env("IMAGE_HOST_URL", "FTV_IMAGE_HOST").rstrip("/")
    if raw and "://" not in raw:
        raw = f"http://{raw}"
    return raw


def url_prefix() -> str:
    """URL 路径前缀（默认 ``ftv``），静态服务按它匹配路径"""
    return _env("IMAGE_HOST_PREFIX", "FTV_IMAGE_PREFIX").strip("/") or _DEFAULT_PREFIX


def static_dir() -> Path:
    """本机静态目录：静态服务把这个目录下（前缀内）的文件对外提供"""
    raw = _env("IMAGE_HOST_DIR", "FTV_IMAGE_DIR")
    if raw:
        return Path(raw).expanduser()
    www = Path("/var/www/html")
    if www.is_dir():
        return www / url_prefix()
    return _PROJECT_ROOT / "html" / url_prefix()


# 兼容旧调用（同模块内的私有别名）
_url_prefix = url_prefix
_static_dir = static_dir


def temp_dir() -> Path:
    """临时图目录（会被 24 小时 TTL 回收）：本机生成的档案图 / 统计图 / 表情包"""
    return static_dir() / TEMP_DIRNAME


def keep_dir() -> Path:
    """持久图目录（永不回收）：每日鉴毛转存的卡片"""
    return static_dir() / KEEP_DIRNAME


def temp_ttl() -> float:
    """临时图保留秒数：``IMAGE_TTL_HOURS`` 小时；``0`` 表示不回收（返回 0）"""
    raw = env_text("IMAGE_TTL_HOURS").strip()
    hours = _DEFAULT_TTL_HOURS
    if raw:
        try:
            hours = float(raw)
        except ValueError:
            logger.warning(f"IMAGE_TTL_HOURS 不是数字（{raw}），按默认 {_DEFAULT_TTL_HOURS} 小时")
            hours = _DEFAULT_TTL_HOURS
    if hours <= 0:
        return 0.0
    return hours * 3600.0


def sniff_ext(data: bytes) -> str:
    """按文件头判断图片类型，决定落盘后缀（静态服务按后缀给 Content-Type）"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def touch(path: Path) -> None:
    """刷新文件的 mtime：表示这张图「刚被用过一次」

    图床服务每次回图、``host_bytes`` 命中已有文件时都会调它。

    **现在判断过期以 SQLite 索引为准**（``util/image_index.py`` 的 ``used_at``），
    mtime 只是兜底：库里的行丢了 / 手工拷进来的文件，还能靠它判断新旧。
    两个都刷新，等于双保险。
    """
    try:
        os.utime(path, None)
    except OSError:
        pass


def _purge_strays(root: Path, ttl: float, now: float, managed: set) -> int:
    """删掉「索引没记着」且 mtime 也过期的图 —— 早期版本留在根目录的旧图、手工拷进来的副本

    索引里管着的文件不在这里判断（那是 ``expired()`` 的活），避免两套时间打架。
    """
    keep = root / KEEP_DIRNAME
    removed = 0
    try:
        candidates = list(root.rglob("*"))
    except OSError:
        return 0
    for path in candidates:
        try:
            if not path.is_file() or path.is_symlink():
                continue
            # keep/ 下的持久图（以及 keep 目录自身）一律跳过
            if keep == path or keep in path.parents:
                continue
            name = path.name
            if name.startswith(".") or name in managed:
                continue
            if path.suffix.lower() not in _KEEP_SUFFIXES:
                continue
            if now - path.stat().st_mtime > ttl:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def purge_expired(root: Optional[Path] = None, ttl: float = 0.0) -> int:
    """回收过期的临时图，返回删除张数

    过期判断走 SQLite 索引（``util/image_index.py``）：

    1. 查 ``images`` 表里 ``scope='tmp'`` 且 ``used_at`` 超过 TTL 的行，删文件 + 删行；
    2. 再兜底扫一遍目录，清掉**索引里没有**、mtime 也过期的漏网文件。

    ``keep/`` 下的持久图（每日鉴毛）永不参与回收。
    ``ttl`` 传 0 时读 ``IMAGE_TTL_HOURS``（默认 24 小时）；配成 0 小时即整体不回收。
    任何单个文件 / 单条 SQL 的错误都不会中断整轮清理。
    """
    root = root or static_dir()
    if not root.is_dir():
        return 0
    ttl = ttl or temp_ttl()
    if ttl <= 0:
        return 0

    now = time.time()
    removed = 0
    temp_root = temp_dir()
    managed = image_index.names(image_index.SCOPE_TMP)

    for name in image_index.expired(ttl, now, image_index.SCOPE_TMP):
        path = temp_root / name
        try:
            if path.is_file():
                path.unlink()
                removed += 1
        except OSError:
            pass
        # 文件删不掉（权限等）也把行清掉：不然每次启动都会拿同一批名字重试
        image_index.drop(name, image_index.SCOPE_TMP)

    removed += _purge_strays(root, ttl, now, managed)
    return removed


def _cleanup(directory: Path) -> None:
    """发布图片时顺手回收（节流：最多每小时扫一次，避免每张图都遍历目录）"""
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < _CLEANUP_INTERVAL:
        return
    _last_cleanup = now
    removed = purge_expired(directory)
    if removed:
        logger.info(f"图床回收了 {removed} 张超过 {temp_ttl() / 3600:.0f} 小时没用过的临时图")


def init_hosting() -> None:
    """机器人启动时初始化图床：建目录 + 接上索引库 + 清一遍过期图 + 打一行状态日志

    由 ``util/static_server.py`` 的启动钩子调用（见该模块 ``setup()``）。
    没配 ``IMAGE_HOST_URL``（图片托管整体关闭）时只提示一句，不做任何事。
    """
    if not hosting_enabled():
        logger.info("图床未启用（未配置 IMAGE_HOST_URL），跳过初始化")
        return

    root = static_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
        temp_dir().mkdir(parents=True, exist_ok=True)
        keep_dir().mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error(f"图床目录创建失败（{root}）：{e}，图片托管将不可用")
        return

    # 索引库：建表 + 清掉「库里有、盘上没了」的幽灵行（换了静态目录 / 手工删过文件）
    if image_index.ensure():
        image_index.forget_missing(root)
    else:
        logger.warning("图床索引库不可用，本次按文件 mtime 兜底判断过期")

    removed = purge_expired(root)
    ttl = temp_ttl()
    ttl_text = f"{ttl / 3600:.0f} 小时" if ttl else "不回收"

    stat = image_index.stats()
    tmp_stat = stat.get(image_index.SCOPE_TMP, {"count": 0, "bytes": 0})
    keep_stat = stat.get(image_index.SCOPE_KEEP, {"count": 0, "bytes": 0})
    logger.success(
        f"图床初始化完成 | 目录 {root} | 临时图保留 {ttl_text}（{TEMP_DIRNAME}/）| "
        f"每日鉴毛的图永久保留（{KEEP_DIRNAME}/）| 本次清理过期图 {removed} 张 | "
        f"现存 临时 {tmp_stat['count']} 张/{_mb(tmp_stat['bytes'])}、"
        f"持久 {keep_stat['count']} 张/{_mb(keep_stat['bytes'])} | "
        f"索引 {image_index.db_path()}"
    )


def _mb(size: int) -> str:
    """把字节数写得好看点（日志里用）"""
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f}MB"
    return f"{size / 1024:.0f}KB"


def host_bytes(data: bytes, ext: str = "", persistent: bool = False) -> Optional[str]:
    """把图片字节写入静态目录并返回公网 URL；不可用/失败返回 None。

    ``ext`` 留空时按文件头自动判断（``.png`` / ``.jpg`` / ``.webp`` / ``.gif``）。
    文件名取内容 md5，同图复用同一 URL，便于 CDN 与客户端缓存 —— 命中已有文件时
    会刷新它的使用时间（索引里的 ``used_at`` 与文件 mtime），等于把回收期限往后推。

    每次发布都往 SQLite 索引（``util/image_index.py``）记一笔，
    回收时就不用去扫目录、也不再受外部改 mtime 的影响。

    ``persistent`` 决定落在哪个子目录：

    - ``False``（默认）→ ``tmp/``，**24 小时没被用过就回收**（ftv 档案/统计图、
      awmc 表情包等，重发就能再生成，没必要长期占盘）；
    - ``True`` → ``keep/``，**永久保留**（每日鉴毛的转存图，翻旧消息还得看得见）。
    """
    global _host_warned, _host_logged
    if not hosting_enabled():
        return None

    directory = keep_dir() if persistent else temp_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        if not _host_warned:
            _host_warned = True
            logger.warning(f"图床目录不可写（{directory}）：{e}，将回落为分条发送")
        return None

    name = hashlib.md5(data).hexdigest() + (ext or sniff_ext(data))
    path = directory / name
    if path.exists():
        touch(path)  # 同图复用：记一次「刚用过」，回收期限顺延
    else:
        try:
            tmp = directory / f".{name}.tmp"
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except OSError as e:
            if not _host_warned:
                _host_warned = True
                logger.warning(f"图床写入失败（{path}）：{e}，将回落为分条发送")
            return None

    # 记账：这张图是 tmp 还是 keep、多大、什么时候落的 / 刚被用过
    image_index.record(name, directory.name, len(data))

    _cleanup(static_dir())
    prefix = url_prefix()
    parts = [p for p in (prefix, directory.name, name) if p]
    url = f"{image_base_url()}/{'/'.join(parts)}"

    if not _host_logged:
        _host_logged = True
        logger.info(f"图床已启用：对外地址 {image_base_url()}，本地目录 {static_dir()}")
    return url


def host_image(img, persistent: bool = False) -> Optional[str]:
    """把 PIL 图片存成 PNG 后发布，返回公网 URL；失败返回 None。

    ``persistent`` 语义同 ``host_bytes``：默认进 ``tmp/``（24 小时 TTL），
    每日鉴毛那种要长期留的图传 ``True`` 进 ``keep/``。
    """
    try:
        buf = BytesIO()
        img.save(buf, format="PNG")
    except Exception as e:
        logger.warning(f"图床图片编码 PNG 失败：{e}")
        return None
    return host_bytes(buf.getvalue(), ".png", persistent=persistent)


def image_markdown(url: str, alt: str = "图片", width: int = 0, height: int = 0) -> str:
    """拼 QQ markdown 图片语法：``![说明 #宽 #高](url)``（宽高为 0 时省略尺寸）。"""
    safe_alt = (alt or "图片").replace("[", "（").replace("]", "）").replace("\n", " ")
    size = f" #{int(width)}px #{int(height)}px" if width and height else ""
    return f"![{safe_alt}{size}]({url})"


# 官方《Markdown 格式说明》给的两条建议：宽不超过 720px、高不超过 1080px。
# 只是「建议」，但照它走手机端观感最好（否则一张长图能占好几屏）。
MD_MAX_WIDTH = 720
MD_MAX_HEIGHT = 1080


def md_display_size(
    width: int,
    height: int,
    max_width: int = MD_MAX_WIDTH,
    max_height: int = MD_MAX_HEIGHT,
) -> tuple:
    """算出 markdown 该回填的**显示尺寸**（等比，不放大）

    只改「回填给 QQ 的数字」，**不动图片本身**：图床上放的还是原图，
    缩放交给客户端按这个尺寸去做，因此无需为了排版重新编码、掉画质。

    - 没超建议尺寸：原样返回；
    - 超了：等比缩进 ``max_width`` x ``max_height`` 的框；
    - 宽高不合法（0/负数，通常是没读出尺寸）：返回 ``(0, 0)``。注意 markdown
      图片**必须**带尺寸，所以这种图应改走普通消息，而不是硬发不带尺寸的 markdown。
    """
    if width <= 0 or height <= 0:
        return 0, 0
    if width <= max_width and height <= max_height:
        return int(width), int(height)
    ratio = min(max_width / width, max_height / height)
    return max(1, int(width * ratio)), max(1, int(height * ratio))
