# TelegramEmbyBot 部署文档

TelegramEmbyBot 用于联动 Xboard 订阅系统、Emby 媒体服务器和 Telegram Bot。

用户可以在 Telegram 中绑定 Xboard 账号、查看订阅、创建/查询/重置/删除 Emby 账号。后台任务会定时检查 Xboard 订阅状态，并自动启用或禁用对应的 Emby 用户。

本文档按生产部署来写，推荐项目目录为：

```bash
/root/Embybot
```

部署完成后，本文档也建议放在：

```bash
/root/Embybot/README.md
```

## 功能概览

- Telegram 用户绑定 Xboard 账号
- 查询套餐、到期时间、流量、余额和订阅链接
- 创建/绑定 Emby 账号
- Bot 创建的 Emby 账号默认限制播放转码、下载、共享设备、相机上传，并限制最大同时视频流为 1
- 查询 Emby 地址、端口、用户名和已保存密码
- 重置 Emby 密码
- 删除 Emby 账号
- 订阅失效后自动禁用 Emby 账号
- 订阅恢复后自动启用 Emby 账号
- 管理员更新机场 IP 列表
- 管理员查看 Bot 管理的 Emby 用户数
- 管理员删除全部 Bot 管理的 Emby 账号
- 管理员通过 Telegram 菜单触发 Bot 重启

## 目录结构

```text
/root/Embybot/
├── main.py                  # 主入口，启动 Bot 和后台同步任务
├── telegram_bot.py          # Telegram 命令、按钮、对话流程
├── xboard_api.py            # Xboard 数据库查询、绑定、订阅信息
├── emby_api.py              # Emby API 调用
├── subscription_parser.py   # 订阅节点解析，生成 IP 列表
├── config.example.py        # 配置模板
├── config.py                # 实际配置，包含敏感信息
├── requirements.txt         # Python 依赖
├── start_bot.sh             # 后台启动脚本
├── stop_bot.sh              # 停止脚本
├── restart_script.sh        # 重启脚本
├── emby_passwords.json      # 运行时生成，保存 Emby 用户名/密码映射
├── bot.log                  # 运行日志
└── restart.log              # 重启日志
```

## 一、服务器准备

推荐环境：

- Debian / Ubuntu Linux
- Python 3.10 或 Python 3.11
- 可以访问 Telegram API
- 可以访问 Emby API
- 可以访问 Xboard 数据库
- root 用户或有 sudo 权限的用户

安装基础组件：

```bash
apt update
apt install -y python3 python3-venv python3-pip curl vim unzip
```

检查 Python：

```bash
python3 --version
```

## 二、上传项目到 `/root/Embybot`

在服务器创建目录：

```bash
mkdir -p /root/Embybot
cd /root/Embybot
```

把项目文件上传到 `/root/Embybot`。上传后至少应看到这些文件：

```bash
ls -la /root/Embybot
```

应包含：

```text
main.py
telegram_bot.py
xboard_api.py
emby_api.py
subscription_parser.py
config.example.py
requirements.txt
start_bot.sh
stop_bot.sh
restart_script.sh
```

如果你在本地电脑上传，可以用 `scp`，示例：

```bash
scp -r TelegramEmbyBot/* root@你的服务器IP:/root/Embybot/
```

## 三、创建虚拟环境并安装依赖

进入项目目录：

```bash
cd /root/Embybot
```

创建虚拟环境：

```bash
python3 -m venv venv
```

激活虚拟环境：

```bash
source venv/bin/activate
```

安装依赖：

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

安装完成后检查：

```bash
python -c "import telegram, requests, pymysql, bcrypt; print('ok')"
```

看到 `ok` 即表示主要依赖可用。

## 四、配置 `config.py`

复制配置模板：

```bash
cd /root/Embybot
cp config.example.py config.py
```

编辑配置：

```bash
vim config.py
```

### 1. Xboard 基础配置

```python
XBOARD_URL = "https://your-xboard-domain.com"
XBOARD_DISPLAY_NAME = "你的机场名称"
```

`XBOARD_URL` 用于生成订阅链接。

`XBOARD_DISPLAY_NAME` 是 Telegram Bot 里展示给用户看的名称。

### 2. Xboard 数据库配置

本项目支持 MySQL / MariaDB / SQLite。

如果 Xboard 使用 MySQL 或 MariaDB：

```python
XBOARD_DB_TYPE = "mysql"
XBOARD_DB_HOST = "localhost"
XBOARD_DB_USER = "your_db_user"
XBOARD_DB_PASSWORD = "your_db_password"
XBOARD_DB_NAME = "xboard"
```

如果 Xboard 使用 SQLite：

```python
XBOARD_DB_TYPE = "sqlite"
XBOARD_SQLITE_PATH = "/root/Xboard/.docker/.data/database.sqlite"
```

