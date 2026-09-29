"""guanli 管理插件的数据层（SQLite）

数据库位置由 ``guanli.yaml`` 的 ``database_url`` 决定，默认
``sqlite:///src/data/guanli_panel.db``（相对项目根）。面板与 QQ 指令读写同一份数据。

和旧库的关系：旧版 guanli 插件把数据放在 ``src/data/guanli.db``，表结构是
``violations`` / ``group_configs`` / ``group_keys`` / ``media_monitor_status``。
新版换了更清晰的表名，所以首次建库时会做一次**只读搬迁**（见 ``_migrate_legacy``），
搬完在 ``meta`` 表里留个标记，之后不再读旧库；旧库损坏也只是跳过，不会影响启动。

线程安全：sqlite3 连接不跨线程复用，这里每次操作开一个短连接，
写操作再套一把全局锁，避免面板线程与事件循环线程同时写导致 database is locked。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .config import PROJECT_ROOT, get_config

_write_lock = threading.Lock()
_initialized: Optional[str] = None
_initializing = False

# 默认库文件（相对项目根）
_DEFAULT_DB = "src/data/guanli_panel.db"

# 旧版 guanli 插件的库位置，按优先级尝试
_LEGACY_DB_PATHS = ("src/data/guanli.db", "data/guanli.db")

# 搬迁完成标记，写在 meta 表里，避免每次启动重复扫描旧库
_MIGRATE_FLAG = "legacy_migrated"

# 群扩展开关默认值（存在 group_settings.extra 这列 JSON 里）
_DEFAULT_EXTRA: Dict[str, bool] = {
    "notice": True,      # 违禁词 / 审批等事件是否播报到群
    "stat": True,        # 是否累计发言统计
    "autoreply": True,   # 是否启用关键词自动回复
    "anti_recall": True, # 命中违禁词时是否自动撤回
}


# ------------------------------------------------------------------ 路径
def db_path() -> Path:
    """把 database_url 解析成本地文件路径（相对路径按项目根解析）"""
    url = (get_config().database_url or "").strip()
    raw = url.split("sqlite:///")[-1] if "sqlite:///" in url else url
    raw = raw or _DEFAULT_DB
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def _log_warn(msg: str) -> None:
    """优先走 NoneBot 日志；脱离 NoneBot 单独调试时静默忽略"""
    try:
        from nonebot.log import logger

        logger.warning(msg)
    except Exception:
        pass


def _connect() -> sqlite3.Connection:
    global _initialized, _initializing
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    # _initializing 是防重入闸：建表 / 搬迁过程中若再次 _connect，直接返回连接，
    # 否则会在 _initialized 赋值前递归回到这里。
    if _initialized != str(path) and not _initializing:
        _initializing = True
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            _init_schema(conn)
            _initialized = str(path)
        finally:
            _initializing = False
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS warns (
                group_id   TEXT NOT NULL,
                user_id    TEXT NOT NULL,
                count      INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (group_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS group_settings (
                group_id     TEXT PRIMARY KEY,
                watch_image  INTEGER NOT NULL DEFAULT 1,
                keywords     TEXT NOT NULL DEFAULT '[]',
                plugins      TEXT NOT NULL DEFAULT '{}',
                updated_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS secrets (
                name       TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS meta (
                name  TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            -- 发言统计（本地累加，与平台无关）
            CREATE TABLE IF NOT EXISTS message_stats (
                group_id TEXT NOT NULL,
                user_id  TEXT NOT NULL,
                count    INTEGER NOT NULL DEFAULT 0,
                last_at  TEXT NOT NULL,
                PRIMARY KEY (group_id, user_id)
            );

            -- 黑白名单：kind=black 拦截 / kind=white 豁免
            CREATE TABLE IF NOT EXISTS lists (
                group_id   TEXT NOT NULL,
                user_id    TEXT NOT NULL,
                kind       TEXT NOT NULL,
                note       TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                PRIMARY KEY (group_id, user_id, kind)
            );

            -- 关键词自动回复（group_id 为空串表示全局）
            CREATE TABLE IF NOT EXISTS autoreplies (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id   TEXT NOT NULL DEFAULT '',
                pattern    TEXT NOT NULL,
                reply      TEXT NOT NULL,
                mode       TEXT NOT NULL DEFAULT 'contains',
                enabled    INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                UNIQUE (group_id, pattern)
            );

            -- 入群申请缓存（flag 为两端通用的申请凭证）
            CREATE TABLE IF NOT EXISTS approvals (
                flag       TEXT PRIMARY KEY,
                group_id   TEXT NOT NULL,
                user_id    TEXT NOT NULL,
                comment    TEXT NOT NULL DEFAULT '',
                sub_type   TEXT NOT NULL DEFAULT 'add',
                status     TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                handled_at TEXT NOT NULL DEFAULT ''
            );

            -- 操作审计日志
            CREATE TABLE IF NOT EXISTS audit_log (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       TEXT NOT NULL,
                source   TEXT NOT NULL DEFAULT 'qq',
                actor    TEXT NOT NULL DEFAULT '',
                group_id TEXT NOT NULL DEFAULT '',
                target   TEXT NOT NULL DEFAULT '',
                action   TEXT NOT NULL DEFAULT '',
                detail   TEXT NOT NULL DEFAULT ''
            );

            -- 面板账号：provider=local 时用口令登录；provider=oidc 时口令列为空、靠 subject 认人
            CREATE TABLE IF NOT EXISTS users (
                username      TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL DEFAULT '',
                salt          TEXT NOT NULL DEFAULT '',
                role          TEXT NOT NULL DEFAULT 'admin',
                display_name  TEXT NOT NULL DEFAULT '',
                provider      TEXT NOT NULL DEFAULT 'local',
                subject       TEXT NOT NULL DEFAULT '',
                enabled       INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL,
                last_login_at TEXT NOT NULL DEFAULT ''
            );

            -- 面板会话：放库里而不是进程内存，插件热重载后可继续登录态
            CREATE TABLE IF NOT EXISTS sessions (
                sid        TEXT PRIMARY KEY,
                username   TEXT NOT NULL,
                role       TEXT NOT NULL DEFAULT 'viewer',
                provider   TEXT NOT NULL DEFAULT 'local',
                ip         TEXT NOT NULL DEFAULT '',
                ua         TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                seen_at    TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_users_subject ON users(provider, subject);
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(username);
            """
        )
        _ensure_columns(conn)
    try:
        _migrate_legacy(conn)
    except Exception as e:  # 搬迁只是锦上添花，失败不能拖垮启动
        _log_warn(f"guanli 旧数据搬迁异常（已跳过）：{e!r}")


