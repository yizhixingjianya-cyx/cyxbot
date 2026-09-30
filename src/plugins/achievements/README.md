# 通用成就系统插件

## 插件介绍

这是一个基于NoneBot2框架开发的通用成就系统插件，支持其他插件注册和给予成就，同时包含签到和货币系统，为机器人提供完整的成就和经济管理功能。

## 功能特性

- ✅ 成就注册：支持其他插件注册自定义成就
- ✅ 成就给予：支持向用户给予成就，自动检查重复
- ✅ 成就查询：支持查询用户成就和所有成就
- ✅ 数据持久化：使用SQLite数据库存储数据
- ✅ 事件通知：用户获得成就时自动发送通知
- ✅ 成就图片：自动生成成就获得图片，支持自定义样式
- ✅ 命令支持：提供成就、签到、货币查询相关命令
- ✅ 隐藏成就：支持设置隐藏成就
- ✅ 签到系统：支持用户每日签到，获得货币奖励
- ✅ 连续签到：支持连续签到奖励和成就
- ✅ 货币系统：支持货币的增加、减少和查询
- ✅ 货币管理：提供货币相关API供其他插件使用

## 安装方法

1. 确保你的机器人已经安装了NoneBot2框架
2. 将本插件放入机器人的插件目录
3. 安装依赖：
   ```
   nonebot-plugin-apscheduler
   pillow
   ```

## 配置选项

在机器人的配置文件中添加以下配置项（可选）：

```python
# 是否启用成就通知
achievements_notification = True

# 成就通知模板
achievements_notification_template = "🎉 恭喜你获得成就：{name}\n📝 描述：{description}\n🏆 成就ID：{id}"

# 数据自动保存间隔（分钟）
achievements_auto_save_interval = 60

# 是否允许重复注册相同ID的成就
achievements_allow_duplicate_registration = False

# 是否启用成就图片
achievements_image_enabled = True

# 成就图片背景颜色
achievements_image_background = "#2c3e50"

# 成就图片文字颜色
achievements_image_text_color = "#ffffff"

# 成就图片标题颜色
achievements_image_title_color = "#f1c40f"
```

## API文档

### 1. 注册成就

```python
register_achievement(achievement_id: str, name: str, description: str, icon: str = None, hidden: bool = False) -> bool
```

**参数说明：**
- `achievement_id`：成就唯一标识符
- `name`：成就名称
- `description`：成就描述
- `icon`：成就图标（可选）
- `hidden`：是否隐藏成就（可选，默认False）

**返回值：**
- `True`：注册成功
- `False`：成就已存在

### 2. 给予成就

```python
grant_achievement(user_id: str, achievement_id: str) -> bool
```

**参数说明：**
- `user_id`：用户唯一标识符
- `achievement_id`：成就唯一标识符

**返回值：**
- `True`：给予成功
- `False`：已拥有或成就不存在

### 3. 检查成就

```python
has_achievement(user_id: str, achievement_id: str) -> bool
```

**参数说明：**
- `user_id`：用户唯一标识符
- `achievement_id`：成就唯一标识符

**返回值：**
- `True`：拥有该成就
- `False`：未拥有该成就

### 4. 获取用户成就

```python
get_user_achievements(user_id: str) -> List[Dict]
```

**参数说明：**
- `user_id`：用户唯一标识符

**返回值：**
- 用户成就列表

### 5. 获取所有成就

```python
get_all_achievements(show_hidden: bool = False) -> Dict[str, Dict]
```

**参数说明：**
- `show_hidden`：是否显示隐藏成就（可选，默认False）

**返回值：**
- 所有成就字典

### 6. 用户签到

```python
checkin(user_id: str) -> Tuple[bool, str, int, int]
```

**参数说明：**
- `user_id`：用户唯一标识符

**返回值：**
- `Tuple[bool, str, int, int]`：(是否签到成功, 签到消息, 获得货币, 连续签到天数)

### 7. 增加货币