SQLite 路径必须是真实存在的数据库文件。可以这样检查：

```bash
ls -lh /root/Xboard/.docker/.data/database.sqlite
```

如果路径写错，Bot 会无法读取 Xboard 用户和订阅信息。

### 3. Emby 配置

推荐只填写 `EMBY_SERVERS`。1 个 Emby 服就填写 1 项；多个 Emby 服就继续往列表里添加。

```python
# Bot 菜单和提示文案里展示的总名称；每台服务器自己的名称看下面的 display_name
EMBY_DISPLAY_NAME = "Emby"

EMBY_SERVERS = [
    {
        "key": "emby_a",
        "display_name": "Emby A服",
        "emby_url": "https://emby-a.example.com",
        "api": "your_emby_a_api_key",
        "public_url": "https://emby-a.example.com",
        "port": "443",
    },
    {
        "key": "emby_b",
        "display_name": "Emby B服",
        "emby_url": "https://emby-b.example.com",
        "api": "your_emby_b_api_key",
        "public_url": "https://emby-b.example.com",
        "port": "443",
    },
]
```

说明：

- `key` 是服务器唯一标识，只能包含字母、数字、下划线和中划线；配置后不要随意修改。
- `display_name` 是这台 Emby 服显示给用户看的名称。
- `emby_url` 是 Emby 链接，Bot 会用它调用 Emby API。
- `api` 是对应 Emby 后台创建的 API Key。
- `public_url` 是发给用户看的登录地址；不填时默认使用 `emby_url`。
- `port` 是发给用户看的端口。

如果只有一个 Emby 服，`EMBY_SERVERS` 保留一项即可。如果有多个，继续添加多项。用户点击“注册/绑定 Emby”时会通过 Telegram 按钮选择服务器，服务器按钮按每行两个显示；同一个 Xboard 账号可以分别在多个 Emby 服开户注册。后台同步会按服务器逐个启用或禁用账号。

Bot 创建或重新激活 Emby 账号时，会自动应用默认用户策略：

- 关闭“允许音频转码为兼容格式”
- 关闭“允许视频转码为兼容格式”
- 关闭“允许更改容器格式”
- 最大同时视频流设为 1
- 关闭“允许遥控共享设备”
- 关闭“允许下载媒体”
- 关闭“允许下载需要转码的媒体”
- 关闭“允许下载字幕”
- 关闭“允许相机上传”

旧版单 Emby 配置 `EMBY_SERVER_URL`、`EMBY_PUBLIC_URL`、`EMBY_PORT`、`EMBY_API_KEY` 只作为兼容保留。已经配置 `EMBY_SERVERS` 时，可以不再填写这些旧配置。

测试服务器能否访问 Emby：

```bash
curl -I "https://your-emby-server.com"
```

### 4. Telegram Bot 配置

```python
TELEGRAM_BOT_TOKEN = "your_bot_token_from_botfather"

ADMIN_USER_IDS = [
    123456789,
]
```

`TELEGRAM_BOT_TOKEN` 从 Telegram 的 `@BotFather` 获取。

`ADMIN_USER_IDS` 填 Telegram 用户 ID，不是用户名。可以用 `@userinfobot` 查看自己的 ID。

测试服务器能否访问 Telegram API：

```bash
curl -I https://api.telegram.org
```

测试 Bot Token：

```bash
curl "https://api.telegram.org/bot你的TOKEN/getMe"
```

不要把 Token 发给别人。

### 5. 后台同步间隔

```python
CHECK_INTERVAL_SECONDS = 300
```

表示每 300 秒检查一次 Xboard 订阅状态，并同步 Emby 用户启用/禁用状态。

### 6. 订阅链接列表

管理员菜单里的“更新机场IP列表”会用到：

```python
SUBSCRIPTION_URLS = [
    "https://your-xboard-domain.com/s/your_subscription_token",
]
```

如果暂时不用这个功能，可以先留空：

```python
SUBSCRIPTION_URLS = []
```

### 6.1 检测订阅节点落地 IP

如果你需要检测节点真正访问外网时的落地 IP，不能只解析订阅里的入口域名，必须实际连接节点后访问查 IP 接口。

先在 Linux 服务器安装 `sing-box`，确认可用：

```bash
sing-box version
curl --version
```

然后在 `config.py` 中配置：

```python
SUBSCRIPTION_URLS = [
    "https://your-xboard-domain.com/s/your_subscription_token",
]
```

再直接运行：

```bash
python3 detect_exit_ips.py
```

脚本会自动解析订阅中的 `ss://`、`vmess://`、`vless://` 节点，逐个启动临时 `sing-box` 配置检测落地 IP，并输出：