# group_settings 的增量列：老库用 CREATE TABLE IF NOT EXISTS 建不出来，得单独补
_GROUP_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("welcome", "TEXT NOT NULL DEFAULT ''"),
    ("leave_notice", "TEXT NOT NULL DEFAULT ''"),
    ("extra", "TEXT NOT NULL DEFAULT '{}'"),
)


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """给已存在的 group_settings 补上后加的列（幂等）"""
    try:
        have = {str(r[1]) for r in conn.execute("PRAGMA table_info(group_settings)")}
    except sqlite3.Error:
        return
    for name, ddl in _GROUP_COLUMNS:
        if name in have:
            continue
        try:
            conn.execute(f"ALTER TABLE group_settings ADD COLUMN {name} {ddl}")
        except sqlite3.Error as e:
            _log_warn(f"guanli 为 group_settings 补列 {name} 失败：{e!r}")
    try:
        conn.commit()
    except sqlite3.Error:
        pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _rows(cur) -> List[Dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


# ------------------------------------------------------------------ 违规计数 / 禁言
def get_warns(group_id: Any, user_id: Any) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT count FROM warns WHERE group_id=? AND user_id=?",
            (str(group_id), str(user_id)),
        )
        row = cur.fetchone()
        return int(row["count"]) if row else 0
    finally:
        conn.close()


def add_warn(group_id: Any, user_id: Any) -> int:
    """违规次数 +1，返回累加后的次数"""
    with _write_lock:
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO warns(group_id, user_id, count, updated_at)
                    VALUES(?, ?, 1, ?)
                    ON CONFLICT(group_id, user_id)
                    DO UPDATE SET count = count + 1, updated_at = excluded.updated_at
                    """,
                    (str(group_id), str(user_id), _now()),
                )
            return get_warns(group_id, user_id)
        finally:
            conn.close()


def clear_warn(group_id: Any, user_id: Any) -> None:
    with _write_lock:
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    "DELETE FROM warns WHERE group_id=? AND user_id=?",
                    (str(group_id), str(user_id)),
                )
        finally:
            conn.close()


def list_warns(group_id: Optional[Any] = None) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        if group_id is None:
            cur = conn.execute("SELECT * FROM warns ORDER BY count DESC, updated_at DESC")
        else:
            cur = conn.execute(
                "SELECT * FROM warns WHERE group_id=? ORDER BY count DESC",
                (str(group_id),),
            )
        return _rows(cur)
    finally:
        conn.close()


# ------------------------------------------------------------------ 群设置
def _default_group_row(group_id: Any) -> Dict[str, Any]:
    return {
        "group_id": str(group_id),
        "watch_image": 1,
        "keywords": [],
        "plugins": {},
        "welcome": "",
        "leave_notice": "",
        "extra": dict(_DEFAULT_EXTRA),
        "updated_at": "",
    }


def get_group(group_id: Any) -> Dict[str, Any]:
    conn = _connect()
    try:
        cur = conn.execute("SELECT * FROM group_settings WHERE group_id=?", (str(group_id),))
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return _default_group_row(group_id)
    data = dict(row)
    data["watch_image"] = int(data.get("watch_image", 1))
    data["keywords"] = _loads(data.get("keywords"), [])
    data["plugins"] = _loads(data.get("plugins"), {})
    data["welcome"] = data.get("welcome") or ""
    data["leave_notice"] = data.get("leave_notice") or ""
    extra = _loads(data.get("extra"), {})
    if not isinstance(extra, dict):
        extra = {}
    # 缺的键落回默认值，保证前端拿到的字段永远齐全
    data["extra"] = {k: bool(extra.get(k, v)) for k, v in _DEFAULT_EXTRA.items()}
    return data


def save_group(
    group_id: Any,
    *,
    watch_image: Optional[bool] = None,
    keywords: Optional[List[str]] = None,
    plugins: Optional[Dict[str, bool]] = None,
    welcome: Optional[str] = None,
    leave_notice: Optional[str] = None,
    extra: Optional[Dict[str, bool]] = None,
) -> Dict[str, Any]:
    """局部更新群设置，未传的字段保持不变"""
    current = get_group(group_id)
    if watch_image is not None:
        current["watch_image"] = 1 if watch_image else 0
    if keywords is not None:
        cleaned = [str(k).strip() for k in keywords]
        current["keywords"] = list(dict.fromkeys(k for k in cleaned if k))
    if plugins is not None:
        merged = dict(current.get("plugins") or {})
        merged.update({str(k): bool(v) for k, v in plugins.items()})
        current["plugins"] = merged
    if welcome is not None:
        current["welcome"] = str(welcome)
    if leave_notice is not None:
        current["leave_notice"] = str(leave_notice)
    if extra is not None:
        merged_extra = dict(current.get("extra") or _DEFAULT_EXTRA)
        merged_extra.update({str(k): bool(v) for k, v in extra.items()})
        current["extra"] = merged_extra

    with _write_lock:
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO group_settings(
                        group_id, watch_image, keywords, plugins,
                        welcome, leave_notice, extra, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(group_id) DO UPDATE SET
                        watch_image  = excluded.watch_image,
                        keywords     = excluded.keywords,
                        plugins      = excluded.plugins,
                        welcome      = excluded.welcome,
                        leave_notice = excluded.leave_notice,
                        extra        = excluded.extra,
                        updated_at   = excluded.updated_at
                    """,
                    (
                        str(group_id),
                        int(current["watch_image"]),
                        json.dumps(current["keywords"], ensure_ascii=False),
                        json.dumps(current["plugins"], ensure_ascii=False),
                        current["welcome"],
                        current["leave_notice"],
                        json.dumps(current["extra"], ensure_ascii=False),
                        _now(),
                    ),
                )
        finally:
            conn.close()
    return get_group(group_id)


