"""
API 请求工具模块
提供统一的 HTTP 请求封装，支持同步和异步请求、自动重试、错误处理等
"""

import json
import logging
from typing import Dict, Any, List, Optional, Union
from dataclasses import dataclass

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

logger = logging.getLogger(__name__)


@dataclass
class APIResponse:
    """API响应数据类"""
    success: bool
    data: Any = None
    error: str = ""
    status_code: int = 0
    raw_response: str = ""


class BaseAPIClient:
    """API客户端基类"""

    def __init__(
        self,
        base_url: str = "",
        headers: Optional[Dict[str, str]] = None,
        timeout: int = 10,
        retries: int = 2
    ):
        self.base_url = base_url.rstrip('/')
        self.headers = headers or {}
        self.timeout = timeout
        self.retries = retries

    def _safe_json_parse(self, raw_data: str) -> Dict[str, Any]:
        """安全解析JSON字符串"""
        try:
            return {"success": True, "data": json.loads(raw_data)}
        except json.JSONDecodeError as e:
            logger.error(f"JSON解析失败: {str(e)} (位置: 行{e.lineno}, 列{e.colno})")
            return {
                "success": False,
                "error": f"JSON解析失败: {str(e)}",
                "raw_data": raw_data[:200]
            }

    def _validate_required_fields(
        self,
        data: Dict[str, Any],
        required_fields: List[str]
    ) -> Dict[str, Any]:
        """验证数据是否包含必填字段"""
        missing = [field for field in required_fields if field not in data]

        if missing:
            logger.warning(f"数据缺失必填字段: {', '.join(missing)}")

        return {
            "valid": len(missing) == 0,
            "missing_fields": missing,
            "error": f"缺失必填字段: {', '.join(missing)}" if missing else ""
        }

    def _extract_field(
        self,
        data: Dict[str, Any],
        field_path: List[str],
        default: Any = None
    ) -> Any:
        """安全提取嵌套字段"""
        current = data
        for field in field_path:
            if isinstance(current, dict) and field in current:
                current = current[field]
            else:
                return default
        return current

    def _build_url(self, endpoint: str) -> str:
        """构建完整URL"""
        endpoint = endpoint.lstrip('/')
        if self.base_url:
            return f"{self.base_url}/{endpoint}"
        return endpoint