```text
exit_ips.txt
exit_ip_results.json
```

常用参数：

```bash
# 只测试前 3 个节点
python3 detect_exit_ips.py --limit 3

# 每个节点最多检测 20 秒
python3 detect_exit_ips.py --timeout 20

# 使用自己的查 IP 接口
python3 detect_exit_ips.py --ip-url "https://api.ipify.org"

# 临时指定订阅链接，不读取 config.py
python3 detect_exit_ips.py "https://your-xboard-domain.com/s/your_subscription_token"
```

注意：订阅链接包含 token，测试后建议在面板重置订阅链接。落地 IP 也可能因为机场负载均衡而变化。

### 7. 公告配置

```python
ANNOUNCEMENT = {
    "enabled": False,
    "show_in_start": False,
    "show_button": False,
    "button_text": "📢 查看公告",
    "title": "📢 系统公告",
    "content": f"欢迎使用 {EMBY_DISPLAY_NAME}！",
    "updated": "",
}
```

修改 `config.py` 后，需要重启 Bot 才会生效。

## 五、初始化运行数据文件

`emby_passwords.json` 用于保存 Xboard 邮箱、Telegram ID、Emby 用户名和密码之间的映射。

首次部署可以创建一个空 JSON 文件：

```bash
cd /root/Embybot
echo '{}' > emby_passwords.json
chmod 600 emby_passwords.json
```

如果这个文件为空或损坏，日志可能出现：

```text
无法读取密码存储文件，将重新初始化: Expecting value: line 1 column 1 (char 0)
```

这表示 `emby_passwords.json` 不是合法 JSON。修复方式：

```bash
cp emby_passwords.json emby_passwords.json.bak
echo '{}' > emby_passwords.json
chmod 600 emby_passwords.json
```

注意：初始化会让 Bot 忘记以前保存的 Emby 用户名和密码映射。生产环境请先备份。

## 六、设置脚本权限

```bash
cd /root/Embybot
chmod +x start_bot.sh stop_bot.sh restart_script.sh
chmod 600 config.py
```

如果 `emby_passwords.json` 已存在：

```bash
chmod 600 emby_passwords.json
```

## 七、前台测试运行

首次部署建议先前台运行，方便直接看到错误。

```bash
cd /root/Embybot
source venv/bin/activate
python3 main.py
```

看到类似日志表示 Bot 已启动：

```text
Telegram Bot 已启动并开始轮询。
```

然后到 Telegram 里给 Bot 发送：

```text
/start
```

如果能弹出菜单，说明 Telegram 基础通信正常。

停止前台运行：

```text
Ctrl+C
```

## 八、后台运行

确认前台运行没问题后，使用脚本后台启动。

```bash
cd /root/Embybot
./start_bot.sh
```

查看日志：

```bash
tail -f bot.log
```

停止：

```bash
./stop_bot.sh
```

重启：

```bash
./restart_script.sh
```

检查进程：

```bash
pgrep -af "python.*main.py"
```

或：

```bash
ps -p $(cat bot.pid) -o pid,%cpu,%mem,cmd
```

## 九、管理员菜单重启

管理员在 Telegram 里可以使用：

```text
/admin
```

然后点击“重启机器人”。

这个功能依赖：

```bash
/root/Embybot/restart_script.sh
```

所以请确保：

```bash
cd /root/Embybot
chmod +x restart_script.sh
```

如果点击重启失败，查看：

```bash
tail -n 100 bot.log
tail -n 100 restart.log
```

## 十、可选：开机自启

如果你希望服务器重启后自动启动 Bot，可以添加 systemd 服务。

创建服务文件：

```bash
vim /etc/systemd/system/embybot.service
```

写入：