```python
add_coins(user_id: str, amount: int) -> bool
```

**参数说明：**
- `user_id`：用户唯一标识符
- `amount`：增加的货币数量

**返回值：**
- `True`：操作成功
- `False`：参数错误

### 8. 减少货币

```python
reduce_coins(user_id: str, amount: int) -> bool
```

**参数说明：**
- `user_id`：用户唯一标识符
- `amount`：减少的货币数量

**返回值：**
- `True`：操作成功
- `False`：参数错误或货币不足

### 9. 获取货币

```python
get_coins(user_id: str) -> int
```

**参数说明：**
- `user_id`：用户唯一标识符

**返回值：**
- 用户货币数量

## 使用示例

### 其他插件如何使用本插件

```python
from nonebot import require

# 加载成就插件
achievements = require("achievements")

# 注册成就
achievements.register_achievement(
    "first_login",
    "首次登录",
    "完成首次登录",
    "🏆"
)

# 给予成就
await achievements.grant_achievement(user_id, "first_login")

# 检查用户是否拥有成就
if await achievements.has_achievement(user_id, "first_login"):
    print("用户已拥有该成就")

# 获取用户所有成就
user_achievements = await achievements.get_user_achievements(user_id)

# 获取所有成就
all_achievements = await achievements.get_all_achievements()

# 用户签到
await achievements.checkin(user_id)

# 增加用户货币
await achievements.add_coins(user_id, 100)

# 减少用户货币
await achievements.reduce_coins(user_id, 50)

# 获取用户货币
coins = await achievements.get_coins(user_id)
```

## 命令说明

### 1. 查询个人成就

**命令：** `成就` 或 `我的成就`

**功能：** 查询当前用户的所有成就

**示例：**
```
用户：成就
机器人：📋 你的成就列表 (2/10)
🏆 首次登录
   完成首次登录

🏆 活跃用户
   连续登录7天
```

### 2. 查询所有成就

**命令：** `成就列表` 或 `所有成就`

**功能：** 查询所有已注册的成就

**示例：**
```
用户：成就列表
机器人：📋 所有成就列表 (10)
🏆 首次登录
   完成首次登录

🏆 活跃用户
   连续登录7天

🏆 社交达人
   加入10个群组
```

### 3. 用户签到

**命令：** `签到`

**功能：** 每日签到获得货币奖励

**示例：**
```
用户：签到
机器人：🎉 签到成功！
💰 获得货币：12
🔥 连续签到：1 天
💎 当前余额：12
```

### 4. 查询货币

**命令：** `我的货币` 或 `货币` 或 `余额`

**功能：** 查询当前用户的货币余额

**示例：**
```
用户：我的货币
机器人：💎 你的货币余额：123
```

## 数据存储

插件使用SQLite数据库存储所有数据，数据库文件位于：

- `data/achievements.db`：包含所有成就、用户成就、用户信息、签到记录和货币数据

数据库会自动创建和管理，无需手动干预。

## 开发指南

### 插件结构

```
achievements/
├── __init__.py       # 插件主入口，包含核心API
├── config.py         # 插件配置定义
├── data/             # 数据存储目录
│   ├── achievements.json      # 成就定义数据
│   └── user_achievements.json # 用户成就数据
└── README.md         # 插件文档
```

### 核心模块

1. **数据管理**：负责成就数据的加载、保存和管理
2. **API接口**：提供成就注册、给予、查询等API
3. **命令处理**：处理用户的成就查询命令
4. **事件通知**：处理成就获得事件通知

## 注意事项

1. 成就ID必须唯一，重复注册会失败
2. 用户ID建议使用字符串格式，避免类型转换问题
3. 成就数据会自动保存，无需手动处理
4. 可以通过配置关闭成就通知
5. 隐藏成就不会在成就列表中显示

## 版本历史

- v1.0.0：初始版本，实现基本成就功能

## 贡献指南

欢迎提交Issue和Pull Request，共同完善这个插件！

## 许可证

MIT License
