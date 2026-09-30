from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict
from nonebot import get_driver
import os
import yaml


class Config(BaseSettings):
    # 关联 .env 文件
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    appid: str = ""
    appkey: str = ""
    ftvbaseurl: str = ""
    
    
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
    config = Config.model_validate(dict(driver_config))
except ValueError:
    # NoneBot未初始化时使用默认配置
    config = Config()

# 从YAML文件加载配置
yaml_config = load_yaml_config()
api_config = yaml_config.get('api', {})

# 应用凭证（优先级：环境变量 > YAML配置 > 默认值）
APP_ID = config.appid or api_config.get('appid', '')
APP_KEY = config.appkey or api_config.get('appkey', '')
BASEURL = config.ftvbaseurl or api_config.get('baseurl', "https://api-vdp.hydun.com")

# 图片默认URL（当API返回的图片URL无效时使用）
images_config = yaml_config.get('images', {})
DEFAULT_VERTICAL_IMG = images_config.get('vertical', "https://tse1-mm.cn.bing.net/th/id/OIP-C.VeGlN8GP0jnVxlvUjszMEQHaO0?w=175&h=350&c=7&r=0&o=5&pid=1.7")  # 竖版背景图
DEFAULT_AVATAR_IMG = images_config.get('avatar', "https://vcos.vds.pub/static/furtv/default.jpg")      # 头像图
DEFAULT_HORIZONTAL_IMG = images_config.get('horizontal', "https://tse2-mm.cn.bing.net/th/id/OIP-C.iYmWWijzITKLV-GqJERBlgHaD1?w=203&h=105&c=7&r=0&o=5&pid=1.7")  # 横版展示图
DEFAULT_SHOWCASE_IMG = images_config.get('showcase', "https://tse2-mm.cn.bing.net/th/id/OIP-C.MHxLdjzqbkH5RmuQctlAGgHaHa?w=203&h=203&c=7&r=0&o=5&pid=1.7")



