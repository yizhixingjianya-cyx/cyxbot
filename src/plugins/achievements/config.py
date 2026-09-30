from pydantic import BaseModel
from typing import Optional

class Config(BaseModel):
    """成就插件配置"""
    # 是否启用成就通知
    achievements_notification: bool = True
    # 成就通知模板
    achievements_notification_template: str = "🎉 恭喜你获得成就：{name}\n📝 描述：{description}\n🏆 成就ID：{id}"
    # 数据自动保存间隔（分钟）
    achievements_auto_save_interval: int = 60
    # 是否允许重复注册相同ID的成就
    achievements_allow_duplicate_registration: bool = False
    # 是否启用成就图片
    achievements_image_enabled: bool = True
    # 成就图片背景颜色
    achievements_image_background: str = "#2c3e50"
    # 成就图片文字颜色
    achievements_image_text_color: str = "#ffffff"
    # 成就图片标题颜色
    achievements_image_title_color: str = "#f1c40f"
