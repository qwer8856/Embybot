# telegram_bot.py
import logging
import string
import random
import time
import asyncio
import subprocess
import sys
import os
import json
import html
import warnings
import stat
import threading
import config
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import Application, BaseUpdateProcessor, CommandHandler, ConversationHandler, MessageHandler, filters, CallbackContext, CallbackQueryHandler
from telegram.warnings import PTBUserWarning

XBOARD_DISPLAY_NAME = getattr(config, 'XBOARD_DISPLAY_NAME', 'Xboard')
EMBY_DISPLAY_NAME = getattr(config, 'EMBY_DISPLAY_NAME', 'Emby')
SUBSCRIPTION_URLS = getattr(config, 'SUBSCRIPTION_URLS', [])
ADMIN_USER_IDS = getattr(config, 'ADMIN_USER_IDS', [])
ANNOUNCEMENT = getattr(config, 'ANNOUNCEMENT', {
    'enabled': False,
    'show_in_start': False,
    'show_button': False,
})
from xboard_api import (
    get_user_by_telegram_id,
    get_user_by_email,
    get_subscription_details,
    verify_user_password,
    bind_telegram_id,
    unbind_telegram_id,
)
from emby_api import (
    get_emby_users,
    get_emby_user_by_name,
    create_emby_user,
    reset_emby_password,
    enable_emby_user,
    delete_emby_user,
    apply_bot_default_user_policy,
    user_policy_matches_bot_default,
    get_emby_servers,
    get_emby_server,
)
from subscription_parser import generate_ip_list_from_subscription

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Suppress warnings about per_message=False; callbacks must start conversations to accept follow-up messages.
warnings.filterwarnings("ignore", category=PTBUserWarning)

_BLOCKING_IO_SEMAPHORE = asyncio.Semaphore(16)
_PASSWORD_STORE_LOCK = threading.RLock()
PASSWORD_STORE_SCHEMA_VERSION = 2
LEGACY_DEFAULT_EMBY_SERVER_KEY = 'default'

async def run_blocking(func, *args, **kwargs):
    """Run blocking DB/API/file work outside the Telegram event loop."""
    async with _BLOCKING_IO_SEMAPHORE:
        return await asyncio.to_thread(func, *args, **kwargs)

class PerUserUpdateProcessor(BaseUpdateProcessor):
    """Process different users concurrently while keeping each user's updates ordered."""

    def __init__(self, max_concurrent_updates=16):
        super().__init__(max_concurrent_updates)
        self._locks = {}
        self._locks_guard = None

    async def initialize(self):
        self._locks_guard = asyncio.Lock()

    async def shutdown(self):
        self._locks.clear()
        self._locks_guard = None

    @staticmethod
    def _update_key(update):
        user = getattr(update, 'effective_user', None)
        if user and user.id is not None:
            return ('user', user.id)

        chat = getattr(update, 'effective_chat', None)
        if chat and chat.id is not None:
            return ('chat', chat.id)

        return ('unknown', 0)

    async def do_process_update(self, update, coroutine):
        key = self._update_key(update)

        if self._locks_guard is None:
            await coroutine
            return

        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock

        async with lock:
            await coroutine

async def safe_answer_callback_query(query):
    """Acknowledge a button click without letting Telegram network hiccups abort handling."""
    try:
        await query.answer()
    except (TimedOut, NetworkError, BadRequest) as exc:
        logger.warning("回答按钮回调失败，继续处理后续逻辑: %s", exc)

# 用于保存用户设置的 Emby 密码
PASSWORD_STORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'emby_passwords.json')

