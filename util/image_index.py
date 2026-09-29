"""图床索引（SQLite）：记录每张图**什么时候落的盘、最近什么时候被用过**。

为什么不用文件 mtime 当唯一依据：

- mtime 太容易被外力改掉 —— ``rsync`` / 备份还原 / 手动 ``cp`` 都会把时间刷成
  「现在」，于是「24 小时没人用过」的判断直接失真，该删的删不掉；
- 回收时只需要知道「过期的是哪几张」，查库比遍历目录 ``stat`` 每个文件快得多；
- 顺手能统计：临时图 / 持久图各几张、占多少盘、命中多少次，启动日志与面板都能用。

库文件默认 ``src/data/image_index.db``（相对项目根），可用 ``IMAGE_INDEX_DB`` 改。
表 ``images`` 一行一张图，主键是 **``(name, scope)``**：

==============  ==========================================================
``name``        文件名（内容 md5 + 后缀），也是 URL 最后一段
``scope``       ``tmp`` / ``keep``，对应静态目录下的同名子目录
``size``        字节数
``created_at``  首次落盘时间（unix 秒）
``used_at``     最后一次被用（发布 / 被取图），**回收就看它**
``hits``        被用次数
==============  ==========================================================

主键为什么带上 ``scope``：文件名取内容 md5，**同一张图可能同时以临时和持久两种身份
存在**（URL ``/ftv/tmp/<md5>.png`` 与 ``/ftv/keep/<md5>.png`` 本来就是两个资源），
只用 name 当主键的话后写的一行会把前一行顶掉，临时 / 持久就分不清了。

线程安全：静态服务跑在后台线程、回收跑在 asyncio 线程，所以每次操作开一个短连接
（sqlite3 连接不跨线程复用），写操作统一过一把模块级锁，库开 WAL。
图床索引**只是记账**：库出问题（磁盘满、文件损坏）时记一笔日志就算了，
绝不能让发图 / 回收流程跟着挂掉 —— 所以对外函数一律吞掉 ``sqlite3.Error``。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from nonebot.log import logger

from .env_value import text as env_text

__all__ = [
    "SCOPE_TMP",
    "SCOPE_KEEP",
    "DEFAULT_DB",
    "db_path",
    "scope_of",
    "ensure",
    "record",
    "mark_used",
    "expired",
    "names",
    "drop",
    "forget_missing",
    "stats",
]

SCOPE_TMP = "tmp"
SCOPE_KEEP = "keep"

DEFAULT_DB = "src/data/image_index.db"

# 同一张图 60 秒内只写一次 used_at：图片会被 QQ 反复取，没必要每次都落一条写
_USED_THROTTLE = 60.0
# _recent 的容量上限，超过就整体丢掉重来（只是节流缓存，丢了不影响正确性）
_RECENT_MAX = 5000

# util/image_index.py -> 项目根
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    name       TEXT NOT NULL,
    scope      TEXT NOT NULL,
    size       INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    used_at    REAL NOT NULL,
    hits       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (name, scope)
);

CREATE INDEX IF NOT EXISTS idx_images_scope_used ON images(scope, used_at);
"""

_write_lock = threading.Lock()
_recent: Dict[Tuple[str, str], float] = {}
_ready_path: Optional[str] = None
_initializing = False


def _warn(msg: str) -> None:
    try:
        logger.warning(msg)
    except Exception:  # 脱离 NoneBot 单独跑时不当场炸
        pass


def db_path() -> Path:
    """索引库位置：``IMAGE_INDEX_DB`` > 默认 ``src/data/image_index.db``（相对项目根）"""
    raw = env_text("IMAGE_INDEX_DB").strip()
    path = Path(raw) if raw else Path(DEFAULT_DB)
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return path


def scope_of(path: Path) -> str:
    """从落盘路径反推 scope：父目录叫 ``keep`` 就是持久图，其余按临时图算

    静态服务回图时拿得到的是文件路径，用这个把 scope 还原出来。
    """
    return SCOPE_KEEP if path.parent.name == SCOPE_KEEP else SCOPE_TMP


def _connect() -> sqlite3.Connection:
    """开一个连接；首次连上时顺便建表（``_initializing`` 防重入）"""
    global _ready_path, _initializing
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    if str(path) != _ready_path and not _initializing:
        _initializing = True
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            with conn:
                conn.executescript(_SCHEMA)
            _ready_path = str(path)
        finally:
            _initializing = False
    return conn


