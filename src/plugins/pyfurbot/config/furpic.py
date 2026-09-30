from pydantic import BaseModel
from typing import Optional
from nonebot import get_driver
import os
import yaml


class Config(BaseModel):
    # API基础URL
    furry_api_base_url: str = "https://furry.axzt.top"
    
    # 默认参数设置
    furry_api_format: str = "json"
    furry_api_redirect: int = 1
    furry_api_mode: str = "large"  # 修改默认模式为large(横屏)
    
    # 指令设置
    furry_command: str = ".furry"
    
    # 请求设置
    furry_request_timeout: int = 30


# 从YAML文件加载配置
def load_yaml_config():
    yaml_config_path = os.path.join(os.path.dirname(__file__), '..', 'config.yaml')
    if os.path.exists(yaml_config_path):
        with open(yaml_config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    return {}


# 从NoneBot配置中获取配置
try:
    global_config = get_driver().config
    furry_config = Config.parse_obj(global_config.dict())
except ValueError:
    # NoneBot未初始化时使用默认配置
    furry_config = Config()

# 从YAML文件加载配置
yaml_config = load_yaml_config()
furry_api_config = yaml_config.get('furry_api', {})

# 更新配置值（优先级：环境变量 > YAML配置 > 默认值）
if hasattr(global_config, 'furry_api_base_url') and global_config.furry_api_base_url:
    furry_config.furry_api_base_url = global_config.furry_api_base_url
elif furry_api_config.get('base_url'):
    furry_config.furry_api_base_url = furry_api_config.get('base_url')

if hasattr(global_config, 'furry_api_format') and global_config.furry_api_format:
    furry_config.furry_api_format = global_config.furry_api_format
elif furry_api_config.get('format'):
    furry_config.furry_api_format = furry_api_config.get('format')

if hasattr(global_config, 'furry_api_redirect') and global_config.furry_api_redirect is not None:
    furry_config.furry_api_redirect = global_config.furry_api_redirect
elif furry_api_config.get('redirect') is not None:
    furry_config.furry_api_redirect = furry_api_config.get('redirect')

if hasattr(global_config, 'furry_api_mode') and global_config.furry_api_mode:
    furry_config.furry_api_mode = global_config.furry_api_mode
elif furry_api_config.get('mode'):
    furry_config.furry_api_mode = furry_api_config.get('mode')

if hasattr(global_config, 'furry_command') and global_config.furry_command:
    furry_config.furry_command = global_config.furry_command
elif furry_api_config.get('command'):
    furry_config.furry_command = furry_api_config.get('command')

if hasattr(global_config, 'furry_request_timeout') and global_config.furry_request_timeout:
    furry_config.furry_request_timeout = global_config.furry_request_timeout
elif furry_api_config.get('request_timeout'):
    furry_config.furry_request_timeout = furry_api_config.get('request_timeout')