def _load_password_store():
    """加载保存的密码映射。"""
    with _PASSWORD_STORE_LOCK:
        if not os.path.exists(PASSWORD_STORE_PATH):
            return {}
        try:
            with open(PASSWORD_STORE_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("无法读取密码存储文件，将重新初始化: %s", exc)
            return {}

def load_password_store():
    """Return a thread-safe snapshot of the password store."""
    return _load_password_store()

def _save_password_store(store):
    """保存密码映射。"""
    with _PASSWORD_STORE_LOCK:
        tmp_path = f"{PASSWORD_STORE_PATH}.tmp"
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(store, f, ensure_ascii=False)
            os.replace(tmp_path, PASSWORD_STORE_PATH)
        except OSError as exc:
            logger.error("保存密码存储文件失败: %s", exc)

def _is_valid_emby_username(username):
    """Return True only for real, non-empty Emby usernames."""
    return isinstance(username, str) and bool(username.strip())

def _default_emby_server_key():
    return get_emby_server()['key']

def _normalize_stored_server_key(server_key):
    """Map legacy single-server records to the current default Emby server key."""
    key = str(server_key or '').strip()
    default_key = _default_emby_server_key()
    if key == LEGACY_DEFAULT_EMBY_SERVER_KEY and default_key != LEGACY_DEFAULT_EMBY_SERVER_KEY:
        return default_key
    return key or default_key

def _merge_server_records(existing, incoming):
    """Merge duplicate server records while preserving the explicit current-key data."""
    merged = {}
    if isinstance(incoming, dict):
        merged.update(incoming)
    if isinstance(existing, dict):
        for key, value in existing.items():
            if value not in (None, ''):
                merged[key] = value
    return merged

def _normalize_record_server_keys(record):
    normalized_servers = {}
    for raw_key, server_record in record.get('servers', {}).items():
        raw_key = str(raw_key or '').strip()
        server_key = _normalize_stored_server_key(raw_key)
        server_record = server_record.copy() if isinstance(server_record, dict) else {}
        if server_key in normalized_servers:
            if raw_key == server_key:
                normalized_servers[server_key] = _merge_server_records(server_record, normalized_servers[server_key])
            else:
                normalized_servers[server_key] = _merge_server_records(normalized_servers[server_key], server_record)
        else:
            normalized_servers[server_key] = server_record

    record['servers'] = normalized_servers
    record['schema_version'] = PASSWORD_STORE_SCHEMA_VERSION
    return record

def _coerce_password_record(stored_data):
    """将旧密码记录转换成多 Emby 结构，调用方按需写回。"""
    default_key = _default_emby_server_key()

    if isinstance(stored_data, dict) and isinstance(stored_data.get('servers'), dict):
        record = stored_data.copy()
        record['servers'] = {
            str(key): value.copy() if isinstance(value, dict) else {}
            for key, value in stored_data.get('servers', {}).items()
        }
        return _normalize_record_server_keys(record)

    if isinstance(stored_data, dict):
        record = {
            'telegram_id': stored_data.get('telegram_id'),
            'servers': {},
            'schema_version': PASSWORD_STORE_SCHEMA_VERSION,
        }
        emby_username = stored_data.get('emby_username')
        password = stored_data.get('password')
        if _is_valid_emby_username(emby_username) or password:
            record['servers'][default_key] = {
                'emby_username': emby_username,
                'password': password,
            }
        return _normalize_record_server_keys(record)

    if isinstance(stored_data, str):
        return _normalize_record_server_keys({
            'telegram_id': None,
            'schema_version': PASSWORD_STORE_SCHEMA_VERSION,
            'servers': {
                default_key: {
                    'emby_username': None,
                    'password': stored_data,
                }
            }
        })

    return _normalize_record_server_keys({
        'telegram_id': None,
        'schema_version': PASSWORD_STORE_SCHEMA_VERSION,
        'servers': {}
    })

def _get_server_record(stored_data, server_key=None):
    record = _coerce_password_record(stored_data)
    key = _normalize_stored_server_key(server_key or _default_emby_server_key())
    return record.get('servers', {}).get(key)

def migrate_password_store_to_current_schema():
    """Persist old password-store records in the current multi-Emby schema."""
    with _PASSWORD_STORE_LOCK:
        store = _load_password_store()
        if not store:
            return 0

        migrated = 0
        for email, stored_data in list(store.items()):
            record = _coerce_password_record(stored_data)
            if record != stored_data:
                store[email] = record
                migrated += 1

        if migrated:
            _save_password_store(store)

        return migrated

def _get_available_emby_servers():
    return get_emby_servers()

def _server_choice_keyboard(action_prefix, servers=None):
    servers = servers or _get_available_emby_servers()
    buttons = [
        InlineKeyboardButton(server['display_name'], callback_data=f"{action_prefix}:{server['key']}")
        for server in servers
    ]
    keyboard = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    keyboard.append([InlineKeyboardButton("返回主页", callback_data='main_menu')])
    return InlineKeyboardMarkup(keyboard)

def _format_server_label(server):
    return server['display_name']

def _escape_html(value):
    return html.escape(str(value), quote=False)

def _format_account_message(title, account, include_password=True):
    server = account['server']
    message = (
        f"<b>{title}</b>\n\n"
        f"<b>服务器:</b> {_format_server_label(server)}\n"
        f"<b>服务器地址:</b> <code>{server['public_url']}</code>\n"
        f"<b>服务器端口:</b> <code>{server['port']}</code>\n"
        f"<b>用户名:</b> <code>{account['emby_username']}</code>\n"
    )
    if include_password and account.get('password'):
        message += f"<b>密码:</b> <code>{account['password']}</code>\n"
    elif include_password:
        message += "暂未记录密码，请使用重置密码功能重新设置。\n"

    message += "\n💡 <i>点击上方信息可直接复制</i>"
    return message

def store_emby_password(email, password, emby_username=None, telegram_id=None, server_key=None):
    """记录用户当前的 Emby 密码（兼容旧格式）。"""
    server_key = server_key or _default_emby_server_key()
    with _PASSWORD_STORE_LOCK:
        store = _load_password_store()
        record = _coerce_password_record(store.get(email))
        if telegram_id is not None:
            record['telegram_id'] = telegram_id

        server_record = record.setdefault('servers', {}).setdefault(server_key, {})
        if _is_valid_emby_username(emby_username):
            server_record['emby_username'] = emby_username.strip()
        server_record['password'] = password

        # 保留默认服顶层字段，便于旧版本脚本临时回滚时读取。
        if server_key == _default_emby_server_key():
            if _is_valid_emby_username(server_record.get('emby_username')):
                record['emby_username'] = server_record['emby_username']
            record['password'] = server_record.get('password')

        store[email] = record
        _save_password_store(store)

def get_stored_emby_password(email, server_key=None):
    """读取用户最近一次设置的密码（兼容新旧格式）。"""
    store = _load_password_store()
    data = _get_server_record(store.get(email), server_key)
    if isinstance(data, dict):
        return data.get('password')
    return None

def delete_stored_emby_password(email, server_key=None):
    """删除用户的密码记录。"""
    with _PASSWORD_STORE_LOCK:
        store = _load_password_store()
        if email not in store:
            return

        if server_key is None:
            store.pop(email)
        else:
            record = _coerce_password_record(store.get(email))
            record.get('servers', {}).pop(server_key, None)
            if server_key == _default_emby_server_key():
                record.pop('emby_username', None)
                record.pop('password', None)
            if record.get('servers'):
                store[email] = record
            else:
                store.pop(email, None)
        _save_password_store(store)


def delete_stored_emby_passwords_for_identity(email=None, telegram_id=None):
    """删除某个邮箱或 Telegram ID 在本地保存的 Emby 账号记录。"""
    with _PASSWORD_STORE_LOCK:
        store = _load_password_store()
        removed = 0

        for stored_email, stored_data in list(store.items()):
            record = _coerce_password_record(stored_data)
            if stored_email == email or (telegram_id is not None and record.get('telegram_id') == telegram_id):
                store.pop(stored_email, None)
                removed += 1

        if removed:
            _save_password_store(store)

        return removed


def get_stored_emby_account_entries(email, telegram_id=None, password_store=None):
    """返回某个 Xboard 邮箱可能关联的 Emby 账号记录，不访问 Emby API。"""
    password_store = password_store if isinstance(password_store, dict) else _load_password_store()
    servers = _get_available_emby_servers()
    server_keys = {server['key'] for server in servers}
    entries = []
    seen = set()

    def add_entry(server_key, emby_username, password=None, stored_email=email):
        if server_key not in server_keys or not _is_valid_emby_username(emby_username):
            return
        unique_key = (server_key, emby_username.strip().lower())
        if unique_key in seen:
            return
        seen.add(unique_key)
        entries.append({
            'source_email': stored_email,
            'server_key': server_key,
            'emby_username': emby_username.strip(),
            'password': password,
        })

    def add_from_stored(stored_email, stored_data):
        record = _coerce_password_record(stored_data)
        if telegram_id is not None and record.get('telegram_id') not in (None, telegram_id):
            return

        for server_key, server_record in record.get('servers', {}).items():
            if not isinstance(server_record, dict):
                continue
            emby_username = server_record.get('emby_username') or stored_email
            add_entry(server_key, emby_username, server_record.get('password'), stored_email)

    if email in password_store:
        add_from_stored(email, password_store.get(email))

    if telegram_id is not None:
        for stored_email, stored_data in password_store.items():
            if stored_email == email or not isinstance(stored_data, dict):
                continue
            record = _coerce_password_record(stored_data)
            if record.get('telegram_id') == telegram_id:
                add_from_stored(stored_email, stored_data)

    # 兼容历史行为：没有显式记录时，也尝试用邮箱作为各服用户名匹配。
    for server in servers:
        add_entry(server['key'], email, None, email)

    return entries


def get_bot_managed_emby_account_entries(password_store=None):
    """返回 Bot 密码存储中明确记录过的 Emby 账号；不做邮箱兜底匹配。"""
    password_store = password_store if isinstance(password_store, dict) else _load_password_store()
    entries = []
    seen = set()

    for stored_email, stored_data in password_store.items():
        record = _coerce_password_record(stored_data)
        for server_key, server_record in record.get('servers', {}).items():
            if not isinstance(server_record, dict):
                continue

            emby_username = server_record.get('emby_username') or stored_email
            if not _is_valid_emby_username(emby_username):
                continue

            unique_key = (server_key, emby_username.strip().lower())
            if unique_key in seen:
                continue
            seen.add(unique_key)

            entries.append({
                'source_email': stored_email,
                'telegram_id': record.get('telegram_id'),
                'server_key': server_key,
                'emby_username': emby_username.strip(),
                'password': server_record.get('password'),
            })

    return entries


def _remove_bot_managed_entries_from_store(entries):
    removed = 0
    with _PASSWORD_STORE_LOCK:
        store = _load_password_store()
        for entry in entries:
            email = entry.get('source_email')
            server_key = entry.get('server_key')
            if not email or not server_key or email not in store:
                continue

            record = _coerce_password_record(store.get(email))
            if server_key not in record.get('servers', {}):
                continue

            record['servers'].pop(server_key, None)
            if server_key == _default_emby_server_key():
                record.pop('emby_username', None)
                record.pop('password', None)

            if record.get('servers'):
                store[email] = record
            else:
                store.pop(email, None)
            removed += 1

        if removed:
            _save_password_store(store)
    return removed


def delete_all_bot_managed_emby_users():
    """删除所有 Bot 存储中记录过的 Emby 用户，不扫描删除 Emby 里的其他账号。"""
    entries = get_bot_managed_emby_account_entries()
    result = {
        'planned': len(entries),
        'deleted': 0,
        'not_found': 0,
        'failed': 0,
        'removed_records': 0,
        'failures': [],
    }
    removable_entries = []

    for entry in entries:
        server = get_emby_server(entry['server_key'])
        label = f"{entry['emby_username']}@{entry['server_key']}"
        if not server:
            result['failed'] += 1
            result['failures'].append(f"{label}: 未找到服务器配置")
            continue

        emby_user = get_emby_user_by_name(entry['emby_username'], server_key=server['key'])
        if not emby_user:
            result['not_found'] += 1
            removable_entries.append(entry)
            continue

        if delete_emby_user(emby_user['Id'], server['key']):
            result['deleted'] += 1
            removable_entries.append(entry)
        else:
            result['failed'] += 1
            result['failures'].append(f"{entry['emby_username']}@{server['display_name']}: 删除失败")

    result['removed_records'] = _remove_bot_managed_entries_from_store(removable_entries)
    return result


def delete_user_emby_accounts_for_unbind(email, telegram_id):
    """删除用户解绑时关联的所有真实存在的 Emby 账号。"""
    result = {
        'planned': 0,
        'deleted': 0,
        'failed': 0,
        'failures': [],
    }
    entries = get_stored_emby_account_entries(email, telegram_id)
    emby_users_cache = {}
    seen = set()

    def get_user_map(server):
        server_key = server['key']
        if server_key not in emby_users_cache:
            users = get_emby_users(server_key)
            if not isinstance(users, list):
                emby_users_cache[server_key] = None
                result['failed'] += 1
                result['failures'].append(f"{server['display_name']}: 无法获取用户列表")
                return None

            emby_users_cache[server_key] = {
                user.get('Name', '').lower(): user
                for user in users
                if isinstance(user, dict) and user.get('Name')
            }

        return emby_users_cache[server_key]

    for entry in entries:
        server = get_emby_server(entry['server_key'])
        if not server:
            result['failed'] += 1
            result['failures'].append(f"{entry['emby_username']}@{entry['server_key']}: 未找到服务器配置")
            continue

        user_map = get_user_map(server)
        if user_map is None:
            continue

        emby_username = entry['emby_username']
        emby_user = user_map.get(emby_username.strip().lower())
        if not emby_user:
            continue

        unique_key = (server['key'], emby_user.get('Id') or emby_username.strip().lower())
        if unique_key in seen:
            continue
        seen.add(unique_key)

        result['planned'] += 1
        label = f"{emby_username}@{server['display_name']}"

        if delete_emby_user(emby_user['Id'], server['key']):
            result['deleted'] += 1
            delete_stored_emby_password(entry['source_email'], server['key'])
        else:
            result['failed'] += 1
            result['failures'].append(label)

    return result


def count_emby_users_bound_via_bot():
    """统计通过机器人绑定并拥有 Emby 账号的用户数量"""
    account_entries = get_bot_managed_emby_account_entries()
    if not account_entries:
        return 0

    emby_name_sets = {}
    for server in _get_available_emby_servers():
        emby_users = get_emby_users(server['key'])
        if not isinstance(emby_users, list):
            continue
        emby_name_sets[server['key']] = {
            user.get('Name', '').lower()
            for user in emby_users
            if isinstance(user, dict) and user.get('Name')
        }

    count = 0
    for entry in account_entries:
        emby_names = emby_name_sets.get(entry['server_key'], set())
        if entry['emby_username'].lower() in emby_names:
            count += 1
    
    return count

# 对话状态
ASK_EMAIL, ASK_PASSWORD, ASK_NEW_EMBY_PASSWORD, CONFIRM_DELETE = range(4)

# --- 键盘 ---
def main_menu_keyboard(is_admin=False):
    keyboard = [
        [InlineKeyboardButton("我的订阅", callback_data='my_subscription'), InlineKeyboardButton(f"{EMBY_DISPLAY_NAME} 账号", callback_data='query_emby_account')],
        [InlineKeyboardButton(f"开通 {EMBY_DISPLAY_NAME}", callback_data='bind_emby'), InlineKeyboardButton("重置密码", callback_data='reset_password')],
        [InlineKeyboardButton("服务器地址", callback_data='view_server_address'), InlineKeyboardButton(f"删除 {EMBY_DISPLAY_NAME} 账号", callback_data='delete_account')],
    ]
    
    # 添加公告按钮（如果启用了公告功能）
    if ANNOUNCEMENT.get('enabled', False) and ANNOUNCEMENT.get('show_button', False):
        announcement_button = InlineKeyboardButton(
            ANNOUNCEMENT.get('button_text', '📢 查看公告'), 
            callback_data='view_announcement'
        )
        keyboard.insert(-1, [announcement_button])  # 插入到删除账号按钮之前
    
    if is_admin:
        keyboard.append([InlineKeyboardButton("管理员后台", callback_data='admin_menu')])
    return InlineKeyboardMarkup(keyboard)

def home_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("返回主页", callback_data='main_menu')]])

