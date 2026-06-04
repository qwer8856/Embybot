# xboard_api.py
import os
import sqlite3
from datetime import datetime

import bcrypt
import pymysql

import config

DB_TYPE = getattr(config, 'XBOARD_DB_TYPE', 'mysql').lower()
if DB_TYPE in ('sqlite', 'sqlite3'):
    DB_BACKEND = 'sqlite'
elif DB_TYPE in ('mysql', 'mariadb'):
    DB_BACKEND = 'mysql'
else:
    raise ValueError("XBOARD_DB_TYPE 仅支持 mysql / mariadb / sqlite / sqlite3")
DB_ERRORS = (pymysql.MySQLError, sqlite3.Error)


def _resolve_sqlite_path(path):
    if os.path.isabs(path):
        return path
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, path)


def _db_placeholder():
    return '?' if DB_BACKEND == 'sqlite' else '%s'


def _get_db_connection():
    """建立数据库连接。"""
    if DB_BACKEND == 'sqlite':
        db_path = _resolve_sqlite_path(getattr(config, 'XBOARD_SQLITE_PATH', 'xboard.db'))
        if not os.path.exists(db_path):
            raise sqlite3.OperationalError(f"SQLite 数据库文件不存在: {db_path}")
        connection = sqlite3.connect(db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    return pymysql.connect(
        host=getattr(config, 'XBOARD_DB_HOST', 'localhost'),
        user=getattr(config, 'XBOARD_DB_USER', ''),
        password=getattr(config, 'XBOARD_DB_PASSWORD', ''),
        database=getattr(config, 'XBOARD_DB_NAME', ''),
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=20,
        write_timeout=20,
    )


def _row_to_dict(row):
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    return dict(row)


def _rows_to_dicts(rows):
    if not rows:
        return []
    return [_row_to_dict(row) for row in rows]


def _fetch_one(sql, params=()):
    connection = None
    cursor = None
    try:
        connection = _get_db_connection()
        cursor = connection.cursor()
        cursor.execute(sql, params)
        return _row_to_dict(cursor.fetchone())
    except DB_ERRORS as e:
        print(f"数据库错误: {e}")
        return None
    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()


def _fetch_all(sql, params=()):
    connection = None
    cursor = None
    try:
        connection = _get_db_connection()
        cursor = connection.cursor()
        cursor.execute(sql, params)
        return _rows_to_dicts(cursor.fetchall())
    except DB_ERRORS as e:
        print(f"数据库错误: {e}")
        return None
    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()


def _execute_write(sql, params=()):
    connection = None
    cursor = None
    try:
        connection = _get_db_connection()
        cursor = connection.cursor()
        cursor.execute(sql, params)
        connection.commit()
        return cursor.rowcount > 0
    except DB_ERRORS as e:
        print(f"数据库错误: {e}")
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()


def _to_int(value, default=None):
    if value in (None, ''):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def _ensure_bytes(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    return str(value).encode('utf-8')


def get_user_by_telegram_id(telegram_id):
    """通过 Telegram ID 获取 xboard 用户信息"""
    ph = _db_placeholder()
    sql = f"SELECT * FROM `v2_user` WHERE `telegram_id` = {ph}"
    return _fetch_one(sql, (telegram_id,))


def get_user_by_email(email):
    """通过 Email 获取 xboard 用户信息"""
    ph = _db_placeholder()
    sql = f"SELECT * FROM `v2_user` WHERE `email` = {ph}"
    return _fetch_one(sql, (email,))


def verify_user_password(email, password):
    """验证用户的邮箱和密码"""
    user = get_user_by_email(email)
    if not user or 'password' not in user:
        return False

    hashed_password = _ensure_bytes(user['password'])
    if not hashed_password:
        return False

    try:
        return bcrypt.checkpw(password.encode('utf-8'), hashed_password)
    except ValueError:
        return False


def bind_telegram_id(email, telegram_id):
    """为指定邮箱的用户绑定 Telegram ID"""
    ph = _db_placeholder()
    sql = f"UPDATE `v2_user` SET `telegram_id` = {ph} WHERE `email` = {ph}"
    return _execute_write(sql, (telegram_id, email))


def unbind_telegram_id(telegram_id):
    """解绑用户的 Telegram ID"""
    ph = _db_placeholder()
    sql = f"UPDATE `v2_user` SET `telegram_id` = NULL WHERE `telegram_id` = {ph}"
    return _execute_write(sql, (telegram_id,))


def get_subscription_details(telegram_id):
    """获取用户的订阅详情"""
    user = get_user_by_telegram_id(telegram_id)
    if not user:
        return None

    plan_id = user.get('plan_id')
    if plan_id in (None, '', 0, '0'):
        return None

    ph = _db_placeholder()
    sql = f"SELECT `name` FROM `v2_plan` WHERE `id` = {ph}"
    plan = _fetch_one(sql, (plan_id,))
    if not plan:
        return None

    expired_at_ts = _to_int(user.get('expired_at'), None)
    if expired_at_ts in (None, 0):
        expired_at = '长期有效'
    else:
        expired_at = datetime.fromtimestamp(expired_at_ts).strftime('%Y-%m-%d %H:%M:%S')

    uploaded_gb = round(_to_int(user.get('u'), 0) / (1024 ** 3), 2)
    downloaded_gb = round(_to_int(user.get('d'), 0) / (1024 ** 3), 2)
    total_gb = round(_to_int(user.get('transfer_enable'), 0) / (1024 ** 3), 2)

    subscription_url = f"{config.XBOARD_URL}/s/{user['token']}"

    return {
        'plan_name': plan['name'],
        'expired_at': expired_at,
        'uploaded_gb': uploaded_gb,
        'downloaded_gb': downloaded_gb,
        'total_gb': total_gb,
        'balance': user.get('balance', 0),
        'subscription_url': subscription_url,
    }


def get_active_subscriptions():
    """
    从 xboard 数据库获取所有有效的订阅用户（订阅未过期且流量 > 0）。
    """
    now_ts = int(datetime.now().timestamp())
    ph = _db_placeholder()
    sql = f"""
    SELECT `email`, `telegram_id`
    FROM `v2_user`
    WHERE `plan_id` IS NOT NULL
      AND (COALESCE(`expired_at`, 0) = 0 OR `expired_at` > {ph})
      AND `transfer_enable` > (`u` + `d`)
    """
    return _fetch_all(sql, (now_ts,))


def get_all_users():
    """
    从 xboard 数据库获取所有用户。
    """
    sql = "SELECT `email`, `telegram_id` FROM `v2_user`"
    return _fetch_all(sql)


def get_bound_user_count():
    """统计已绑定 Telegram 的用户数量"""
    sql = "SELECT COUNT(*) AS cnt FROM `v2_user` WHERE `telegram_id` IS NOT NULL"
    row = _fetch_one(sql)
    return _to_int(row.get('cnt') if row else None, 0)
