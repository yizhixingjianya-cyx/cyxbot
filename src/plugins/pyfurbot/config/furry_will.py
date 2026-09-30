from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict
from nonebot import get_driver
import os
import yaml


class FurryWillConfig(BaseSettings):
    # 关联 .env 文件
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    furry_will_base_url: str = ""
    furry_will_jwt_secret: str = ""
    furry_will_jwt_qq: str = ""
    
    
# 从YAML文件加载配置
def load_yaml_config():
    yaml_config_path = os.path.join(os.path.dirname(__file__), '..', 'config.yaml')
    if os.path.exists(yaml_config_path):
        with open(yaml_config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    return {}


# 获取驱动配置
try:
    driver_config = get_driver().config
    config = FurryWillConfig.model_validate(dict(driver_config))
except ValueError:
    # NoneBot未初始化时使用默认配置
    config = FurryWillConfig()

# 从YAML文件加载配置
yaml_config = load_yaml_config()
furry_will_config = yaml_config.get('furry_will', {})

# Furry Will API配置（优先级：环境变量 > YAML配置 > 默认值）
FURRY_WILL_BASE_URL = config.furry_will_base_url or furry_will_config.get('base_url', 'https://mrjm.fur-bot.com')
FURRY_WILL_JWT_SECRET = config.furry_will_jwt_secret or furry_will_config.get('jwt_secret', '')
FURRY_WILL_JWT_QQ = config.furry_will_jwt_qq or str(furry_will_config.get('jwt_qq', ''))