def admin_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton(f"{EMBY_DISPLAY_NAME} 统计", callback_data='admin_emby_stats'), InlineKeyboardButton("更新 IP", callback_data='admin_update_ips')],
        [InlineKeyboardButton("重启 Bot", callback_data='admin_restart_bot'), InlineKeyboardButton(f"删除{EMBY_DISPLAY_NAME}账号", callback_data='admin_delete_all_bot_emby')],
        [InlineKeyboardButton("返回主页", callback_data='main_menu')]
    ]
    return InlineKeyboardMarkup(keyboard)

def admin_delete_all_confirm_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("删除全部", callback_data='admin_delete_all_bot_emby_confirm'),
            InlineKeyboardButton("取消", callback_data='admin_menu'),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# --- 帮助函数 ---
def generate_random_password(length=10):
    """生成随机密码"""
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for i in range(length))

def generate_random_string(length=8):
    """生成随机字符串（用于用户名）"""
    characters = string.ascii_lowercase + string.digits
    return ''.join(random.choice(characters) for i in range(length))

def store_emby_password_with_username(email, emby_username, password, telegram_id, server_key=None):
    """记录用户的 Emby 用户名和密码，关联 Telegram ID。"""
    store_emby_password(email, password, emby_username=emby_username, telegram_id=telegram_id, server_key=server_key)

def get_emby_username_and_password(email, server_key=None):
    """获取用户的 Emby 用户名和密码（兼容新旧格式）
    
    Returns:
        tuple: (emby_username, password) 如果未找到则返回 (email, None)
    """
    password_store = _load_password_store()
    server_record = _get_server_record(password_store.get(email), server_key)

    if isinstance(server_record, dict):
        emby_username = server_record.get('emby_username')
        if not _is_valid_emby_username(emby_username):
            emby_username = email
        return emby_username, server_record.get('password')

    return email, None

def resolve_emby_accounts(email, telegram_id=None, server_key=None):
    """解析当前用户真实存在的所有 Emby 账号。"""
    accounts = []
    seen = set()
    emby_users_cache = {}

    def find_emby_user(username, target_server_key):
        if target_server_key not in emby_users_cache:
            users = get_emby_users(target_server_key)
            emby_users_cache[target_server_key] = {
                user.get('Name', '').lower(): user
                for user in users
                if isinstance(user, dict) and user.get('Name')
            } if isinstance(users, list) else {}

        return emby_users_cache[target_server_key].get(username.strip().lower())

    for entry in get_stored_emby_account_entries(email, telegram_id):
        if server_key and entry['server_key'] != server_key:
            continue

        server = get_emby_server(entry['server_key'])
        if not server:
            continue

        emby_user = find_emby_user(entry['emby_username'], server['key'])
        if not emby_user:
            continue

        unique_key = (server['key'], emby_user.get('Id') or entry['emby_username'].lower())
        if unique_key in seen:
            continue
        seen.add(unique_key)

        accounts.append({
            'source_email': entry['source_email'],
            'server_key': server['key'],
            'server': server,
            'emby_username': entry['emby_username'],
            'password': entry.get('password'),
            'telegram_id': telegram_id,
            'emby_user': emby_user,
        })

    return accounts