def list_groups() -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        cur = conn.execute("SELECT group_id FROM group_settings ORDER BY updated_at DESC")
        ids = [r["group_id"] for r in cur.fetchall()]
    finally:
        conn.close()
    return [get_group(g) for g in ids]


# ------------------------------------------------------------------ 键值（口令等）
def get_secret(name: str) -> Optional[str]:
    conn = _connect()
    try:
        cur = conn.execute("SELECT value FROM secrets WHERE name=?", (name,))
        row = cur.fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def set_secret(name: str, value: str) -> None:
    with _write_lock:
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO secrets(name, value, updated_at) VALUES(?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                    """,
                    (name, value, _now()),
                )
        finally:
            conn.close()


# ------------------------------------------------------------------ 旧库搬迁
def _legacy_tables(conn: sqlite3.Connection) -> Set[str]:
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {str(r[0]) for r in cur.fetchall()}


def _parse_keywords(raw: Any) -> List[str]:
    """旧库的关键词字段可能是 JSON 数组，也可能是逗号 / 换行分隔的字符串"""
    if raw is None:
        return []
    if isinstance(raw, list):
        items: Optional[List[str]] = [str(x) for x in raw]
    else:
        text = str(raw).strip()
        if not text:
            return []
        items = None
        if text[:1] in "[{":
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    items = [str(x) for x in parsed]
            except (TypeError, ValueError):
                items = None
        if items is None:
            items = re.split(r"[,，;；\s]+", text)
    cleaned = [str(i).strip() for i in items]
    return list(dict.fromkeys(i for i in cleaned if i))


def _find_legacy_db() -> Optional[Path]:
    """找一个存在且不是当前库的旧库文件"""
    current = str(db_path())
    for rel in _LEGACY_DB_PATHS:
        path = PROJECT_ROOT / rel
        if path.exists() and str(path) != current:
            return path
    return None


def _migrate_legacy(conn: sqlite3.Connection) -> None:
    """一次性把旧版 guanli 库的数据搬进新表（幂等）

    - ``violations``           -> ``warns``
    - ``media_monitor_status`` -> ``group_settings.watch_image``
    - ``group_configs``        -> ``group_settings.keywords``
    - ``group_keys``           -> ``secrets['group_key:<群号>']``

    旧库一律以只读方式打开：即使它已经损坏（旧版 ``src/data/guanli.db`` 的
    ``permission_cache`` 就有坏页），读不动的表跳过即可，绝不影响面板启动。
    """
    try:
        done = conn.execute("SELECT value FROM meta WHERE name=?", (_MIGRATE_FLAG,)).fetchone()
    except sqlite3.Error:
        return
    if done:
        return

    source = _find_legacy_db()
    if source is None:
        return

    try:
        old = sqlite3.connect(
            "file:{}?mode=ro".format(source.as_posix()), uri=True, timeout=5
        )
        old.row_factory = sqlite3.Row
    except sqlite3.Error as e:
        _log_warn(f"guanli 打开旧数据库 {source.name} 失败（已跳过搬迁）：{e!r}")
        return

    moved: List[str] = []
    group_count = 0
    try:
        tables = _legacy_tables(old)

        # 先在内存里把「群 -> 设置」拼好，最后一次性写库
        groups: Dict[str, Dict[str, Any]] = {}

        def _slot(gid: Any) -> Dict[str, Any]:
            return groups.setdefault(str(gid), {"watch_image": None, "keywords": None})

        if "media_monitor_status" in tables:
            try:
                for r in old.execute("SELECT group_id, enabled FROM media_monitor_status"):
                    _slot(r["group_id"])["watch_image"] = 1 if r["enabled"] else 0
                moved.append("media_monitor_status")
            except sqlite3.Error as e:
                _log_warn(f"guanli 搬迁 media_monitor_status 失败（已跳过）：{e!r}")

        if "group_configs" in tables:
            try:
                for r in old.execute("SELECT group_id, custom_keywords FROM group_configs"):
                    _slot(r["group_id"])["keywords"] = _parse_keywords(r["custom_keywords"])
                moved.append("group_configs")
            except sqlite3.Error as e:
                _log_warn(f"guanli 搬迁 group_configs 失败（已跳过）：{e!r}")

        if groups:
            with conn:
                for gid, item in groups.items():
                    row = conn.execute(
                        "SELECT watch_image, keywords FROM group_settings WHERE group_id=?",
                        (gid,),
                    ).fetchone()
                    if row:
                        watch = (
                            int(row["watch_image"])
                            if item["watch_image"] is None
                            else int(item["watch_image"])
                        )
                        kws = list(_loads(row["keywords"], []))
                        if item["keywords"]:
                            kws = list(dict.fromkeys(kws + list(item["keywords"])))
                    else:
                        watch = 1 if item["watch_image"] is None else int(item["watch_image"])
                        kws = list(item["keywords"] or [])
                    conn.execute(
                        """
                        INSERT INTO group_settings(group_id, watch_image, keywords, plugins, updated_at)
                        VALUES(?, ?, ?, '{}', ?)
                        ON CONFLICT(group_id) DO UPDATE SET
                            watch_image = excluded.watch_image,
                            keywords    = excluded.keywords,
                            updated_at  = excluded.updated_at
                        """,
                        (gid, watch, json.dumps(kws, ensure_ascii=False), _now()),
                    )
            group_count = len(groups)

        if "violations" in tables:
            try:
                rows = list(old.execute("SELECT group_id, user_id, count FROM violations"))
                with conn:
                    for r in rows:
                        conn.execute(
                            """
                            INSERT INTO warns(group_id, user_id, count, updated_at)
                            VALUES(?, ?, ?, ?)
                            ON CONFLICT(group_id, user_id) DO UPDATE SET
                                count = excluded.count, updated_at = excluded.updated_at
                            """,
                            (str(r["group_id"]), str(r["user_id"]), int(r["count"] or 0), _now()),
                        )
                moved.append("violations")
            except sqlite3.Error as e:
                _log_warn(f"guanli 搬迁 violations 失败（已跳过）：{e!r}")

        if "group_keys" in tables:
            try:
                rows = list(old.execute("SELECT group_id, secret_key FROM group_keys"))
                with conn:
                    for r in rows:
                        if not r["secret_key"]:
                            continue
                        conn.execute(
                            """
                            INSERT INTO secrets(name, value, updated_at) VALUES(?, ?, ?)
                            ON CONFLICT(name) DO UPDATE SET
                                value = excluded.value, updated_at = excluded.updated_at
                            """,
                            (f"group_key:{r['group_id']}", str(r["secret_key"]), _now()),
                        )
                moved.append("group_keys")
            except sqlite3.Error as e:
                _log_warn(f"guanli 搬迁 group_keys 失败（已跳过）：{e!r}")
    finally:
        try:
            old.close()
        except sqlite3.Error:
            pass

    # 无论有没有搬到东西都记标记：旧库坏了也不要每次启动重试
    try:
        with conn:
            conn.execute(
                "INSERT INTO meta(name, value) VALUES(?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (
                    _MIGRATE_FLAG,
                    "{} {} @ {}".format(source.name, ",".join(moved) or "empty", _now()),
                ),
            )
    except sqlite3.Error:
        pass

    if moved:
        _log_warn(
            "guanli 已从旧数据库 {} 搬迁：{}（{} 个群设置）".format(
                source.name, "、".join(moved), group_count
            )
        )


# ------------------------------------------------------------------ 发言统计
def add_message(group_id: Any, user_id: Any) -> int:
    """发言计数 +1，返回该成员在本群的累计条数（本地统计，两端通用）"""
    with _write_lock:
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO message_stats(group_id, user_id, count, last_at)
                    VALUES(?, ?, 1, ?)
                    ON CONFLICT(group_id, user_id)
                    DO UPDATE SET count = count + 1, last_at = excluded.last_at
                    """,
                    (str(group_id), str(user_id), _now()),
                )
            cur = conn.execute(
                "SELECT count FROM message_stats WHERE group_id=? AND user_id=?",
                (str(group_id), str(user_id)),
            )
            row = cur.fetchone()
            return int(row["count"]) if row else 0
        finally:
            conn.close()


