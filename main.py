# main.py
import asyncio
import json
import logging
import os
import sys
import config
from telegram import Bot
from telegram.ext import Application
from xboard_api import get_active_subscriptions, get_all_users
from emby_api import get_emby_users, disable_emby_user, enable_emby_user, apply_bot_default_user_policy, get_emby_servers
from telegram_bot import setup_bot, run_blocking, load_password_store, get_stored_emby_account_entries

CHECK_INTERVAL_SECONDS = getattr(config, 'CHECK_INTERVAL_SECONDS', 300)
TELEGRAM_BOT_TOKEN = getattr(config, 'TELEGRAM_BOT_TOKEN', '')
EMBY_DISPLAY_NAME = getattr(config, 'EMBY_DISPLAY_NAME', 'Emby')

# 密码存储文件路径（与 telegram_bot.py 共享）
PASSWORD_STORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'emby_passwords.json')

# --- PID 文件管理 ---
PID_FILE = "bot.pid"

def write_pid():
    """将当前进程ID写入PID文件"""
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

def remove_pid():
    """删除PID文件"""
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)

# 配置日志
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# 设置 telegram.ext 的日志级别为 WARNING，以减少不必要的日志输出
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

async def send_notification(bot: Bot, telegram_id: int, message: str, user_email: str):
    """安全地发送通知并记录日志"""
    if not telegram_id:
        return
    try:
        await bot.send_message(chat_id=telegram_id, text=message)
        logger.info(f"已向用户 {user_email} (TG ID: {telegram_id}) 发送通知。")
    except Exception as e:
        logger.error(f"向用户 {user_email} (TG ID: {telegram_id}) 发送通知失败: {e}")