def resolve_emby_account(email, telegram_id=None, server_key=None):
    """解析当前用户真实存在的一个 Emby 账号，兼容旧调用。"""
    accounts = resolve_emby_accounts(email, telegram_id=telegram_id, server_key=server_key)
    return accounts[0] if accounts else None

def generate_main_menu_message(user, xboard_user, is_admin=False):
    """生成主菜单的欢迎消息"""
    if not xboard_user:
        return f"您好！您的 Telegram 账户尚未绑定任何 {XBOARD_DISPLAY_NAME} 账户。\n请使用 /bind 命令进行绑定。"
    
    # 构建欢迎消息
    start_message = f"您好, {user.first_name}!\n\n"

    # 如果启用了公告且设置在 start 中显示
    if ANNOUNCEMENT.get('enabled', False) and ANNOUNCEMENT.get('show_in_start', False):
        announcement_content = ANNOUNCEMENT.get('content', '')
        announcement_updated = ANNOUNCEMENT.get('updated', '')
        
        if announcement_content:
            ##start_message += f"\n{ANNOUNCEMENT.get('title', '📢 系统公告')}\n\n"
            start_message += f"{announcement_content}\n\n"
            if announcement_updated:
                start_message += f"<i>更新时间: {announcement_updated}</i>\n\n"
            start_message += "─" * 30 + "\n\n"
    
    start_message += "您也可以使用 /unbind 来解绑当前账户。"
    
    if is_admin:
        start_message += "\n您是管理员，可以使用特殊功能。"

    return start_message

# --- 命令处理 ---
async def start(update: Update, context: CallbackContext) -> None:
    """处理 /start 命令"""
    user = update.effective_user
    user_id = user.id
    is_admin = user_id in ADMIN_USER_IDS
    xboard_user = await run_blocking(get_user_by_telegram_id, user_id)

    start_message = generate_main_menu_message(user, xboard_user, is_admin)

    await update.message.reply_text(
        start_message,
        parse_mode='HTML',
        reply_markup=main_menu_keyboard(is_admin=is_admin)
    )

async def help_command(update: Update, context: CallbackContext) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    is_admin = user_id in ADMIN_USER_IDS if user_id else False
    lines = [
        f"欢迎使用 {XBOARD_DISPLAY_NAME} / {EMBY_DISPLAY_NAME} 服务助手。",
        "",
        "常用指令:",
        "/start  打开主菜单",
        f"/bind   绑定您的 {XBOARD_DISPLAY_NAME} 账户",
        "/unbind 解除绑定",
        "/status 查看当前绑定的邮箱",
        "/help   查看此帮助",
    ]
    if is_admin:
        lines.extend([
            "",
            "管理员指令:",
            "/admin 打开管理员后台"
        ])
    lines.extend([
        "",
        f"更多功能可通过主菜单按钮操作，例如查 {EMBY_DISPLAY_NAME} 账号、重置密码、查看订阅等。"
    ])
    await update.message.reply_text("\n".join(lines))

async def bind_command(update: Update, context: CallbackContext) -> int:
    """开始绑定流程的第一步：询问邮箱"""
    user_id = update.effective_user.id
    if await run_blocking(get_user_by_telegram_id, user_id):
        await update.message.reply_text(f"您的 Telegram 账户已经绑定了一个 {XBOARD_DISPLAY_NAME} 账户。如需更换，请先使用 /unbind 解绑。")
        return ConversationHandler.END
        
    await update.message.reply_text(f"请输入您的 {XBOARD_DISPLAY_NAME} 账户邮箱地址：")
    return ASK_EMAIL

async def ask_password(update: Update, context: CallbackContext) -> int:
    """接收邮箱，检查存在性，初始化重试次数，并询问密码"""
    email = update.message.text
    
    # 导入 get_user_by_email 以检查邮箱是否存在
    if not await run_blocking(get_user_by_email, email):
        await update.message.reply_text("该邮箱未在系统中注册，请重新开始 /bind。")
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data['email'] = email
    # 初始化密码尝试次数
    context.user_data['password_attempts'] = 3
    await update.message.reply_text(f"请输入您的 {XBOARD_DISPLAY_NAME} 账户密码：")
    return ASK_PASSWORD

async def process_password(update: Update, context: CallbackContext) -> int:
    """接收密码，验证并处理重试逻辑"""
    password = update.message.text
    email = context.user_data.get('email')
    user_id = update.effective_user.id

    # 验证密码
    if await run_blocking(verify_user_password, email, password):
        # 密码正确，执行绑定
        if await run_blocking(bind_telegram_id, email, user_id):
            await update.message.reply_text(
                f"🎉 绑定成功！\n您的 Telegram 账户现已关联到邮箱 {email}。\n"
                "使用 /start 查看可用服务。"
            )
        else:
            await update.message.reply_text("绑定过程中发生数据库错误，请联系管理员。")
        
        # 清理数据并结束对话
        context.user_data.clear()
        return ConversationHandler.END
    else:
        # 密码错误，处理重试逻辑
        context.user_data['password_attempts'] -= 1
        attempts_left = context.user_data['password_attempts']

        if attempts_left > 0:
            await update.message.reply_text(
                f"密码错误，您还有 {attempts_left} 次尝试机会。\n请重新输入密码："
            )
            return ASK_PASSWORD # 保持在输入密码的状态
        else:
            await update.message.reply_text(
                "密码错误次数过多，绑定流程已取消。\n如需重试，请重新使用 /bind 命令。"
            )
            # 清理数据并结束对话
            context.user_data.clear()
            return ConversationHandler.END

async def unbind_command(update: Update, context: CallbackContext) -> None:
    """处理 /unbind 命令"""
    user_id = update.effective_user.id
    xboard_user = await run_blocking(get_user_by_telegram_id, user_id)

    if not xboard_user:
        await update.message.reply_text(f"您的 Telegram 账户尚未绑定任何 {XBOARD_DISPLAY_NAME} 账户。")
        return

    email = xboard_user.get('email')
    delete_result = await run_blocking(delete_user_emby_accounts_for_unbind, email, user_id)
    if delete_result['failed']:
        failures = "\n".join(f"- {item}" for item in delete_result['failures'][:10])
        await update.message.reply_text(
            f"解绑前删除 {EMBY_DISPLAY_NAME} 账号失败，已取消解绑。\n\n"
            f"删除成功: {delete_result['deleted']} 个\n"
            f"删除失败: {delete_result['failed']} 个\n"
            f"{failures}\n\n"
            "请联系管理员处理后再重试。"
        )
        return

    if await run_blocking(unbind_telegram_id, user_id):
        await run_blocking(delete_stored_emby_passwords_for_identity, email, user_id)
        await update.message.reply_text(
            f"解绑成功！您的 Telegram 账户不再关联任何 {XBOARD_DISPLAY_NAME} 账户。\n"
            f"已同步删除 {delete_result['deleted']} 个 {EMBY_DISPLAY_NAME} 账号。\n"
            "如需再次使用，请 /bind。"
        )
    else:
        await update.message.reply_text(
            f"解绑失败，请联系管理员。已删除 {delete_result['deleted']} 个 {EMBY_DISPLAY_NAME} 账号。"
        )


async def status_command(update: Update, context: CallbackContext) -> None:
    """处理 /status 命令，检查绑定状态"""
    user_id = update.effective_user.id
    xboard_user = await run_blocking(get_user_by_telegram_id, user_id)

    if xboard_user:
        email = xboard_user.get('email', 'N/A')
        # 使用 MarkdownV2 需要对特殊字符进行转义，但这里 email 一般没有问题
        await update.message.reply_text(f"您的 Telegram 账户已绑定到 {XBOARD_DISPLAY_NAME} 邮箱：\n`{email}`", parse_mode='MarkdownV2')
    else:
        await update.message.reply_text(f"您的 Telegram 账户尚未绑定任何 {XBOARD_DISPLAY_NAME} 账户。\n请使用 /bind 命令进行绑定。")