def list_stats(group_id: Optional[Any] = None, limit: int = 100) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        if group_id is None:
            cur = conn.execute(
                "SELECT * FROM message_stats ORDER BY count DESC, last_at DESC LIMIT ?",
                (int(limit),),
            )
        else:
            cur = conn.execute(
                """
                SELECT * FROM message_stats WHERE group_id=?
                ORDER BY count DESC, last_at DESC LIMIT ?
                """,
                (str(group_id), int(limit)),
            )
        return _rows(cur)
    finally:
        conn.close()


def clear_stats(group_id: Optional[Any] = None) -> int:
    with _write_lock:
        conn = _connect()
        try:
            with conn:
                if group_id is None:
                    cur = conn.execute("DELETE FROM message_stats")
                else:
                    cur = conn.execute(
                        "DELETE FROM message_stats WHERE group_id=?", (str(group_id),)
                    )
            return cur.rowcount or 0
        finally:
            conn.close()


def rank_of(group_id: Any, user_id: Any, limit: int = 200) -> Optional[int]:
    """某成员在本群的发言排名（1 起），不在榜内返回 None"""
    for i, row in enumerate(list_stats(group_id, limit), start=1):
        if str(row["user_id"]) == str(user_id):
            return i
    return None


# ------------------------------------------------------------------ 黑白名单
def add_to_list(
    group_id: Any, user_id: Any, kind: str = "black", note: str = ""
) -> Dict[str, Any]:
    """把成员加入本群名单：kind=black 拦截 / kind=white 豁免"""
    kind = "white" if str(kind) == "white" else "black"
    with _write_lock:
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO lists(group_id, user_id, kind, note, created_at)
                    VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(group_id, user_id, kind)
                    DO UPDATE SET note = excluded.note
                    """,
                    (str(group_id), str(user_id), kind, str(note), _now()),
                )
        finally:
            conn.close()
    return {"group_id": str(group_id), "user_id": str(user_id), "kind": kind, "note": str(note)}


def remove_from_list(group_id: Any, user_id: Any, kind: Optional[str] = None) -> int:
    with _write_lock:
        conn = _connect()
        try:
            with conn:
                if kind:
                    cur = conn.execute(
                        "DELETE FROM lists WHERE group_id=? AND user_id=? AND kind=?",
                        (str(group_id), str(user_id), str(kind)),
                    )
                else:
                    cur = conn.execute(
                        "DELETE FROM lists WHERE group_id=? AND user_id=?",
                        (str(group_id), str(user_id)),
                    )
            return cur.rowcount or 0
        finally:
            conn.close()


def list_entries(
    group_id: Optional[Any] = None, kind: Optional[str] = None
) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        sql = "SELECT * FROM lists"
        where: List[str] = []
        params: List[Any] = []
        if group_id is not None:
            where.append("group_id=?")
            params.append(str(group_id))
        if kind:
            where.append("kind=?")
            params.append(str(kind))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC"
        return _rows(conn.execute(sql, tuple(params)))
    finally:
        conn.close()


def list_members(group_id: Any, kind: str) -> Set[str]:
    """取某群名单成员集合（消息热路径上调用，尽量轻）"""
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT user_id FROM lists WHERE group_id=? AND kind=?",
            (str(group_id), str(kind)),
        )
        return {str(r["user_id"]) for r in cur.fetchall()}
    finally:
        conn.close()


# ------------------------------------------------------------------ 关键词自动回复
def add_autoreply(
    group_id: Any, pattern: str, reply: str, mode: str = "contains"
) -> Dict[str, Any]:
    """新增/覆盖一条自动回复；group_id 传空表示全局规则"""
    pattern = str(pattern or "").strip()
    if not pattern:
        raise ValueError("匹配词不能为空")
    mode = mode if mode in ("contains", "exact", "prefix") else "contains"
    gid = "" if group_id in (None, "", "global", "全局") else str(group_id)
    with _write_lock:
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO autoreplies(group_id, pattern, reply, mode, enabled, created_at)
                    VALUES(?, ?, ?, ?, 1, ?)
                    ON CONFLICT(group_id, pattern) DO UPDATE SET
                        reply = excluded.reply, mode = excluded.mode, enabled = 1
                    """,
                    (gid, pattern, str(reply), mode, _now()),
                )
            cur = conn.execute(
                "SELECT * FROM autoreplies WHERE group_id=? AND pattern=?", (gid, pattern)
            )
            row = cur.fetchone()
            return dict(row) if row else {}
        finally:
            conn.close()