async def sync_emby_users(application: Application):
    """
    同步 Emby 用户状态的异步任务，并在状态变更时发送通知。
    """
    bot = application.bot
    while True:
        logger.info("开始新一轮 %s 用户同步...", EMBY_DISPLAY_NAME)

        try:
            emby_servers = get_emby_servers()
            results = await asyncio.gather(
                run_blocking(get_all_users),
                run_blocking(get_active_subscriptions),
                *[run_blocking(get_emby_users, server['key']) for server in emby_servers],
            )
            all_xboard_users_list = results[0]
            active_xboard_users = results[1]
            emby_users_results = results[2:]

            if all_xboard_users_list is None:
                logger.error("获取 xboard 用户数据失败，跳过本轮同步。")
                await asyncio.sleep(CHECK_INTERVAL_SECONDS)
                continue

            if active_xboard_users is None:
                logger.error("获取有效订阅数据失败，跳过本轮同步。")
                await asyncio.sleep(CHECK_INTERVAL_SECONDS)
                continue

            # 将用户列表转换为以 email 为键的字典，方便快速查找
            all_xboard_users_map = {user['email']: user for user in all_xboard_users_list}
            
            active_xboard_emails = {user['email'] for user in active_xboard_users}

            # 构建每个 Emby 服的 用户名 -> 用户对象 映射（用户名统一转小写以便匹配）
            emby_usernames_maps = {}
            for server, emby_users in zip(emby_servers, emby_users_results):
                if emby_users is None:
                    logger.error("获取 %s 用户数据失败，本轮跳过该服务器。", server['display_name'])
                    continue

                emby_usernames_maps[server['key']] = {
                    user['Name'].lower(): user
                    for user in emby_users
                    if isinstance(user, dict) and user.get('Name')
                }

            if not emby_usernames_maps:
                logger.error("所有 Emby 服务器用户数据获取失败，跳过本轮同步。")
                await asyncio.sleep(CHECK_INTERVAL_SECONDS)
                continue

            # 加载密码存储文件，获取 email -> emby_username 的映射关系
            password_store = await run_blocking(load_password_store)

            logger.info(f"检测到 {len(active_xboard_emails)} 个有效 xboard 订阅。")
            total_emby_users = sum(len(user_map) for user_map in emby_usernames_maps.values())
            logger.info(f"开始检查 {total_emby_users} 个 {EMBY_DISPLAY_NAME} 用户状态。")

            # 遍历所有在 xboard 中存在的用户
            for email, xboard_user in all_xboard_users_map.items():
                telegram_id = xboard_user.get('telegram_id')
                account_entries = await run_blocking(
                    get_stored_emby_account_entries,
                    email,
                    telegram_id,
                    password_store,
                )

                for account_entry in account_entries:
                    server_key = account_entry['server_key']
                    emby_usernames_map = emby_usernames_maps.get(server_key)
                    if not emby_usernames_map:
                        continue

                    emby_username = account_entry['emby_username']
                    emby_user = emby_usernames_map.get(emby_username.strip().lower())
                    if not emby_user:
                        continue

                    server = next((item for item in emby_servers if item['key'] == server_key), None)
                    server_name = server['display_name'] if server else EMBY_DISPLAY_NAME
                    emby_user_id = emby_user['Id']
                    is_disabled = emby_user.get('Policy', {}).get('IsDisabled', False)

                    # 场景1: 订阅有效
                    if email in active_xboard_emails:
                        if is_disabled:
                            logger.info(f"用户 {email} 订阅已恢复，正在解封 {server_name} 账户...")
                            if await run_blocking(enable_emby_user, emby_user_id, server_key):
                                await run_blocking(apply_bot_default_user_policy, emby_user_id, server_key)
                                logger.info(f"成功解封 {server_name} 用户 {email}。")
                                await send_notification(
                                    bot, telegram_id, 
                                    f"您的 {server_name} 账户已重新激活，欢迎回来！", email
                                )
                            else:
                                logger.error(f"解封 {server_name} 用户 {email} 失败。")
                    
                    # 场景2: 订阅无效
                    else:
                        if not is_disabled:
                            logger.info(f"用户 {email} 订阅已失效，正在封禁 {server_name} 账户...")
                            if await run_blocking(disable_emby_user, emby_user_id, server_key):
                                logger.info(f"成功封禁 {server_name} 用户 {email}。")
                                await send_notification(
                                    bot, telegram_id,
                                    f"您的 {server_name} 账户因订阅失效或流量用尽已被暂停。续费后将自动激活。", email
                                )
                            else:
                                logger.error(f"封禁 {server_name} 用户 {email} 失败。")

        except Exception as e:
            logger.error(f"同步 {EMBY_DISPLAY_NAME} 用户时发生严重错误: {e}", exc_info=True)

        logger.info(f"本轮同步结束。等待 {CHECK_INTERVAL_SECONDS} 秒后进行下一次同步...")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def main() -> None:
    """设置并运行机器人和后台任务 (v20+ 最佳实践)。"""
    if not TELEGRAM_BOT_TOKEN:
        logger.error("错误：未在 config.py 中配置 TELEGRAM_BOT_TOKEN。")
        return

    # 写入PID文件
    write_pid()

    # 设置机器人
    application = setup_bot(TELEGRAM_BOT_TOKEN)

    # 使用 async with 语句来确保 application 被正确地启动和关闭
    async with application:
        # 启动后台同步任务
        sync_task = asyncio.create_task(sync_emby_users(application))
        logger.info("后台同步任务已创建。")

        # 启动 bot
        await application.start()
        await application.updater.start_polling()
        logger.info("Telegram Bot 已启动并开始轮询。")

        # 让主程序一直运行，直到被中断
        await asyncio.Future()

        # 当程序被中断时 (例如 Ctrl+C), async with 会自动处理 application.stop() 等清理工作
        sync_task.cancel()


if __name__ == "__main__":
    logger.info("启动 xboard-%s 联动服务...", EMBY_DISPLAY_NAME)
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("服务已成功停止。")
    except Exception as e:
        logger.critical(f"服务因意外错误而终止: {e}", exc_info=True)
    finally:
        # 确保在退出时删除PID文件
        remove_pid()
        logger.info("PID 文件已清理。")
