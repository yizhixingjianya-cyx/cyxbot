"""
FurtvAPI Cache Manager
负责管理 API 响应和本地图片的缓存
"""
import json
from util.database import create_sqlite_db, SQLiteDB
import os
import hashlib
import aiofiles
from datetime import datetime, timedelta
from typing import Optional, Any, Dict
from pathlib import Path
from nonebot.log import logger
import aiohttp

# 缓存时间配置
CACHE_CONFIG = {
    'user': 3600,        # 用户相关 API 缓存 1 小时 (3600 秒)
    'gathering': 86400,  # 聚会相关 API 缓存 1 天 (86400 秒)
    'default': 1800      # 其他 API 缓存 30 分钟 (1800 秒)
}

# 端点类型映射
ENDPOINT_TYPES = {
    # 用户相关端点
    '/users/': 'user',
    '/characters/': 'user',
    '/relationships/': 'user',
    '/social-badges': 'user',
    '/store-products': 'user',
    '/like-status/': 'user',
    
    # 聚会相关端点
    '/gatherings/': 'gathering',
    
    # 学校相关
    '/schools/': 'default',
    
    # 搜索相关
    '/search': 'default',
    '/popular': 'default',
    '/random': 'default',
    '/species': 'default',
    '/locations/': 'default',
}