async def admin_command(update: Update, context: CallbackContext) -> None:
    """处理 /admin 命令，仅限管理员"""
    user_id = update.effective_user.id
    if user_id not in ADMIN_USER_IDS:
        await update.message.reply_text("抱歉，此功能仅限管理员使用。")
        return

    await update.message.reply_text(
        "管理员后台:",
        reply_markup=admin_menu_keyboard()
    )

async def cancel(update: Update, context: CallbackContext) -> int:
    """取消对话"""
    # 检查是否有原始查询，以便在取消时编辑消息
    query = update.callback_query
    if query:
        await safe_answer_callback_query(query)
        # 检查用户是否是管理员，以便显示正确的键盘
        is_admin = query.from_user.id in ADMIN_USER_IDS
        await query.edit_message_text(
            '操作已取消。',
            reply_markup=home_keyboard()
        )
    else:
        await update.message.reply_text('操作已取消。')
    
    context.user_data.clear()
    return ConversationHandler.END

# --- 新的密码重置流程 ---
async def start_password_reset_flow(update: Update, context: CallbackContext) -> int:
    """开始重置Emby密码的对话流程，要求用户输入新密码。"""
    query = update.callback_query
    await safe_answer_callback_query(query)
    user_id = query.from_user.id
    selected_server_key = None
    if query.data and query.data.startswith('reset_password_server:'):
        selected_server_key = query.data.split(':', 1)[1]

    xboard_user = await run_blocking(get_user_by_telegram_id, user_id)
    if not xboard_user:
        await query.edit_message_text(
            f"请先使用 /bind 命令绑定您的 {XBOARD_DISPLAY_NAME} 账户。",
            reply_markup=home_keyboard()
        )
        return ConversationHandler.END

    email = xboard_user['email']
    
    # 查找用户真实存在的 Emby 账号
    emby_accounts = await run_blocking(resolve_emby_accounts, email, user_id, selected_server_key)

    if not emby_accounts:
        await query.edit_message_text(
            f'未找到您的 {EMBY_DISPLAY_NAME} 账户，请先通过"注册/绑定 {EMBY_DISPLAY_NAME}"创建。',
            reply_markup=home_keyboard()
        )
        return ConversationHandler.END

    if selected_server_key is None and len(emby_accounts) > 1:
        await query.edit_message_text(
            f"请选择要重置密码的 {EMBY_DISPLAY_NAME} 服务器：",
            reply_markup=_server_choice_keyboard('reset_password_server', [account['server'] for account in emby_accounts])
        )
        return ConversationHandler.END

    emby_account = emby_accounts[0]

    emby_username = emby_account['emby_username']
    emby_user = emby_account['emby_user']
    
    # 将 emby_user_id 和 emby_username 存储到上下文中以便后续使用
    context.user_data['emby_user_id'] = emby_user['Id']
    context.user_data['email'] = email
    context.user_data['emby_username'] = emby_username
    context.user_data['emby_server_key'] = emby_account['server_key']
    
    await query.edit_message_text(f"请输入您想设置的新的 {_format_server_label(emby_account['server'])} 密码（至少6位）:")
    return ASK_NEW_EMBY_PASSWORD

async def process_new_emby_password(update: Update, context: CallbackContext) -> int:
    """处理用户输入的新密码并重置。"""
    new_password = update.message.text
    user_id = update.effective_user.id
    is_admin = user_id in ADMIN_USER_IDS
    
    if len(new_password) < 6:
        await update.message.reply_text("密码太短，请输入至少6位的新密码:")
        return ASK_NEW_EMBY_PASSWORD # 保持在当前状态

    emby_user_id = context.user_data.get('emby_user_id')
    email = context.user_data.get('email')
    emby_username = context.user_data.get('emby_username', email)
    server_key = context.user_data.get('emby_server_key')
    server = get_emby_server(server_key) or get_emby_server()

    if not emby_user_id or not email:
        await update.message.reply_text(
            f"发生内部错误，无法找到您的 {EMBY_DISPLAY_NAME} 账户信息。请重新开始。",
            reply_markup=home_keyboard()
            )
        context.user_data.clear()
        return ConversationHandler.END

    if await run_blocking(reset_emby_password, emby_user_id, new_password, server['key']):
        await run_blocking(store_emby_password, email, new_password, emby_username=emby_username, telegram_id=user_id, server_key=server['key'])
        message = (
            f"<b>🔑 {_format_server_label(server)} 密码重置成功！</b>\n\n"
            f"<b>服务器:</b> {_format_server_label(server)}\n"
            f"<b>用户名:</b> <code>{emby_username}</code>\n"
            f"<b>新密码:</b> <code>{new_password}</code>\n\n"
            f"💡 <i>点击上方用户名和密码可直接复制</i>\n"
            f"⚠️ 请尽快登录。"
        )
        await update.message.reply_text(
            text=message, 
            parse_mode='HTML',
            reply_markup=home_keyboard()
            )
    else:
        await update.message.reply_text(
            "重置密码失败，请联系管理员。",
            reply_markup=home_keyboard()
            )

    context.user_data.clear()
    return ConversationHandler.END