```ini
[Unit]
Description=TelegramEmbyBot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/root/Embybot
ExecStart=/root/Embybot/venv/bin/python3 /root/Embybot/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启用并启动：

```bash
systemctl daemon-reload
systemctl enable embybot
systemctl start embybot
```

查看状态：

```bash
systemctl status embybot --no-pager
```

查看日志：

```bash
journalctl -u embybot -f
```

如果使用 systemd 管理进程，建议不要再同时使用 `start_bot.sh` 启动，避免重复运行两个 Bot。

## 十一、常用命令

进入项目：

```bash
cd /root/Embybot
```

启动：

```bash
./start_bot.sh
```

停止：

```bash
./stop_bot.sh
```

重启：

```bash
./restart_script.sh
```

实时日志：

```bash
tail -f bot.log
```

最近 100 行日志：

```bash
tail -n 100 bot.log
```

搜索错误：

```bash
grep -i "error\|exception\|失败" bot.log
```

检查 JSON 文件：

```bash
python3 -m json.tool emby_passwords.json
```

## 十二、Telegram 命令

普通用户：

| 命令 | 说明 |
| --- | --- |
| `/start` | 打开主菜单 |
| `/bind` | 绑定 Xboard 账号 |
| `/unbind` | 解绑当前账号 |
| `/status` | 查看绑定状态 |
| `/help` | 查看帮助 |

管理员：

| 命令 | 说明 |
| --- | --- |
| `/admin` | 打开管理员菜单 |

主菜单功能：

- 我的订阅
- 查询 Emby 账号
- 注册/绑定 Emby
- 重置 Emby 密码
- 查看服务器地址
- 删除账号
- 查看公告
- 管理员功能

通用菜单名称由 `config.py` 中的 `XBOARD_DISPLAY_NAME` 和 `EMBY_DISPLAY_NAME` 决定；每个 Emby 服自己的名称由 `EMBY_SERVERS` 里的 `display_name` 决定。

管理员菜单中的“删除全部 Bot Emby 账号”只会删除 `emby_passwords.json` 中记录过的 Bot 管理账号，不会删除 Emby 服务器中未被 Bot 记录的其他用户。执行前会显示待删除数量并要求二次确认。

## 十三、常见问题

### 1. Bot 没反应

检查进程：

```bash
pgrep -af "python.*main.py"
```

检查日志：

```bash
tail -n 100 /root/Embybot/bot.log
```

测试 Telegram API：

```bash
curl -I https://api.telegram.org
```

如果服务器无法连接 Telegram API，需要配置服务器网络或代理。

### 2. 绑定 Xboard 账号失败

检查：

- 邮箱是否存在于 Xboard
- 密码是否正确
- `XBOARD_DB_TYPE` 是否正确
- MySQL / MariaDB 连接信息是否正确
- SQLite 路径是否正确

SQLite 常用检查：

```bash
ls -lh /root/Xboard/.docker/.data/database.sqlite
```

### 3. 创建 Emby 账号失败

检查：

- 用户是否有有效订阅
- `EMBY_SERVERS` 中对应服务器的 `emby_url` 是否能访问
- `EMBY_SERVERS` 中对应服务器的 `api` 是否正确
- Emby API 是否允许创建用户

查看日志：

```bash
tail -n 100 /root/Embybot/bot.log
```

### 4. 提示账号已存在，但实际没有账号

通常是 `emby_passwords.json` 中有旧记录或脏数据。

先备份：

```bash
cd /root/Embybot
cp emby_passwords.json emby_passwords.json.bak
```

检查 JSON 是否有效：

```bash
python3 -m json.tool emby_passwords.json
```

如果文件损坏，可以初始化：

```bash
echo '{}' > emby_passwords.json
chmod 600 emby_passwords.json
./restart_script.sh
```

### 5. 修改配置不生效

修改 `config.py` 后必须重启：

```bash
cd /root/Embybot
./restart_script.sh
```

### 6. 日志出现 Telegram 超时

类似：

```text
telegram.error.TimedOut
httpx.ConnectTimeout
httpx.RemoteProtocolError
```

这是服务器连接 Telegram API 不稳定。检查：

```bash
curl -I https://api.telegram.org
```

如果长期失败，需要调整服务器网络环境。

## 十四、备份建议

至少备份这两个文件：

```text
/root/Embybot/config.py
/root/Embybot/emby_passwords.json
```

推荐备份命令：

```bash
cd /root/Embybot
mkdir -p backup
cp config.py backup/config.py.$(date +%F-%H%M%S)
cp emby_passwords.json backup/emby_passwords.json.$(date +%F-%H%M%S)
```

恢复时先停止 Bot：

```bash
cd /root/Embybot
./stop_bot.sh
cp backup/你的备份文件 config.py
cp backup/你的备份文件 emby_passwords.json
./start_bot.sh
```

## 十五、安全建议

不要公开这些文件：

```text
config.py
emby_passwords.json
bot.log
restart.log
bot.pid
server_ips.txt
```

推荐权限：

```bash
cd /root/Embybot
chmod 600 config.py
chmod 600 emby_passwords.json
chmod 700 *.sh
```

如果使用 Git，建议 `.gitignore` 包含：

```gitignore
config.py
emby_passwords.json
bot.log
restart.log
bot.pid
server_ips.txt
nohup.out
venv/
__pycache__/
*.pyc
```

## 快速部署命令汇总

已经上传项目文件后，可以按下面流程执行：

```bash
cd /root/Embybot

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

cp config.example.py config.py
vim config.py

echo '{}' > emby_passwords.json
chmod 600 config.py emby_passwords.json
chmod +x start_bot.sh stop_bot.sh restart_script.sh

python3 main.py
```

前台测试正常后：

```bash
Ctrl+C
./start_bot.sh
tail -f bot.log
```
