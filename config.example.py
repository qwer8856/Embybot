# config.example.py
# 这是配置文件模板，请复制为 config.py 并填入真实信息

# xboard 面板的后端公开访问地址，用于生成订阅链接（订阅域名）
XBOARD_URL = "https://your-xboard-domain.com" 
XBOARD_DISPLAY_NAME = '你的xboard服务名称'  # Bot 中展示的 xboard / 机场服务名称

# xboard 数据库配置
# 支持 mysql / mariadb / sqlite / sqlite3
XBOARD_DB_TYPE = 'mysql'

# MySQL / MariaDB 配置，XBOARD_DB_TYPE = 'mysql' 时使用
XBOARD_DB_HOST = 'localhost'
XBOARD_DB_USER = 'your_db_user'
XBOARD_DB_PASSWORD = 'your_db_password'
XBOARD_DB_NAME = 'xboard'

# SQLite 配置，XBOARD_DB_TYPE = 'sqlite' 时使用
# 相对路径会按项目根目录解析，也可以填写绝对路径
XBOARD_SQLITE_PATH = '/root/Xboard/.docker/.data/database.sqlite'

# Emby 总显示名称，用于 Bot 菜单上的通用文案，例如“注册/绑定 Emby”
# 每个服务器自己的名称使用 EMBY_SERVERS 中的 display_name
EMBY_DISPLAY_NAME = 'Emby'

# Emby 服务器配置
# 1 个 Emby 服就填写 1 项；多个 Emby 服就继续往列表里添加。
EMBY_SERVERS = [
    {
        'key': 'emby_a',  # 唯一标识，只能包含字母/数字/_/-；配置后不要随意修改
        'display_name': 'Emby A服',  # 这个名称会显示在服务器选择、账号信息和通知里
        'emby_url': 'https://emby-a.example.com',  # Emby 链接，Bot 会用它调用 API
        'api': 'your_emby_a_api_key',  # Emby API Key
        'public_url': 'https://emby-a.example.com',  # 可选：发给用户看的地址；不填则默认使用 emby_url
        'port': '443',
    },
    # {
    #     'key': 'emby_b',
    #     'display_name': 'Emby B服',
    #     'emby_url': 'https://emby-b.example.com',
    #     'api': 'your_emby_b_api_key',
    #     'public_url': 'https://emby-b.example.com',
    #     'port': '443',
    # },
]

# 旧版单 Emby 配置（兼容用）
# 如果你已经填写 EMBY_SERVERS，可以不再填写下面这些旧配置。
# EMBY_SERVER_URL = 'https://your-emby-server.com'
# EMBY_PUBLIC_URL = 'https://your-emby-server.com'
# EMBY_PORT = '443'
# EMBY_API_KEY = 'your_emby_api_key'

# Telegram Bot 配置
TELEGRAM_BOT_TOKEN = 'your_bot_token_from_botfather'
ADMIN_TELEGRAM_ID = 123456789  # (可选) 管理员的 Telegram ID

# Telegram Bot 管理员的用户ID列表，可以添加多个
# 如何获取自己的ID: 在Telegram中搜索 @userinfobot，开始对话即可看到您的ID
ADMIN_USER_IDS = [
    123456789,  # 请将这里替换为您的真实Telegram User ID
    # 987654321,  # 如果有多个管理员，可以继续添加
]

# 其他配置
CHECK_INTERVAL_SECONDS = 300  # 检查订阅状态的时间间隔（秒）

# 机场订阅链接，支持添加多个
# 示例格式: https://your-xboard-domain.com/s/xxxxxxxxxxxxx
SUBSCRIPTION_URLS = [
    "https://your-xboard-domain.com/s/your_subscription_token",
    # 如果有更多订阅链接，请继续在此处添加
]

# 公告配置
ANNOUNCEMENT = {
    'enabled': False,  # 是否启用公告功能
    'show_in_start': False,  # 是否在 /start 中展示公告
    'show_button': False,  # 是否在主菜单中展示公告按钮
    'button_text': '📢 查看公告',
    'title': '📢 系统公告',
    'content': f'欢迎使用流影社 {EMBY_DISPLAY_NAME}！',
    'updated': '',
}