def _write(sql: str, params: tuple = ()) -> int:
    """执行一条写语句，返回受影响行数；库不可用时返回 0（不影响图床工作）"""
    try:
        with _write_lock:
            conn = _connect()
            try:
                with conn:
                    cur = conn.execute(sql, params)
                    return cur.rowcount or 0
            finally:
                conn.close()
    except (sqlite3.Error, OSError) as e:
        _warn(f"图床索引写入失败（{db_path()}）：{e}")
        return 0


def _read(sql: str, params: tuple = ()) -> List[sqlite3.Row]:
    """执行一条读语句；库不可用时返回空列表"""
    try:
        with _write_lock:
            conn = _connect()
            try:
                return list(conn.execute(sql, params).fetchall())
            finally:
                conn.close()
    except (sqlite3.Error, OSError) as e:
        _warn(f"图床索引读取失败（{db_path()}）：{e}")
        return []


def ensure() -> bool:
    """确保库文件与表结构就绪（启动时调一次），返回是否可用"""
    try:
        conn = _connect()
        try:
            return True
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as e:
        _warn(f"图床索引不可用（{db_path()}）：{e}")
        return False


def record(
    name: str,
    scope: str = SCOPE_TMP,
    size: int = 0,
    when: Optional[float] = None,
) -> bool:
    """记一次「这张图刚被发布」：没有则插入，有则刷新 ``used_at`` 并把 ``hits`` +1"""
    now = time.time() if when is None else when
    rowcount = _write(
        """
        INSERT INTO images (name, scope, size, created_at, used_at, hits)
        VALUES (?, ?, ?, ?, ?, 1)
        ON CONFLICT(name, scope) DO UPDATE SET
            size    = excluded.size,
            used_at = excluded.used_at,
            hits    = images.hits + 1
        """,
        (name, scope, int(size), now, now),
    )
    if rowcount:
        _recent[(scope, name)] = now
    return bool(rowcount)


def mark_used(
    name: str,
    scope: str = SCOPE_TMP,
    when: Optional[float] = None,
    throttle: float = _USED_THROTTLE,
) -> bool:
    """记一次「这张图刚被取用」（静态服务每次回图调它）

    ``throttle`` 秒内重复取同一张图只写一次库 —— QQ 抓图很频繁，不值得每张都写。
    返回 True 表示这次真的落库了。
    """
    now = time.time() if when is None else when
    key = (scope, name)
    if throttle > 0:
        last = _recent.get(key)
        if last is not None and now - last < throttle:
            return False
        if len(_recent) > _RECENT_MAX:
            _recent.clear()
        _recent[key] = now
    return bool(
        _write(
            "UPDATE images SET used_at = ?, hits = hits + 1 WHERE name = ? AND scope = ?",
            (now, name, scope),
        )
    )


def expired(ttl: float, when: Optional[float] = None, scope: str = SCOPE_TMP) -> List[str]:
    """列出 ``scope`` 下超过 ``ttl`` 秒没被用过的图片名"""
    now = time.time() if when is None else when
    cutoff = now - ttl
    rows = _read(
        "SELECT name FROM images WHERE scope = ? AND used_at <= ? ORDER BY used_at",
        (scope, cutoff),
    )
    return [r["name"] for r in rows]


def names(scope: Optional[str] = None) -> Set[str]:
    """列出库里记着的图片名（给「目录里这个文件是不是有人管」判断用）

    传 ``scope`` 就只列该目录下的；同名图在 tmp / keep 各有一行时只返回一条。
    """
    if scope:
        rows = _read("SELECT name FROM images WHERE scope = ?", (scope,))
    else:
        rows = _read("SELECT name FROM images")
    return {r["name"] for r in rows}


def drop(name: str, scope: str = SCOPE_TMP) -> bool:
    """删掉一行（文件被回收时同步调用）"""
    _recent.pop((scope, name), None)
    return bool(
        _write("DELETE FROM images WHERE name = ? AND scope = ?", (name, scope))
    )


def forget_missing(root: Path) -> int:
    """清掉「库里有、盘上没了」的幽灵行（手工删过文件 / 换过静态目录）"""
    rows = _read("SELECT name, scope FROM images")
    gone = [r for r in rows if not (root / r["scope"] / r["name"]).is_file()]
    for r in gone:
        drop(r["name"], r["scope"])
    return len(gone)


def stats() -> Dict[str, Dict[str, int]]:
    """各 scope 的张数与占用字节数，形如 ``{'tmp': {'count': 3, 'bytes': 1024}}``"""
    rows = _read(
        "SELECT scope, COUNT(*) AS count, COALESCE(SUM(size), 0) AS bytes "
        "FROM images GROUP BY scope"
    )
    out: Dict[str, Dict[str, int]] = {}
    for r in rows:
        out[r["scope"]] = {"count": int(r["count"]), "bytes": int(r["bytes"])}
    return out