def remove_autoreply(reply_id: Any) -> int:
    with _write_lock:
        conn = _connect()
        try:
            with conn:
                cur = conn.execute("DELETE FROM autoreplies WHERE id=?", (int(reply_id),))
            return cur.rowcount or 0
        finally:
            conn.close()


def toggle_autoreply(reply_id: Any, enabled: bool) -> bool:
    with _write_lock:
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE autoreplies SET enabled=? WHERE id=?",
                    (1 if enabled else 0, int(reply_id)),
                )
        finally:
            conn.close()
    return bool(enabled)


def list_autoreplies(group_id: Optional[Any] = None) -> List[Dict[str, Any]]:
    """列出自动回复；给了 group_id 时同时带上全局规则（本群优先）"""
    conn = _connect()
    try:
        if group_id is None:
            cur = conn.execute("SELECT * FROM autoreplies ORDER BY group_id, pattern")
        else:
            cur = conn.execute(
                """
                SELECT * FROM autoreplies WHERE group_id IN ('', ?)
                ORDER BY CASE WHEN group_id='' THEN 1 ELSE 0 END, pattern
                """,
                (str(group_id),),
            )
        rows = _rows(cur)
    finally:
        conn.close()
    for row in rows:
        row["enabled"] = int(row.get("enabled", 1))
    return rows


def match_autoreply(group_id: Any, text: str) -> Optional[Dict[str, Any]]:
    """按「精确 → 前缀 → 包含」优先级匹配自动回复，本群规则排全局规则前面"""
    text = str(text or "")
    if not text:
        return None
    rows = list_autoreplies(group_id)
    for mode in ("exact", "prefix", "contains"):
        for row in rows:
            if not row.get("enabled") or row.get("mode") != mode:
                continue
            pattern = str(row.get("pattern") or "")
            if not pattern:
                continue
            if mode == "exact" and text == pattern:
                return row
            if mode == "prefix" and text.startswith(pattern):
                return row
            if mode == "contains" and pattern in text:
                return row
    return None