class SyncAPIClient(BaseAPIClient):
    """同步API客户端"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not REQUESTS_AVAILABLE:
            raise ImportError("requests library is required for SyncAPIClient")

    def request(
        self,
        method: str,
        endpoint: str,
        **kwargs
    ) -> APIResponse:
        """
        发送同步HTTP请求

        Args:
            method: 请求方法 (GET, POST, etc.)
            endpoint: API端点
            **kwargs: 其他requests参数

        Returns:
            APIResponse对象
        """
        url = self._build_url(endpoint)
        headers = {**self.headers, **kwargs.pop('headers', {})}

        for attempt in range(self.retries + 1):
            try:
                response = requests.request(
                    method=method.upper(),
                    url=url,
                    headers=headers,
                    timeout=self.timeout,
                    **kwargs
                )

                # 尝试解析JSON
                try:
                    data = response.json()
                except json.JSONDecodeError:
                    data = None

                if response.status_code == 200:
                    return APIResponse(
                        success=True,
                        data=data,
                        status_code=response.status_code
                    )
                else:
                    error_msg = data.get('error', f'请求失败，状态码: {response.status_code}') if data else f'请求失败，状态码: {response.status_code}'
                    return APIResponse(
                        success=False,
                        error=error_msg,
                        status_code=response.status_code,
                        raw_response=response.text[:500]
                    )

            except requests.exceptions.Timeout:
                logger.warning(f"请求超时（第{attempt+1}次重试）: {url}")
                if attempt == self.retries:
                    return APIResponse(
                        success=False,
                        error=f"请求超时（超过{self.timeout}秒）"
                    )
            except requests.exceptions.ConnectionError as e:
                logger.warning(f"连接错误（第{attempt+1}次重试）: {e}")
                if attempt == self.retries:
                    return APIResponse(
                        success=False,
                        error=f"网络连接错误: {str(e)}"
                    )
            except Exception as e:
                logger.error(f"请求异常: {e}")
                return APIResponse(
                    success=False,
                    error=f"请求处理失败: {str(e)}"
                )

        return APIResponse(success=False, error="未知错误")

    def get(self, endpoint: str, params: Optional[Dict] = None, **kwargs) -> APIResponse:
        """发送GET请求"""
        return self.request("GET", endpoint, params=params, **kwargs)

    def post(self, endpoint: str, data: Optional[Dict] = None, json_data: Optional[Dict] = None, **kwargs) -> APIResponse:
        """发送POST请求"""
        if json_data:
            kwargs['json'] = json_data
        if data:
            kwargs['data'] = data
        return self.request("POST", endpoint, **kwargs)


class AsyncAPIClient(BaseAPIClient):
    """异步API客户端"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not HTTPX_AVAILABLE:
            raise ImportError("httpx library is required for AsyncAPIClient")

    async def request(
        self,
        method: str,
        endpoint: str,
        **kwargs
    ) -> APIResponse:
        """
        发送异步HTTP请求

        Args:
            method: 请求方法 (GET, POST, etc.)
            endpoint: API端点
            **kwargs: 其他httpx参数

        Returns:
            APIResponse对象
        """
        url = self._build_url(endpoint)
        headers = {**self.headers, **kwargs.pop('headers', {})}

        for attempt in range(self.retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.request(
                        method=method.upper(),
                        url=url,
                        headers=headers,
                        **kwargs
                    )

                    # 尝试解析JSON
                    try:
                        data = response.json()
                    except json.JSONDecodeError:
                        data = None

                    if response.status_code == 200:
                        return APIResponse(
                            success=True,
                            data=data,
                            status_code=response.status_code
                        )
                    else:
                        error_msg = data.get('error', f'请求失败，状态码: {response.status_code}') if data else f'请求失败，状态码: {response.status_code}'
                        return APIResponse(
                            success=False,
                            error=error_msg,
                            status_code=response.status_code,
                            raw_response=response.text[:500]
                        )

            except httpx.TimeoutException:
                logger.warning(f"请求超时（第{attempt+1}次重试）: {url}")
                if attempt == self.retries:
                    return APIResponse(
                        success=False,
                        error=f"请求超时（超过{self.timeout}秒）"
                    )
            except httpx.ConnectError as e:
                logger.warning(f"连接错误（第{attempt+1}次重试）: {e}")
                if attempt == self.retries:
                    return APIResponse(
                        success=False,
                        error=f"网络连接错误: {str(e)}"
                    )
            except Exception as e:
                logger.error(f"请求异常: {e}")
                return APIResponse(
                    success=False,
                    error=f"请求处理失败: {str(e)}"
                )

        return APIResponse(success=False, error="未知错误")

    async def get(self, endpoint: str, params: Optional[Dict] = None, **kwargs) -> APIResponse:
        """发送异步GET请求"""
        return await self.request("GET", endpoint, params=params, **kwargs)

    async def post(self, endpoint: str, data: Optional[Dict] = None, json_data: Optional[Dict] = None, **kwargs) -> APIResponse:
        """发送异步POST请求"""
        if json_data:
            kwargs['json'] = json_data
        if data:
            kwargs['data'] = data
        return await self.request("POST", endpoint, **kwargs)


class APIClientWithAuth(BaseAPIClient):
    """带认证的API客户端（支持同步和异步）"""

    def __init__(
        self,
        app_id: Optional[str] = None,
        app_key: Optional[str] = None,
        token: Optional[str] = None,
        token_header: str = "Authorization",
        token_prefix: str = "Bearer",
        **kwargs
    ):
        super().__init__(**kwargs)
        self.app_id = app_id
        self.app_key = app_key
        self.token = token
        self.token_header = token_header
        self.token_prefix = token_prefix

        # 设置认证头
        if token:
            self.headers[token_header] = f"{token_prefix} {token}" if token_prefix else token
        if app_id:
            self.headers["X-App-Id"] = app_id
        if app_key:
            self.headers["X-App-Key"] = app_key

    def update_token(self, token: str):
        """更新认证令牌"""
        self.token = token
        if token:
            self.headers[self.token_header] = f"{self.token_prefix} {token}" if self.token_prefix else token


def create_api_client(
    base_url: str = "",
    async_mode: bool = False,
    **kwargs
) -> Union[SyncAPIClient, AsyncAPIClient]:
    """
    创建API客户端的工厂函数

    Args:
        base_url: API基础URL
        async_mode: 是否使用异步模式
        **kwargs: 其他配置参数

    Returns:
        API客户端实例
    """
    if async_mode:
        return AsyncAPIClient(base_url=base_url, **kwargs)
    return SyncAPIClient(base_url=base_url, **kwargs)