# --- 删除账号流程 ---
async def start_delete_account_flow(update: Update, context: CallbackContext) -> int:
    """开始删除账号的对话流程，要求用户确认。"""
    query = update.callback_query
    await safe_answer_callback_query(query)
    user_id = query.from_user.id
    selected_server_key = None
    if query.data and query.data.startswith('delete_account_server:'):
        selected_server_key = query.data.split(':', 1)[1]

    xboard_user = await run_blocking(get_user_by_telegram_id, user_id)
    if not xboard_user:
        await query.edit_message_text("您尚未绑定账户，无法执行删除操作。", reply_markup=home_keyboard())
        return ConversationHandler.END

    email = xboard_user['email']
    
    # 查找用户真实存在的 Emby 账号
    emby_accounts = await run_blocking(resolve_emby_accounts, email, user_id, selected_server_key)

    if not emby_accounts:
        await query.edit_message_text(f"未找到您的 {EMBY_DISPLAY_NAME} 账户，无需删除。", reply_markup=home_keyboard())
        return ConversationHandler.END

    if selected_server_key is None and len(emby_accounts) > 1:
        await query.edit_message_text(
            f"请选择要删除的 {EMBY_DISPLAY_NAME} 服务器账号：",
            reply_markup=_server_choice_keyboard('delete_account_server', [account['server'] for account in emby_accounts])
        )
        return ConversationHandler.END

    emby_account = emby_accounts[0]

    emby_username = emby_account['emby_username']
    emby_user = emby_account['emby_user']

    context.user_data['emby_user_id_to_delete'] = emby_user['Id']
    context.user_data['email_to_delete'] = email
    context.user_data['emby_username_to_delete'] = emby_username
    context.user_data['emby_server_key_to_delete'] = emby_account['server_key']

    keyboard = [
        [
            InlineKeyboardButton("删除账号", callback_data='confirm_delete_yes'),
            InlineKeyboardButton("取消", callback_data='confirm_delete_no'),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "<b>⚠️ 警告：此操作不可逆！</b>\n\n"
        f"您确定要删除您的 {_format_server_label(emby_account['server'])} 账户吗？\n"
        f"用户名：<code>{emby_username}</code>\n"
        "所有观看历史和设置都将永久丢失。",
        parse_mode='HTML',
        reply_markup=reply_markup
    )
    return CONFIRM_DELETE

async def process_delete_confirmation(update: Update, context: CallbackContext) -> int:
    """处理用户的删除确认。"""
    query = update.callback_query
    await safe_answer_callback_query(query)
    user_id = query.from_user.id
    is_admin = user_id in ADMIN_USER_IDS

    if query.data == 'confirm_delete_yes':
        emby_user_id = context.user_data.get('emby_user_id_to_delete')
        email = context.user_data.get('email_to_delete')
        emby_username = context.user_data.get('emby_username_to_delete', email)
        server_key = context.user_data.get('emby_server_key_to_delete')
        server = get_emby_server(server_key) or get_emby_server()

        if not emby_user_id or not email:
            await query.edit_message_text(
                "发生内部错误，无法找到要删除的账户信息。请重新开始。",
                reply_markup=home_keyboard()
            )
            context.user_data.clear()
            return ConversationHandler.END

        if await run_blocking(delete_emby_user, emby_user_id, server['key']):
            await run_blocking(delete_stored_emby_password, email, server['key'])
            await query.edit_message_text(
                f"您的 {_format_server_label(server)} 账户 ({emby_username}) 已被成功删除。\n"
                f'后续如需使用，请重新在菜单中选择"注册/绑定 {EMBY_DISPLAY_NAME}" 创建账户。',
                reply_markup=home_keyboard()
            )
        else:
            await query.edit_message_text(
                f"删除 {_format_server_label(server)} 账户失败，请联系管理员。",
                reply_markup=home_keyboard()
            )
    else: # confirm_delete_no
        await query.edit_message_text(
            "删除操作已取消。",
            reply_markup=home_keyboard()
        )

    context.user_data.clear()
    return ConversationHandler.END


async def update_ips_command(update: Update, context: CallbackContext) -> None:
    """(管理员专用) 从配置文件中的链接更新机场IP"""
    # 这是一个回调函数，update可能是CallbackQuery
    if isinstance(update, Update):
        # 从命令调用
        user_id = update.effective_user.id
        messageable = update.message
    else: # CallbackQuery
        user_id = update.from_user.id
        messageable = update.message

    if user_id not in ADMIN_USER_IDS:
        await messageable.reply_text("抱歉，此功能仅限管理员使用。")
        return

    # 检查 SUBSCRIPTION_URLS 是否为空或仍为示例值
    if not SUBSCRIPTION_URLS or all("example.com" in url for url in SUBSCRIPTION_URLS):
        await messageable.reply_text(
            "错误：请先在 `config.py` 文件中正确配置 `SUBSCRIPTION_URLS` 列表。\n"
            "您需要将示例链接替换为自己有效的机场订阅链接。"
        )
        return

    await messageable.reply_text(f"检测到 {len(SUBSCRIPTION_URLS)} 个订阅链接，正在处理，请稍候...")

    all_ips = set()
    failed_urls = []

    for url in SUBSCRIPTION_URLS:
        logger.info(f"正在处理订阅链接: {url}")
        success, result = await run_blocking(generate_ip_list_from_subscription, url)
        if success:
            all_ips.update(result)
        else:
            failed_urls.append(url)
            logger.error(f"处理链接 {url} 失败: {result}")

    if not all_ips:
        await messageable.reply_text(
            "处理完成，但未能从任何订阅链接中解析出有效的IP地址。\n"
            "请检查 `config.py` 中的链接是否正确，或查看日志获取详细错误信息。"
        )
        return

    # 写入文件
    file_path = 'server_ips.txt'
    with open(file_path, 'w', encoding='utf-8') as f:
        for ip in sorted(list(all_ips)):
            f.write(ip + '\n')
    
    logger.info(f"成功将 {len(all_ips)} 个总IP地址写入到 {file_path}")

    response_message = (
        f"成功从 {len(SUBSCRIPTION_URLS) - len(failed_urls)} 个订阅链接中合并并更新了 {len(all_ips)} 个IP地址。\n"
        f"IP列表已保存到 `{file_path}` 文件中。"
    )

    if failed_urls:
        response_message += "\n\n以下链接处理失败，请检查链接有效性或查看日志：\n" + "\n".join(failed_urls)

    await messageable.reply_text(response_message)

    # 可以选择性地展示部分IP
    if len(all_ips) > 0:
        preview_ips = "\n".join(sorted(list(all_ips))[:10])
        await messageable.reply_text(f"IP列表预览 (最多10个):\n{preview_ips}")


# --- 回调查询处理 ---
async def button(update: Update, context: CallbackContext) -> None:
    """处理内联键盘按钮点击"""
    query = update.callback_query
    await safe_answer_callback_query(query)
    user_id = query.from_user.id
    is_admin = user_id in ADMIN_USER_IDS

    # --- 管理员菜单处理 ---
    if query.data == 'admin_menu':
        if not is_admin:
            await query.edit_message_text("抱歉，此功能仅限管理员使用。")
            return
        await query.edit_message_text("管理员后台:", reply_markup=admin_menu_keyboard())
        return

    if query.data == 'main_menu':
        # 获取用户信息
        user = query.from_user
        user_id = user.id
        is_admin = user_id in ADMIN_USER_IDS
        xboard_user = await run_blocking(get_user_by_telegram_id, user_id)

        start_message = generate_main_menu_message(user, xboard_user, is_admin)

        await query.edit_message_text(
            start_message,
            parse_mode='HTML',
            reply_markup=main_menu_keyboard(is_admin=is_admin)
        )
        return
    
    if query.data == 'admin_update_ips':
        if not is_admin:
            await query.edit_message_text("抱歉，此功能仅限管理员使用。")
            return
        # 直接调用更新函数，并传入query作为上下文
        await query.edit_message_text("正在开始更新IP列表，请稍候...")
        await update_ips_command(query, context)
        return

    if query.data == 'admin_emby_stats':
        if not is_admin:
            await query.edit_message_text("抱歉，此功能仅限管理员使用。")
            return
        await query.edit_message_text(f"正在统计 {EMBY_DISPLAY_NAME} 用户数量，请稍候...", reply_markup=None)
        count = await run_blocking(count_emby_users_bound_via_bot)
        await query.edit_message_text(
            f"当前共有 {count} 个通过机器人绑定的 {EMBY_DISPLAY_NAME} 用户。",
            reply_markup=admin_menu_keyboard()
        )
        return

    if query.data == 'admin_delete_all_bot_emby':
        if not is_admin:
            await query.edit_message_text("抱歉，此功能仅限管理员使用。")
            return

        entries = await run_blocking(get_bot_managed_emby_account_entries)
        if not entries:
            await query.edit_message_text(
                f"当前没有 Bot 记录的 {EMBY_DISPLAY_NAME} 账号可删除。",
                reply_markup=admin_menu_keyboard()
            )
            return

        by_server = {}
        for entry in entries:
            server = get_emby_server(entry['server_key'])
            server_name = server['display_name'] if server else entry['server_key']
            by_server[server_name] = by_server.get(server_name, 0) + 1

        server_summary = "\n".join(
            f"- {_escape_html(server_name)}: {count} 个"
            for server_name, count in sorted(by_server.items())
        )

        await query.edit_message_text(
            "<b>⚠️ 确认删除全部 Bot 管理的 Emby 账号？</b>\n\n"
            f"将删除 <b>{len(entries)}</b> 个记录在 <code>emby_passwords.json</code> 中的账号：\n"
            f"{server_summary}\n\n"
            "不会删除 Emby 中没有被 Bot 记录的其他用户。\n"
            "此操作不可逆，请确认。",
            parse_mode='HTML',
            reply_markup=admin_delete_all_confirm_keyboard()
        )
        return

    if query.data == 'admin_delete_all_bot_emby_confirm':
        if not is_admin:
            await query.edit_message_text("抱歉，此功能仅限管理员使用。")
            return

        await query.edit_message_text(
            f"正在删除所有 Bot 记录的 {EMBY_DISPLAY_NAME} 账号，请稍候...",
            reply_markup=None
        )
        result = await run_blocking(delete_all_bot_managed_emby_users)
        message = (
            f"<b>删除完成</b>\n\n"
            f"计划处理: {result['planned']} 个\n"
            f"已从 Emby 删除: {result['deleted']} 个\n"
            f"Emby 中未找到但已清理记录: {result['not_found']} 个\n"
            f"删除失败: {result['failed']} 个\n"
            f"已清理 Bot 记录: {result['removed_records']} 条"
        )
        if result['failures']:
            failed_preview = "\n".join(_escape_html(item) for item in result['failures'][:10])
            message += f"\n\n失败预览:\n{failed_preview}"
            if len(result['failures']) > 10:
                message += f"\n... 还有 {len(result['failures']) - 10} 条"

        await query.edit_message_text(
            message,
            parse_mode='HTML',
            reply_markup=admin_menu_keyboard()
        )
        return

    if query.data == 'admin_restart_bot':
        if not is_admin:
            await query.edit_message_text("抱歉，此功能仅限管理员使用。")
            return
        
        await query.edit_message_text(
            "🔄 机器人将在 3 秒后重启...\n\n"
            "重启完成后请使用 /start 命令确认服务状态。"
        )

        script_dir = os.path.dirname(os.path.abspath(__file__))
        script_path = os.path.join(script_dir, 'restart_script.sh')
        
        # 确保脚本具备执行权限
        try:
            current_mode = os.stat(script_path).st_mode
            os.chmod(script_path, current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except Exception as e:
            logger.warning(f"无法设置脚本权限: {e}")

        try:
            logger.info("管理员 %s 正在执行重启命令", user_id)
            
            # 延迟3秒，让消息发送完成
            await asyncio.sleep(3)
            
            # 使用 nohup 在后台启动重启脚本，脱离当前进程
            # 这样即使当前进程被杀死，重启脚本也能继续运行
            command = f'nohup bash {script_path} > /dev/null 2>&1 &'
            
            # 使用 shell=True 执行命令，让它在后台运行
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=script_dir,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            
            logger.info("重启脚本已在后台启动")
            
            # 不等待进程完成，因为它会杀死当前进程
            # 机器人会在几秒内自动重启
            
        except FileNotFoundError:
            await query.edit_message_text(
                f"❌ 重启脚本不存在\n\n请确保 {script_path} 文件存在。",
                reply_markup=admin_menu_keyboard()
            )
            return
        except PermissionError:
            await query.edit_message_text(
                "❌ 无法执行重启脚本\n\n请检查文件权限:\nchmod +x restart_script.sh",
                reply_markup=admin_menu_keyboard()
            )
        except Exception as exc:
            logger.exception("重启机器人失败")
            await query.edit_message_text(
                f"❌ 重启过程中发生异常\n\n{str(exc)[:3800]}",
                reply_markup=admin_menu_keyboard()
            )
        return

    # --- 普通用户菜单处理 ---
    if query.data == 'query_emby_account':
        xboard_user = await run_blocking(get_user_by_telegram_id, user_id)
        if xboard_user:
            email = xboard_user.get('email', 'N/A')
            
            # 获取真实存在的 Emby 账号和已记录密码
            emby_accounts = await run_blocking(resolve_emby_accounts, email, user_id)
            
            # 检查是否真的有 Emby 账户
            if not emby_accounts:
                await query.edit_message_text(
                    f"您还没有创建 {EMBY_DISPLAY_NAME} 账户。\n请点击 \"注册/绑定 {EMBY_DISPLAY_NAME}\" 创建账户。",
                    reply_markup=home_keyboard()
                )
                return

            if len(emby_accounts) == 1:
                message = _format_account_message(f"📱 {EMBY_DISPLAY_NAME} 账户信息", emby_accounts[0])
                message += f"\n如需修改密码，可使用\"重置 {EMBY_DISPLAY_NAME} 密码\"。"
            else:
                blocks = []
                for index, account in enumerate(emby_accounts, start=1):
                    block = (
                        f"<b>{index}. {_format_server_label(account['server'])}</b>\n"
                        f"<b>服务器地址:</b> <code>{account['server']['public_url']}</code>\n"
                        f"<b>服务器端口:</b> <code>{account['server']['port']}</code>\n"
                        f"<b>用户名:</b> <code>{account['emby_username']}</code>\n"
                    )
                    if account.get('password'):
                        block += f"<b>密码:</b> <code>{account['password']}</code>"
                    else:
                        block += "暂未记录密码"
                    blocks.append(block)
                message = f"<b>📱 {EMBY_DISPLAY_NAME} 账户信息</b>\n\n" + "\n\n".join(blocks)
            await query.edit_message_text(
                text=message,
                parse_mode='HTML',
                reply_markup=home_keyboard()
            )
        else:
            await query.edit_message_text(
                "您尚未绑定账户，请先使用 /bind 命令绑定。",
                reply_markup=home_keyboard()
            )
        return

    if query.data == 'view_announcement':
        # 显示公告
        if not ANNOUNCEMENT.get('enabled', False):
            await query.edit_message_text(
                "公告功能已禁用。",
                reply_markup=home_keyboard()
            )
            return
        
        announcement_title = ANNOUNCEMENT.get('title', '📢 系统公告')
        announcement_content = ANNOUNCEMENT.get('content', '暂无公告内容')
        announcement_updated = ANNOUNCEMENT.get('updated', '')
        
        message = f"<b>{announcement_title}</b>\n\n{announcement_content}"
        if announcement_updated:
            message += f"\n\n<i>更新时间: {announcement_updated}</i>"
        
        await query.edit_message_text(
            text=message,
            parse_mode='HTML',
            reply_markup=home_keyboard()
        )
        return

    if query.data == 'view_server_address':
        xboard_user = await run_blocking(get_user_by_telegram_id, user_id)
        if not xboard_user:
            await query.edit_message_text(
                f"请先使用 /bind 命令绑定您的 {XBOARD_DISPLAY_NAME} 账户。",
                reply_markup=home_keyboard()
            )
            return

        email = xboard_user.get('email', 'N/A')
        emby_accounts = await run_blocking(resolve_emby_accounts, email, user_id)
        if not emby_accounts:
            await query.edit_message_text(
                f"您还没有创建 {EMBY_DISPLAY_NAME} 账户。\n请先点击 \"开通 {EMBY_DISPLAY_NAME}\" 创建账户。",
                reply_markup=home_keyboard()
            )
            return

        server_blocks = [
            (
                f"<b>{index}. {_format_server_label(server)}</b>\n"
                f"<b>服务器地址:</b> <code>{server['public_url']}</code>\n"
                f"<b>服务器端口:</b> <code>{server['port']}</code>"
            )
            for index, server in enumerate((account['server'] for account in emby_accounts), start=1)
        ]
        message = (
            f"<b>🌐 {EMBY_DISPLAY_NAME} 服务器信息</b>\n\n"
            + "\n\n".join(server_blocks)
            + "\n\n💡 <i>点击地址可直接复制</i>"
        )
        await query.edit_message_text(
            text=message,
            parse_mode='HTML',
            reply_markup=home_keyboard()
        )
        return

    if query.data == 'my_subscription':
        details = await run_blocking(get_subscription_details, user_id)
        if not details:
            await query.edit_message_text("未找到您的有效订阅。", reply_markup=home_keyboard())
            return
        
        message = (
            f"<b>您的订阅详情:</b>\n\n"
            f"<b>套餐:</b> {details['plan_name']}\n"
            f"<b>到期时间:</b> {details['expired_at']}\n"
            f"<b>已用流量:</b> {details['uploaded_gb'] + details['downloaded_gb']:.2f} GB\n"
            f"<b>总流量:</b> {details['total_gb']} GB\n"
            f"<b>余额:</b> ¥{details['balance']}\n\n"
            f"<b>订阅链接</b> (点击即可复制):\n<code>{details['subscription_url']}</code>"
        )
        await query.edit_message_text(text=message, parse_mode='HTML', reply_markup=home_keyboard())

    elif query.data == 'bind_emby':
        xboard_user = await run_blocking(get_user_by_telegram_id, user_id)
        if not xboard_user:
            await query.edit_message_text(f"请先使用 /bind 命令绑定您的 {XBOARD_DISPLAY_NAME} 账户。", reply_markup=home_keyboard())
            return

        servers = _get_available_emby_servers()
        if len(servers) > 1:
            await query.edit_message_text(
                f"请选择要注册/绑定的 {EMBY_DISPLAY_NAME} 服务器：",
                reply_markup=_server_choice_keyboard('bind_emby_server', servers)
            )
            return

        await handle_bind_emby_for_server(query, context, servers[0]['key'])
        return

    elif query.data.startswith('bind_emby_server:'):
        server_key = query.data.split(':', 1)[1]
        await handle_bind_emby_for_server(query, context, server_key)
        return

    elif query.data == 'reset_password':
        # This now just acts as an entry point for the conversation
        # The actual logic is in the ConversationHandler
        # We return the state to start the conversation
        return await start_password_reset_flow(update, context)

    elif query.data == 'delete_account':
        return await start_delete_account_flow(update, context)


async def handle_bind_emby_for_server(query, context, server_key):
    user_id = query.from_user.id
    is_admin = user_id in ADMIN_USER_IDS
    server = get_emby_server(server_key)
    if not server:
        await query.edit_message_text("未知的 Emby 服务器，请联系管理员。", reply_markup=home_keyboard())
        return

    xboard_user = await run_blocking(get_user_by_telegram_id, user_id)
    if not xboard_user:
        await query.edit_message_text(f"请先使用 /bind 命令绑定您的 {XBOARD_DISPLAY_NAME} 账户。", reply_markup=home_keyboard())
        return

    email = xboard_user['email']
    
    # 使用当前绑定邮箱和 Telegram ID 查找真实存在的 Emby 账户
    emby_account = await run_blocking(resolve_emby_account, email, user_id, server_key)
    emby_user = emby_account['emby_user'] if emby_account else None

    if not emby_user:
        # 检查订阅是否有效
        if not xboard_user.get('plan_id') or (xboard_user.get('expired_at') is not None and xboard_user.get('expired_at') < time.time()):
             await query.edit_message_text(f"您没有有效的订阅，无法创建 {_format_server_label(server)} 账户。", reply_markup=home_keyboard())
             return

        # 生成自定义用户名: jichang_ + 随机字符串
        custom_username = f"jichang_{generate_random_string(8)}"
        new_password = generate_random_password()
        
        new_emby_user = await run_blocking(create_emby_user, custom_username, server_key)
        password_reset = False
        downloads_disabled = False
        if new_emby_user:
            password_reset = await run_blocking(reset_emby_password, new_emby_user['Id'], new_password, server_key)
            if password_reset:
                downloads_disabled = await run_blocking(apply_bot_default_user_policy, new_emby_user['Id'], server_key)

        if new_emby_user and password_reset and downloads_disabled:
            # 存储密码时关联 Telegram ID 和自定义用户名
            await run_blocking(store_emby_password_with_username, email, custom_username, new_password, user_id, server_key)
            account = {
                'server': server,
                'emby_username': custom_username,
                'password': new_password,
            }
            message = _format_account_message(f"🎉 {_format_server_label(server)} 账户创建成功！", account)
            message += "\n⚠️ 请尽快登录。"
            await query.edit_message_text(text=message, parse_mode='HTML', reply_markup=home_keyboard())
        else:
            if new_emby_user:
                await run_blocking(delete_emby_user, new_emby_user['Id'], server_key)
            await query.edit_message_text(f"创建 {_format_server_label(server)} 账户失败，请联系管理员。", reply_markup=home_keyboard())
    else:
        # 如果用户已存在但被禁用，尝试启用
        if emby_user.get('Policy', {}).get('IsDisabled'):
            if await run_blocking(enable_emby_user, emby_user['Id'], server_key):
                 await run_blocking(apply_bot_default_user_policy, emby_user['Id'], server_key)
                 await query.edit_message_text(f"您的 {_format_server_label(server)} 账户已重新激活。", reply_markup=home_keyboard())
            else:
                 await query.edit_message_text(f"激活您的 {_format_server_label(server)} 账户失败。", reply_markup=home_keyboard())
        else:
            if not user_policy_matches_bot_default(emby_user):
                await run_blocking(apply_bot_default_user_policy, emby_user['Id'], server_key)
            # 从已验证的账号记录中获取用户名和密码，避免展示 None 或脏数据
            stored_username = emby_account['emby_username']
            stored_password = emby_account.get('password')
            
            account = {
                'server': server,
                'emby_username': stored_username,
                'password': stored_password,
            }
            message = _format_account_message(f"✅ 您的 {_format_server_label(server)} 账户已存在", account)
            await query.edit_message_text(text=message, parse_mode='HTML', reply_markup=home_keyboard())


def setup_bot(token):
    """设置并返回 Application 对象"""
    try:
        migrated = migrate_password_store_to_current_schema()
        if migrated:
            logger.info("已迁移 %s 条 Emby 密码存储记录到当前格式", migrated)
    except Exception:
        logger.exception("迁移 Emby 密码存储记录失败，请检查 emby_passwords.json")

    application = (
        Application.builder()
        .token(token)
        .concurrent_updates(PerUserUpdateProcessor(max_concurrent_updates=16))
        .connection_pool_size(32)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(60)
        .get_updates_write_timeout(30)
        .get_updates_pool_timeout(30)
        .build()
    )

    # 将所有处理器（包括对话）添加到 application
    # 1. 绑定流程的对话处理器
    bind_conv_handler = ConversationHandler(
        entry_points=[CommandHandler('bind', bind_command)],
        states={
            ASK_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_password)],
            ASK_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_password)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    # 2. 重置密码流程的对话处理器
    # 这个对话由一个回调查询（按按钮）启动
    reset_password_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_password_reset_flow, pattern='^(reset_password|reset_password_server:.+)$')],
        states={
            ASK_NEW_EMBY_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_new_emby_password)],
        },
        fallbacks=[CommandHandler('cancel', cancel), CallbackQueryHandler(cancel)],
        per_user=True,
        per_message=False,  # 使用 per_message=False 以确保回调触发的对话可以正常接收后续文本
    )

    # 3. 删除账号流程的对话处理器
    delete_account_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_delete_account_flow, pattern='^(delete_account|delete_account_server:.+)$')],
        states={
            CONFIRM_DELETE: [CallbackQueryHandler(process_delete_confirmation, pattern='^confirm_delete_(yes|no)$')],
        },
        fallbacks=[CommandHandler('cancel', cancel), CallbackQueryHandler(cancel)],
        per_user=True,
        per_message=False,
    )

    application.add_handler(bind_conv_handler)
    application.add_handler(reset_password_conv_handler)
    application.add_handler(delete_account_conv_handler)
    
    # 4. 常规命令处理器
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("unbind", unbind_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_command))
    
    # 5. 通用按钮处理器
    # 注意：这个处理器不应处理 'reset_password' 和 'delete_account'，因为它们现在由自己的 ConversationHandler 处理
    application.add_handler(CallbackQueryHandler(button))

    return application