class CacheManager:
    """缓存管理器"""
    
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.db_path = str(cache_dir / "cache.db")
        self.image_cache_dir = cache_dir / "images"
        
        # 确保目录存在
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.image_cache_dir.mkdir(parents=True, exist_ok=True)
        
        # 创建数据库实例
        self.db: SQLiteDB = create_sqlite_db(self.db_path)
        
        # 初始化数据库
        self._init_db()
        
        logger.info(f"缓存管理器已初始化 | 目录：{cache_dir}")
    
    def _init_db(self):
        """初始化 SQLite 数据库"""
        # 创建缓存表
        self.db.execute('''
            CREATE TABLE IF NOT EXISTS api_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint TEXT NOT NULL,
                params_hash TEXT NOT NULL,
                response_data TEXT NOT NULL,
                cache_type TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(endpoint, params_hash)
            )
        ''')
        
        # 创建索引
        self.db.execute('CREATE INDEX IF NOT EXISTS idx_endpoint ON api_cache(endpoint)')
        self.db.execute('CREATE INDEX IF NOT EXISTS idx_expires ON api_cache(expires_at)')
        
        logger.debug("缓存数据库初始化完成")
    
    def _get_params_hash(self, params: Optional[Dict]) -> str:
        """生成参数的哈希值"""
        if not params:
            return hashlib.md5(b'').hexdigest()
        
        # 对参数排序后生成哈希
        params_str = json.dumps(params, sort_keys=True)
        return hashlib.md5(params_str.encode()).hexdigest()
    
    def _get_endpoint_type(self, endpoint: str) -> str:
        """根据端点判断缓存类型"""
        for path, cache_type in ENDPOINT_TYPES.items():
            if path in endpoint:
                return cache_type
        return 'default'
    
    def _get_cache_duration(self, endpoint: str) -> int:
        """获取端点的缓存时长（秒）"""
        cache_type = self._get_endpoint_type(endpoint)
        return CACHE_CONFIG.get(cache_type, CACHE_CONFIG['default'])
    
    async def get(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """从缓存获取数据"""
        params_hash = self._get_params_hash(params)
        
        try:
            row = self.db.execute('''
                SELECT response_data, expires_at 
                FROM api_cache 
                WHERE endpoint = ? AND params_hash = ? AND expires_at > ?
            ''', (endpoint, params_hash, datetime.now().isoformat()), fetchone=True)
            
            if row:
                response_data = json.loads(row['response_data'])
                expires_at = datetime.fromisoformat(row['expires_at'])
                logger.debug(f"缓存命中 | 端点：{endpoint} | 过期时间：{expires_at}")
                return response_data
            else:
                logger.debug(f"缓存未命中 | 端点：{endpoint}")
                return None
        except Exception as e:
            logger.error(f"读取缓存失败：{e}")
            return None
    
    async def set(self, endpoint: str, params: Optional[Dict], response_data: Dict):
        """设置缓存"""
        params_hash = self._get_params_hash(params)
        cache_duration = self._get_cache_duration(endpoint)
        expires_at = datetime.now() + timedelta(seconds=cache_duration)
        
        try:
            self.db.execute('''
                INSERT OR REPLACE INTO api_cache 
                (endpoint, params_hash, response_data, cache_type, expires_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (
                endpoint,
                params_hash,
                json.dumps(response_data, ensure_ascii=False),
                self._get_endpoint_type(endpoint),
                expires_at.isoformat()
            ))
            
            logger.debug(f"缓存已保存 | 端点：{endpoint} | 类型：{self._get_endpoint_type(endpoint)} | 缓存时长：{cache_duration}秒")
        except Exception as e:
            logger.error(f"保存缓存失败：{e}")
    
    async def invalidate(self, endpoint: str, params: Optional[Dict] = None):
        """使缓存失效"""
        try:
            if params:
                params_hash = self._get_params_hash(params)
                self.db.execute('''
                    DELETE FROM api_cache 
                    WHERE endpoint = ? AND params_hash = ?
                ''', (endpoint, params_hash))
            else:
                self.db.execute('''
                    DELETE FROM api_cache 
                    WHERE endpoint = ?
                ''', (endpoint,))
            
            logger.debug(f"缓存已清除 | 端点：{endpoint}")
        except Exception as e:
            logger.error(f"清除缓存失败：{e}")
    
    async def cleanup_expired(self):
        """清理过期缓存"""
        try:
            # 获取过期记录数
            count_result = self.db.execute('SELECT COUNT(*) FROM api_cache WHERE expires_at <= ?',
                                           (datetime.now().isoformat(),), fetchone=True)
            deleted_count = count_result['COUNT(*)'] if count_result else 0
            
            # 删除过期缓存
            self.db.execute('DELETE FROM api_cache WHERE expires_at <= ?',
                           (datetime.now().isoformat(),))
            
            if deleted_count > 0:
                logger.info(f"清理了 {deleted_count} 条过期缓存")
        except Exception as e:
            logger.error(f"清理过期缓存失败：{e}")
    
    async def download_and_cache_image(self, url: str, filename: Optional[str] = None) -> Optional[str]:
        """下载并缓存图片
        
        Args:
            url: 图片 URL
            filename: 可选的文件名，不提供则使用 URL 哈希
        
        Returns:
            本地文件路径，失败返回 None
        """
        try:
            # 生成文件名
            if not filename:
                filename = hashlib.md5(url.encode()).hexdigest() + '.jpg'
            
            file_path = self.image_cache_dir / filename
            
            # 如果文件已存在，直接返回
            if file_path.exists():
                logger.debug(f"图片已缓存 | {filename}")
                return str(file_path)
            
            # 下载图片
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        image_data = await response.read()
                        
                        # 保存文件
                        async with aiofiles.open(file_path, 'wb') as f:
                            await f.write(image_data)
                        
                        logger.debug(f"图片已缓存 | {filename} | 大小：{len(image_data)} 字节")
                        return str(file_path)
                    else:
                        logger.warning(f"图片下载失败 | {url} | 状态码：{response.status}")
                        return None
        except Exception as e:
            logger.error(f"下载图片失败：{e}")
            return None
    
    def get_cached_image(self, filename: str) -> Optional[str]:
        """获取已缓存的本地图片路径"""
        file_path = self.image_cache_dir / filename
        if file_path.exists():
            return str(file_path)
        return None
    
    async def get_stats(self) -> Dict:
        """获取缓存统计信息"""
        try:
            # 总缓存数
            total_row = self.db.execute('SELECT COUNT(*) FROM api_cache', fetchone=True)
            total_count = total_row['COUNT(*)'] if total_row else 0
            
            # 按类型统计
            type_rows = self.db.execute('''
                SELECT cache_type, COUNT(*) 
                FROM api_cache 
                GROUP BY cache_type
            ''', fetchall=True)
            type_stats = {row['cache_type']: row['COUNT(*)'] for row in type_rows} if type_rows else {}
            
            # 即将过期的缓存（5 分钟内）
            expiring_row = self.db.execute('''
                SELECT COUNT(*) 
                FROM api_cache 
                WHERE expires_at <= ?
            ''', (datetime.now() + timedelta(minutes=5)).isoformat(), fetchone=True)
            expiring_soon = expiring_row['COUNT(*)'] if expiring_row else 0
            
            return {
                'total': total_count,
                'by_type': type_stats,
                'expiring_soon': expiring_soon
            }
        except Exception as e:
            logger.error(f"获取缓存统计失败：{e}")
            return {}


# 创建全局缓存管理器实例（将在 __init__.py 中初始化）
cache_manager: Optional[CacheManager] = None


def get_cache_manager() -> Optional[CacheManager]:
    """获取缓存管理器实例"""
    global cache_manager
    if cache_manager is None:
        from nonebot.log import logger
        logger.debug("缓存管理器未初始化")
        return None
    return cache_manager


def is_cache_available() -> bool:
    """检查缓存是否可用"""
    return cache_manager is not None