# ------------------------------------------------------------------ 入群申请
def save_approval(
    flag: str,
    group_id: Any,
    user_id: Any,
    comment: str = "",
    sub_type: str = "add",
) -> None:
    """登记一条入群申请（同 flag 只登记一次，不覆盖已处理状态）"""
    with _write_lock:
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO approvals(flag, group_id, user_id, comment, sub_type, status, created_at)
                    VALUES(?, ?, ?, ?, ?, 'pending', ?)
                    ON CONFLICT(flag) DO UPDATE SET
                        comment = excluded.comment, user_id = excluded.user_id
                    """,
                    (
                        str(flag),
                        str(group_id),
                        str(user_id),
                        str(comment),
                        str(sub_type or "add"),
                        _now(),
                    ),
                )
        finally:
            conn.close()


def list_approvals(
    group_id: Optional[Any] = None, status: Optional[str] = None
) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        sql = "SELECT * FROM approvals"
        where: List[str] = []
        params: List[Any] = []
        if group_id is not None:
            where.append("group_id=?")
            params.append(str(group_id))
        if status:
            where.append("status=?")
            params.append(str(status))
        if where:
            sql += " WHERE " + " AND ".join(where)
        # 待处理置顶
        sql += " ORDER BY CASE WHEN status='pending' THEN 0 ELSE 1 END, created_at DESC"
        return _rows(conn.execute(sql, tuple(params)))
    finally:
        conn.close()


def get_approval(flag: str) -> Optional[Dict[str, Any]]:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM approvals WHERE flag=?", (str(flag),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_approval_status(flag: str, status: str) -> None:
    with _write_lock:
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE approvals SET status=?, handled_at=? WHERE flag=?",
                    (str(status), _now(), str(flag)),
                )
        finally:
            conn.close()


# ------------------------------------------------------------------ 审计日志
def add_audit(
    action: str,
    *,
    actor: str = "",
    group_id: Any = "",
    target: Any = "",
    detail: str = "",
    source: str = "qq",
) -> None:
    """记一条操作日志（谁 · 在哪个群 · 对谁 · 做了什么）"""
    with _write_lock:
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO audit_log(ts, source, actor, group_id, target, action, detail)
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _now(),
                        str(source),
                        str(actor),
                        str(group_id),
                        str(target),
                        str(action),
                        str(detail),
                    ),
                )
        finally:
            conn.close()


def list_audit(
    limit: int = 200, group_id: Optional[Any] = None
) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        if group_id is None:
            cur = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (int(limit),)
            )
        else:
            cur = conn.execute(
                "SELECT * FROM audit_log WHERE group_id=? ORDER BY id DESC LIMIT ?",
                (str(group_id), int(limit)),
            )
        return _rows(cur)
    finally:
        conn.close()


def clear_audit() -> int:
    with _write_lock:
        conn = _connect()
        try:
            with conn:
                cur = conn.execute("DELETE FROM audit_log")
            return cur.rowcount or 0
        finally:
            conn.close()


# ------------------------------------------------------------------ 面板概览统计
def summary() -> Dict[str, Any]:
    """面板首页用的计数（一次连接里把所有 COUNT 取完）"""
    conn = _connect()
    try:
        def _one(sql: str) -> int:
            row = conn.execute(sql).fetchone()
            return int(row[0]) if row and row[0] is not None else 0

        return {
            "groups": _one("SELECT COUNT(*) FROM group_settings"),
            "warns": _one("SELECT COUNT(*) FROM warns"),
            "stat_members": _one("SELECT COUNT(*) FROM message_stats"),
            "stat_total": _one("SELECT COALESCE(SUM(count), 0) FROM message_stats"),
            "blacklist": _one("SELECT COUNT(*) FROM lists WHERE kind='black'"),
            "whitelist": _one("SELECT COUNT(*) FROM lists WHERE kind='white'"),
            "autoreplies": _one("SELECT COUNT(*) FROM autoreplies"),
            "pending_approvals": _one("SELECT COUNT(*) FROM approvals WHERE status='pending'"),
            "audit": _one("SELECT COUNT(*) FROM audit_log"),
        }
    finally:
        conn.close()


# ------------------------------------------------------------------ 面板账号 / 会话
# 角色只分两档：admin 可读写，viewer 只读（写操作由 web 层中间件挡下）
ROLES: Tuple[str, ...] = ("admin", "viewer")
_ROLE_DESC: Dict[str, str] = {"admin": "管理员", "viewer": "只读"}

# scrypt 参数：N=2^14 约 16MB 内存，面板登录频率极低，够用且不拖慢
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1


def role_desc(role: Any) -> str:
    return _ROLE_DESC.get(str(role), str(role))


def _hash_password(password: str, salt: Optional[str] = None) -> Tuple[str, str]:
    """scrypt 派生口令哈希，返回 ``(hash_hex, salt_hex)``。

    scrypt 在内存受限的容器里可能抛 MemoryError，兜底退到 pbkdf2。
    """
    raw = str(password or "").encode("utf-8")
    salt_hex = salt or secrets.token_hex(16)
    salt_bytes = bytes.fromhex(salt_hex)
    try:
        dk = hashlib.scrypt(
            raw, salt=salt_bytes, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
        )
    except (ValueError, MemoryError):  # pragma: no cover - 取决于运行环境
        dk = hashlib.pbkdf2_hmac("sha256", raw, salt_bytes, 120_000, dklen=32)
    return dk.hex(), salt_hex


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    """校验口令（定长比较，避免时序侧信道）"""
    if not password_hash or not salt:
        return False
    try:
        calc, _ = _hash_password(password, salt)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(calc, str(password_hash))


# 端口直通把面板暴露在公网，口令必须挡得住常见的撞库字典
_WEAK_PASSWORDS = frozenset(
    {
        "123456", "1234567", "12345678", "123456789", "1234567890", "12345678910",
        "111111", "000000", "666666", "888888", "abcdef", "abc123", "abcd1234",
        "password", "password1", "passw0rd", "p@ssw0rd", "admin", "admin123",
        "admin888", "administrator", "root", "toor", "qwerty", "qwerty123",
        "qazwsx", "iloveyou", "letmein", "welcome", "monkey", "dragon",
        "cangyao", "guanli", "nonebot", "qqbot", "123qwe", "a123456",
    }
)


def password_min_length() -> int:
    """口令最小长度（``GUANLI_PASSWORD_MIN``，默认 12 位）"""
    from .config import env_password_min

    return env_password_min()


def password_policy_error(password: str, username: str = "") -> Optional[str]:
    """口令强度校验：通过返回 ``None``，不通过返回可直接展示给用户的原因"""
    text = str(password or "")
    if not text:
        return "口令不能为空"
    low = text.lower()
    if username and low == str(username).strip().lower():
        return "口令不能与用户名相同"
    if low in _WEAK_PASSWORDS:
        return "这个口令太常见了，请换一个"
    minimum = password_min_length()
    if len(text) < minimum:
        return f"口令至少 {minimum} 位（面板端口直通暴露公网，建议更长一些）"
    kinds = sum(
        (
            any(c.islower() for c in text),
            any(c.isupper() for c in text),
            any(c.isdigit() for c in text),
            any(not c.isalnum() for c in text),
        )
    )
    if kinds < 2:
        return "口令需包含小写字母 / 大写字母 / 数字 / 符号中的至少两类"
    if len(set(text)) < 4:
        return "口令重复字符太多，请换一个"
    return None


def _user_row(row: Any) -> Dict[str, Any]:
    """对外返回的用户结构：绝不含 hash / salt，只给「有没有口令」的标记"""
    data = dict(row)
    data["enabled"] = int(data.get("enabled", 1))
    data["has_password"] = bool(data.pop("password_hash", ""))
    data.pop("salt", None)
    return data


def count_users() -> int:
    conn = _connect()
    try:
        row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def count_admins(exclude: str = "") -> int:
    """启用的管理员数量（用于「不能删掉最后一个管理员」这类校验）"""
    conn = _connect()
    try:
        if exclude:
            row = conn.execute(
                "SELECT COUNT(*) FROM users WHERE role='admin' AND enabled=1 AND username<>?",
                (str(exclude),),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM users WHERE role='admin' AND enabled=1"
            ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def get_user(username: Any) -> Optional[Dict[str, Any]]:
    """按登录名取用户；返回结构不含口令哈希，内部校验请用 get_user_record"""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE username=?", (str(username),)).fetchone()
        return _user_row(row) if row else None
    finally:
        conn.close()


def _get_user_record(username: Any) -> Optional[Dict[str, Any]]:
    """含 password_hash / salt 的原始记录，仅供登录校验内部使用"""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE username=?", (str(username),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def authenticate(username: Any, password: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """账号口令校验，返回 ``(状态, 用户)``。

    状态取值：``ok`` 通过 / ``missing`` 用户不存在 / ``nopassword`` 该账号只能用 OIDC
    登录 / ``disabled`` 已停用 / ``bad`` 口令错误。调用方据此给出不同提示。
    """
    record = _get_user_record(username)
    if not record:
        return "missing", None
    if not record.get("password_hash"):
        return "nopassword", None
    if not int(record.get("enabled", 1)):
        return "disabled", None
    if not verify_password(
        password, str(record.get("password_hash") or ""), str(record.get("salt") or "")
    ):
        return "bad", None
    return "ok", _user_row(record)


def get_user_by_subject(provider: Any, subject: Any) -> Optional[Dict[str, Any]]:
    """按 OIDC 的 (provider, sub) 认人，用于二次登录时复用同一账号"""
    if not subject:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE provider=? AND subject=?",
            (str(provider or "oidc"), str(subject)),
        ).fetchone()
        return _user_row(row) if row else None
    finally:
        conn.close()


def list_users() -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT * FROM users ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, username"
        )
        return [_user_row(r) for r in cur.fetchall()]
    finally:
        conn.close()


def create_user(
    username: Any,
    password: Optional[str] = None,
    *,
    role: str = "admin",
    display_name: str = "",
    provider: str = "local",
    subject: str = "",
    enabled: bool = True,
    enforce_policy: bool = True,
) -> Dict[str, Any]:
    """新建账号（重名则报错）；provider=oidc 时可只留 subject 不给口令

    ``enforce_policy=False`` 只给「初始管理员」兜底用：即便管理员在 .env 里填了弱口令，
    也要把账号建出来，否则面板就没人能进了（弱口令警告由调用方打日志）。
    """
    name = str(username or "").strip()
    if not name:
        raise ValueError("用户名不能为空")
    if role not in ROLES:
        role = "admin"
    if password and enforce_policy:
        problem = password_policy_error(password, name)
        if problem:
            raise ValueError(problem)
    pw_hash, salt = _hash_password(password) if password else ("", "")
    with _write_lock:
        conn = _connect()
        try:
            exists = conn.execute("SELECT 1 FROM users WHERE username=?", (name,)).fetchone()
            if exists:
                raise ValueError(f"用户 {name} 已存在")
            with conn:
                conn.execute(
                    """
                    INSERT INTO users(
                        username, password_hash, salt, role, display_name,
                        provider, subject, enabled, created_at, last_login_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, '')
                    """,
                    (
                        name,
                        pw_hash,
                        salt,
                        role,
                        str(display_name or name),
                        str(provider or "local"),
                        str(subject or ""),
                        1 if enabled else 0,
                        _now(),
                    ),
                )
        finally:
            conn.close()
    created = get_user(name)
    return created or {}


def update_user(username: Any, **fields: Any) -> Optional[Dict[str, Any]]:
    """局部更新账号：role / enabled / display_name / subject"""
    name = str(username or "").strip()
    sets: List[str] = []
    params: List[Any] = []
    if "role" in fields and fields["role"] is not None:
        role = str(fields["role"])
        sets.append("role=?")
        params.append(role if role in ROLES else "admin")
    if "enabled" in fields and fields["enabled"] is not None:
        sets.append("enabled=?")
        params.append(1 if fields["enabled"] else 0)
    if "display_name" in fields and fields["display_name"] is not None:
        sets.append("display_name=?")
        params.append(str(fields["display_name"]))
    if "subject" in fields and fields["subject"] is not None:
        sets.append("subject=?")
        params.append(str(fields["subject"]))
    if not sets:
        return get_user(name)
    with _write_lock:
        conn = _connect()
        try:
            with conn:
                conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE username=?", (*params, name))
        finally:
            conn.close()
    return get_user(name)


def set_user_password(username: Any, password: str) -> bool:
    """改口令；password 传空表示改成「只能用 OIDC 登录」

    非空口令要先过强度校验，不达标抛 ``ValueError``（消息可直接展示给管理员）。
    """
    if password:
        problem = password_policy_error(password, str(username or ""))
        if problem:
            raise ValueError(problem)
    pw_hash, salt = _hash_password(password) if password else ("", "")
    with _write_lock:
        conn = _connect()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE users SET password_hash=?, salt=? WHERE username=?",
                    (pw_hash, salt, str(username)),
                )
            return bool(cur.rowcount)
        finally:
            conn.close()


def delete_user(username: Any) -> int:
    with _write_lock:
        conn = _connect()
        try:
            with conn:
                cur = conn.execute("DELETE FROM users WHERE username=?", (str(username),))
                conn.execute("DELETE FROM sessions WHERE username=?", (str(username),))
            return cur.rowcount or 0
        finally:
            conn.close()


def touch_user_login(username: Any) -> None:
    """记录最近登录时间"""
    with _write_lock:
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE users SET last_login_at=? WHERE username=?", (_now(), str(username))
                )
        finally:
            conn.close()


# ------------------------------------------------------------------ 面板会话
def create_session(
    username: Any,
    role: str,
    *,
    provider: str = "local",
    ttl: int = 7 * 24 * 3600,
    ip: str = "",
    ua: str = "",
) -> str:
    """签发一个会话 id（随机 32 字节），过期时间按 ttl 秒算"""
    sid = secrets.token_urlsafe(32)
    now = datetime.now()
    expires = datetime.fromtimestamp(now.timestamp() + max(60, int(ttl)))
    stamp = now.isoformat(timespec="seconds")
    with _write_lock:
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO sessions(sid, username, role, provider, ip, ua, created_at, seen_at, expires_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sid,
                        str(username),
                        str(role),
                        str(provider or "local"),
                        str(ip or "")[:64],
                        str(ua or "")[:200],
                        stamp,
                        stamp,
                        expires.isoformat(timespec="seconds"),
                    ),
                )
                # 顺手清掉过期会话，避免表无限增长
                conn.execute("DELETE FROM sessions WHERE expires_at<=?", (stamp,))
        finally:
            conn.close()
    return sid


def get_session(sid: Any) -> Optional[Dict[str, Any]]:
    """取会话；已过期则顺手删除并返回 None"""
    key = str(sid or "").strip()
    if not key:
        return None
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM sessions WHERE sid=?", (key,)).fetchone()
        if not row:
            return None
        data = dict(row)
        if str(data.get("expires_at") or "") <= _now():
            conn.close()
            delete_session(key)
            return None
        return data
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def touch_session(sid: Any) -> None:
    """刷新会话的活跃时间（读接口低频调用即可）"""
    with _write_lock:
        conn = _connect()
        try:
            with conn:
                conn.execute("UPDATE sessions SET seen_at=? WHERE sid=?", (_now(), str(sid)))
        finally:
            conn.close()


def delete_session(sid: Any) -> int:
    with _write_lock:
        conn = _connect()
        try:
            with conn:
                cur = conn.execute("DELETE FROM sessions WHERE sid=?", (str(sid),))
            return cur.rowcount or 0
        finally:
            conn.close()


def delete_user_sessions(username: Any) -> int:
    with _write_lock:
        conn = _connect()
        try:
            with conn:
                cur = conn.execute("DELETE FROM sessions WHERE username=?", (str(username),))
            return cur.rowcount or 0
        finally:
            conn.close()


def list_sessions() -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        cur = conn.execute("SELECT * FROM sessions ORDER BY seen_at DESC")
        return _rows(cur)
    finally:
        conn.close()


def prune_sessions() -> int:
    with _write_lock:
        conn = _connect()
        try:
            with conn:
                cur = conn.execute("DELETE FROM sessions WHERE expires_at<=?", (_now(),))
            return cur.rowcount or 0
        finally:
            conn.close()


# ------------------------------------------------------------------ 工具
def _loads(raw: Any, default: Any) -> Any:
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default
