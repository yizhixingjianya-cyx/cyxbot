"""
数据库工具模块
提供统一的数据库操作封装，支持SQLite等
"""

import sqlite3
import logging
from typing import Dict, List, Tuple, Optional, Any, Callable, Union
from pathlib import Path
from contextlib import contextmanager, asynccontextmanager
from dataclasses import dataclass

logger = logging.getLogger(__name__)

try:
    import aiosqlite
    AIOSQLITE_AVAILABLE = True
except ImportError:
    AIOSQLITE_AVAILABLE = False


@dataclass
class DBConfig:
    """数据库配置"""
    db_path: str
    check_same_thread: bool = False
    timeout: float = 5.0


class SQLiteDB:
    """SQLite数据库管理类"""

    def __init__(self, config: DBConfig):
        self.config = config
        self._connection: Optional[sqlite3.Connection] = None
        self._ensure_directory()

    def _ensure_directory(self):
        """确保数据库目录存在"""
        db_path = Path(self.config.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        """建立数据库连接"""
        if self._connection is None:
            self._connection = sqlite3.connect(
                self.config.db_path,
                check_same_thread=self.config.check_same_thread,
                timeout=self.config.timeout
            )
            # 启用外键支持
            self._connection.execute("PRAGMA foreign_keys = ON")
            # 设置行工厂为字典形式
            self._connection.row_factory = sqlite3.Row
        return self._connection

    def close(self):
        """关闭数据库连接"""
        if self._connection:
            self._connection.close()
            self._connection = None
            logger.info(f"数据库连接已关闭: {self.config.db_path}")

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self._connection.rollback()
        else:
            self._connection.commit()
        self.close()

    @contextmanager
    def cursor(self):
        """获取游标的上下文管理器"""
        conn = self.connect()
        cursor = conn.cursor()
        try:
            yield cursor
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            cursor.close()

    def execute(
        self,
        sql: str,
        params: Tuple = (),
        fetchone: bool = False,
        fetchall: bool = False
    ) -> Any:
        """
        执行SQL语句

        Args:
            sql: SQL语句
            params: 参数
            fetchone: 是否获取单条结果
            fetchall: 是否获取所有结果

        Returns:
            查询结果或None
        """
        with self.cursor() as cursor:
            cursor.execute(sql, params)

            if fetchone:
                row = cursor.fetchone()
                return dict(row) if row else None
            elif fetchall:
                rows = cursor.fetchall()
                return [dict(row) for row in rows]
            return None

    def executemany(self, sql: str, params_list: List[Tuple]):
        """批量执行SQL语句"""
        with self.cursor() as cursor:
            cursor.executemany(sql, params_list)

    def create_table(
        self,
        table_name: str,
        columns: Dict[str, str],
        primary_key: Optional[str] = None,
        foreign_keys: Optional[List[Tuple[str, str, str]]] = None
    ):
        """
        创建表

        Args:
            table_name: 表名
            columns: 列定义 {列名: 类型}
            primary_key: 主键列名
            foreign_keys: 外键定义 [(列名, 引用表, 引用列)]
        """
        column_defs = []
        for col_name, col_type in columns.items():
            if col_name == primary_key:
                column_defs.append(f"{col_name} {col_type} PRIMARY KEY")
            else:
                column_defs.append(f"{col_name} {col_type}")

        if foreign_keys:
            for col, ref_table, ref_col in foreign_keys:
                column_defs.append(f"FOREIGN KEY ({col}) REFERENCES {ref_table}({ref_col})")

        sql = f"CREATE TABLE IF NOT EXISTS {table_name} ({', '.join(column_defs)})"
        self.execute(sql)
        logger.info(f"表 {table_name} 创建成功")

    def create_index(self, index_name: str, table_name: str, columns: List[str], unique: bool = False):
        """创建索引"""
        unique_str = "UNIQUE" if unique else ""
        columns_str = ", ".join(columns)
        sql = f"CREATE {unique_str} INDEX IF NOT EXISTS {index_name} ON {table_name}({columns_str})"
        self.execute(sql)

    def insert(self, table_name: str, data: Dict[str, Any]) -> bool:
        """插入数据"""
        try:
            columns = ", ".join(data.keys())
            placeholders = ", ".join(["?"] * len(data))
            sql = f"INSERT INTO {table_name} ({columns}) VALUES ({placeholders})"
            self.execute(sql, tuple(data.values()))
            return True
        except Exception as e:
            logger.error(f"插入数据失败: {e}")
            return False

    def insert_or_replace(self, table_name: str, data: Dict[str, Any]) -> bool:
        """插入或替换数据"""
        try:
            columns = ", ".join(data.keys())
            placeholders = ", ".join(["?"] * len(data))
            sql = f"INSERT OR REPLACE INTO {table_name} ({columns}) VALUES ({placeholders})"
            self.execute(sql, tuple(data.values()))
            return True
        except Exception as e:
            logger.error(f"插入/替换数据失败: {e}")
            return False

    def update(
        self,
        table_name: str,
        data: Dict[str, Any],
        where: str,
        where_params: Tuple
    ) -> bool:
        """更新数据"""
        try:
            set_clause = ", ".join([f"{k} = ?" for k in data.keys()])
            sql = f"UPDATE {table_name} SET {set_clause} WHERE {where}"
            self.execute(sql, tuple(data.values()) + where_params)
            return True
        except Exception as e:
            logger.error(f"更新数据失败: {e}")
            return False

    def delete(self, table_name: str, where: str, where_params: Tuple) -> bool:
        """删除数据"""
        try:
            sql = f"DELETE FROM {table_name} WHERE {where}"
            self.execute(sql, where_params)
            return True
        except Exception as e:
            logger.error(f"删除数据失败: {e}")
            return False

    def select(
        self,
        table_name: str,
        columns: Optional[List[str]] = None,
        where: Optional[str] = None,
        where_params: Tuple = (),
        order_by: Optional[str] = None,
        limit: Optional[int] = None
    ) -> List[Dict]:
        """查询数据"""
        cols = ", ".join(columns) if columns else "*"
        sql = f"SELECT {cols} FROM {table_name}"

        if where:
            sql += f" WHERE {where}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit:
            sql += f" LIMIT {limit}"

        return self.execute(sql, where_params, fetchall=True) or []

    def select_one(
        self,
        table_name: str,
        columns: Optional[List[str]] = None,
        where: Optional[str] = None,
        where_params: Tuple = ()
    ) -> Optional[Dict]:
        """查询单条数据"""
        cols = ", ".join(columns) if columns else "*"
        sql = f"SELECT {cols} FROM {table_name}"

        if where:
            sql += f" WHERE {where}"

        return self.execute(sql, where_params, fetchone=True)

    def count(self, table_name: str, where: Optional[str] = None, where_params: Tuple = ()) -> int:
        """统计记录数"""
        sql = f"SELECT COUNT(*) as count FROM {table_name}"
        if where:
            sql += f" WHERE {where}"

        result = self.execute(sql, where_params, fetchone=True)
        return result["count"] if result else 0

    def table_exists(self, table_name: str) -> bool:
        """检查表是否存在"""
        sql = "SELECT name FROM sqlite_master WHERE type='table' AND name=?"
        result = self.execute(sql, (table_name,), fetchone=True)
        return result is not None


class SimpleKVStore:
    """简单的键值存储（基于SQLite）"""

    def __init__(self, db: SQLiteDB, table_name: str = "kv_store"):
        self.db = db
        self.table_name = table_name
        self._init_table()

    def _init_table(self):
        """初始化键值表"""
        self.db.create_table(
            self.table_name,
            {
                "key": "TEXT",
                "value": "TEXT",
                "updated_at": "REAL"
            },
            primary_key="key"
        )

    def set(self, key: str, value: str):
        """设置键值"""
        import time
        self.db.insert_or_replace(
            self.table_name,
            {"key": key, "value": value, "updated_at": time.time()}
        )

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """获取键值"""
        result = self.db.select_one(
            self.table_name,
            columns=["value"],
            where="key = ?",
            where_params=(key,)
        )
        return result["value"] if result else default

    def delete(self, key: str) -> bool:
        """删除键值"""
        return self.db.delete(self.table_name, "key = ?", (key,))

    def get_all(self) -> Dict[str, str]:
        """获取所有键值"""
        results = self.db.select(self.table_name, columns=["key", "value"])
        return {row["key"]: row["value"] for row in results}


def create_sqlite_db(
    db_path: Union[str, Path],
    check_same_thread: bool = False
) -> SQLiteDB:
    """
    创建SQLite数据库实例的工厂函数

    Args:
        db_path: 数据库文件路径
        check_same_thread: 是否检查同线程

    Returns:
        SQLiteDB实例
    """
    config = DBConfig(
        db_path=str(db_path),
        check_same_thread=check_same_thread
    )
    return SQLiteDB(config)


class AsyncSQLiteDB:
    """异步SQLite数据库管理类"""

    def __init__(self, config: DBConfig):
        self.config = config
        self._connection: Optional[aiosqlite.Connection] = None
        self._ensure_directory()

    def _ensure_directory(self):
        """确保数据库目录存在"""
        db_path = Path(self.config.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

    async def connect(self) -> aiosqlite.Connection:
        """建立数据库连接"""
        if self._connection is None:
            self._connection = await aiosqlite.connect(
                self.config.db_path,
                check_same_thread=self.config.check_same_thread,
                timeout=self.config.timeout
            )
            # 启用外键支持
            await self._connection.execute("PRAGMA foreign_keys = ON")
            # 设置行工厂为字典形式
            self._connection.row_factory = aiosqlite.Row
        return self._connection

    async def close(self):
        """关闭数据库连接"""
        if self._connection:
            await self._connection.close()
            self._connection = None
            logger.info(f"异步数据库连接已关闭: {self.config.db_path}")

    async def __aenter__(self):
        await self.connect()
        return self._connection

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._connection:
            if exc_type:
                await self._connection.rollback()
            else:
                await self._connection.commit()
            await self.close()

    @asynccontextmanager
    async def cursor(self):
        """获取游标的异步上下文管理器"""
        conn = await self.connect()
        cursor = await conn.cursor()
        try:
            yield cursor
            await conn.commit()
        except Exception as e:
            await conn.rollback()
            raise e
        finally:
            await cursor.close()

    async def execute(
        self,
        sql: str,
        params: Tuple = (),
        fetchone: bool = False,
        fetchall: bool = False
    ) -> Any:
        """
        执行SQL语句

        Args:
            sql: SQL语句
            params: 参数
            fetchone: 是否获取单条结果
            fetchall: 是否获取所有结果

        Returns:
            查询结果或None
        """
        async with self.cursor() as cursor:
            await cursor.execute(sql, params)

            if fetchone:
                row = await cursor.fetchone()
                return dict(row) if row else None
            elif fetchall:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
            return None

    async def executemany(self, sql: str, params_list: List[Tuple]):
        """批量执行SQL语句"""
        async with self.cursor() as cursor:
            await cursor.executemany(sql, params_list)

    async def create_table(
        self,
        table_name: str,
        columns: Dict[str, str],
        primary_key: Optional[str] = None,
        foreign_keys: Optional[List[Tuple[str, str, str]]] = None
    ):
        """
        创建表

        Args:
            table_name: 表名
            columns: 列定义 {列名: 类型}
            primary_key: 主键列名
            foreign_keys: 外键定义 [(列名, 引用表, 引用列)]
        """
        column_defs = []
        for col_name, col_type in columns.items():
            if col_name == primary_key:
                column_defs.append(f"{col_name} {col_type} PRIMARY KEY")
            else:
                column_defs.append(f"{col_name} {col_type}")

        if foreign_keys:
            for col, ref_table, ref_col in foreign_keys:
                column_defs.append(f"FOREIGN KEY ({col}) REFERENCES {ref_table}({ref_col})")

        sql = f"CREATE TABLE IF NOT EXISTS {table_name} ({', '.join(column_defs)})"
        await self.execute(sql)
        logger.info(f"表 {table_name} 创建成功")

    async def create_index(self, index_name: str, table_name: str, columns: List[str], unique: bool = False):
        """创建索引"""
        unique_str = "UNIQUE" if unique else ""
        columns_str = ", ".join(columns)
        sql = f"CREATE {unique_str} INDEX IF NOT EXISTS {index_name} ON {table_name}({columns_str})"
        await self.execute(sql)

    async def insert(self, table_name: str, data: Dict[str, Any]) -> bool:
        """插入数据"""
        try:
            columns = ", ".join(data.keys())
            placeholders = ", ".join(["?"] * len(data))
            sql = f"INSERT INTO {table_name} ({columns}) VALUES ({placeholders})"
            await self.execute(sql, tuple(data.values()))
            return True
        except Exception as e:
            logger.error(f"插入数据失败: {e}")
            return False

    async def insert_or_replace(self, table_name: str, data: Dict[str, Any]) -> bool:
        """插入或替换数据"""
        try:
            columns = ", ".join(data.keys())
            placeholders = ", ".join(["?"] * len(data))
            sql = f"INSERT OR REPLACE INTO {table_name} ({columns}) VALUES ({placeholders})"
            await self.execute(sql, tuple(data.values()))
            return True
        except Exception as e:
            logger.error(f"插入/替换数据失败: {e}")
            return False

    async def update(
        self,
        table_name: str,
        data: Dict[str, Any],
        where: str,
        where_params: Tuple
    ) -> bool:
        """更新数据"""
        try:
            set_clause = ", ".join([f"{k} = ?" for k in data.keys()])
            sql = f"UPDATE {table_name} SET {set_clause} WHERE {where}"
            await self.execute(sql, tuple(data.values()) + where_params)
            return True
        except Exception as e:
            logger.error(f"更新数据失败: {e}")
            return False

    async def delete(self, table_name: str, where: str, where_params: Tuple) -> bool:
        """删除数据"""
        try:
            sql = f"DELETE FROM {table_name} WHERE {where}"
            await self.execute(sql, where_params)
            return True
        except Exception as e:
            logger.error(f"删除数据失败: {e}")
            return False

    async def select(
        self,
        table_name: str,
        columns: Optional[List[str]] = None,
        where: Optional[str] = None,
        where_params: Tuple = (),
        order_by: Optional[str] = None,
        limit: Optional[int] = None
    ) -> List[Dict]:
        """查询数据"""
        cols = ", ".join(columns) if columns else "*"
        sql = f"SELECT {cols} FROM {table_name}"

        if where:
            sql += f" WHERE {where}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit:
            sql += f" LIMIT {limit}"

        return await self.execute(sql, where_params, fetchall=True) or []

    async def select_one(
        self,
        table_name: str,
        columns: Optional[List[str]] = None,
        where: Optional[str] = None,
        where_params: Tuple = ()
    ) -> Optional[Dict]:
        """查询单条数据"""
        cols = ", ".join(columns) if columns else "*"
        sql = f"SELECT {cols} FROM {table_name}"

        if where:
            sql += f" WHERE {where}"

        return await self.execute(sql, where_params, fetchone=True)

    async def count(self, table_name: str, where: Optional[str] = None, where_params: Tuple = ()) -> int:
        """统计记录数"""
        sql = f"SELECT COUNT(*) as count FROM {table_name}"
        if where:
            sql += f" WHERE {where}"

        result = await self.execute(sql, where_params, fetchone=True)
        return result["count"] if result else 0

    async def table_exists(self, table_name: str) -> bool:
        """检查表是否存在"""
        sql = "SELECT name FROM sqlite_master WHERE type='table' AND name=?"
        result = await self.execute(sql, (table_name,), fetchone=True)
        return result is not None


class AsyncSimpleKVStore:
    """异步的键值存储（基于SQLite）"""

    def __init__(self, db: AsyncSQLiteDB, table_name: str = "kv_store"):
        self.db = db
        self.table_name = table_name

    async def init_table(self):
        """初始化键值表"""
        await self.db.create_table(
            self.table_name,
            {
                "key": "TEXT",
                "value": "TEXT",
                "updated_at": "REAL"
            },
            primary_key="key"
        )

    async def set(self, key: str, value: str):
        """设置键值"""
        import time
        await self.db.insert_or_replace(
            self.table_name,
            {"key": key, "value": value, "updated_at": time.time()}
        )

    async def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """获取键值"""
        result = await self.db.select_one(
            self.table_name,
            columns=["value"],
            where="key = ?",
            where_params=(key,)
        )
        return result["value"] if result else default

    async def delete(self, key: str) -> bool:
        """删除键值"""
        return await self.db.delete(self.table_name, "key = ?", (key,))

    async def get_all(self) -> Dict[str, str]:
        """获取所有键值"""
        results = await self.db.select(self.table_name, columns=["key", "value"])
        return {row["key"]: row["value"] for row in results}


def create_async_sqlite_db(
    db_path: Union[str, Path],
    check_same_thread: bool = False
) -> AsyncSQLiteDB:
    """
    创建异步SQLite数据库实例的工厂函数

    Args:
        db_path: 数据库文件路径
        check_same_thread: 是否检查同线程

    Returns:
        AsyncSQLiteDB实例
    """
    if not AIOSQLITE_AVAILABLE:
        raise ImportError("aiosqlite library is required for AsyncSQLiteDB")
    config = DBConfig(
        db_path=str(db_path),
        check_same_thread=check_same_thread
    )
    return AsyncSQLiteDB(config)
