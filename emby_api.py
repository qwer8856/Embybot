# emby_api.py
import re

import requests
import config

REQUEST_TIMEOUT = (5, 20)

BOT_DEFAULT_USER_POLICY = {
    # 播放期间的转码/封装转换
    'EnableAudioPlaybackTranscoding': False,
    'EnableVideoPlaybackTranscoding': False,
    'EnablePlaybackRemuxing': False,
    # 最大同时视频流
    'SimultaneousStreamLimit': 1,
    # 共享设备 / DLNA 控制
    'EnableSharedDeviceControl': False,
    # 下载、转码下载、字幕下载、相机上传
    'EnableContentDownloading': False,
    'EnableMediaConversion': False,
    'EnableSyncTranscoding': False,
    'EnableSubtitleDownloading': False,
    'AllowCameraUpload': False,
}


def _normalize_server_key(value, fallback):
    key = str(value or fallback).strip()
    key = re.sub(r'[^A-Za-z0-9_-]+', '_', key)
    return key or fallback


def _legacy_server_config():
    return {
        'key': 'default',
        'server_url': getattr(config, 'EMBY_SERVER_URL', ''),
        'public_url': getattr(config, 'EMBY_PUBLIC_URL', getattr(config, 'EMBY_SERVER_URL', '')),
        'port': str(getattr(config, 'EMBY_PORT', '443')),
        'display_name': getattr(config, 'EMBY_DISPLAY_NAME', 'Emby'),
        'api_key': getattr(config, 'EMBY_API_KEY', ''),
    }


def _normalize_server(server, index):
    fallback = f"emby{index + 1}"
    if not isinstance(server, dict):
        raise ValueError("EMBY_SERVERS 中的每一项都必须是 dict")

    server_url = (
        server.get('server_url')
        or server.get('emby_url')
        or server.get('url')
        or server.get('EMBY_SERVER_URL')
    )
    api_key = server.get('api_key') or server.get('api') or server.get('EMBY_API_KEY')
    if not server_url or not api_key:
        raise ValueError("EMBY_SERVERS 每一项都必须配置 emby_url/api 或 server_url/api_key")

    display_name = server.get('display_name') or server.get('name') or fallback
    return {
        'key': _normalize_server_key(server.get('key') or server.get('id') or display_name, fallback),
        'server_url': str(server_url).rstrip('/'),
        'public_url': str(server.get('public_url') or server.get('EMBY_PUBLIC_URL') or server_url).rstrip('/'),
        'port': str(server.get('port') or server.get('EMBY_PORT') or '443'),
        'display_name': str(display_name),
        'api_key': str(api_key),
    }


def get_emby_servers():
    """返回所有 Emby 服务器配置；未配置 EMBY_SERVERS 时兼容旧单服配置。"""
    raw_servers = getattr(config, 'EMBY_SERVERS', None)
    if not raw_servers:
        return [_legacy_server_config()]

    servers = []
    seen_keys = set()
    for index, server in enumerate(raw_servers):
        normalized = _normalize_server(server, index)
        key = normalized['key']
        if key in seen_keys:
            raise ValueError(f"EMBY_SERVERS 中存在重复 key: {key}")
        seen_keys.add(key)
        servers.append(normalized)

    return servers or [_legacy_server_config()]


def get_emby_server(server_key=None):
    servers = get_emby_servers()
    if server_key is None:
        return servers[0]

    normalized_key = _normalize_server_key(server_key, str(server_key))
    for server in servers:
        if server['key'] == normalized_key:
            return server

    return None


def is_multi_emby():
    return len(get_emby_servers()) > 1


def _headers(server):
    return {
        'X-Emby-Token': server['api_key'],
        'Content-Type': 'application/json'
    }


def get_emby_users(server_key=None):
    """获取所有 Emby 用户"""
    server = get_emby_server(server_key)
    if not server:
        print(f"未知 Emby 服务器: {server_key}")
        return None

    try:
        url = f"{server['server_url']}/emby/Users"
        headers = _headers(server)
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"获取 {server['display_name']} 用户失败: {e}")
        return None


def get_emby_user_count(server_key=None):
    """返回 Emby 中的用户总数"""
    users = get_emby_users(server_key)
    if isinstance(users, list):
        return len(users)
    return 0


def get_emby_user_by_name(username, server_key=None):
    """根据用户名获取 Emby 用户"""
    if not isinstance(username, str) or not username.strip():
        return None

    users = get_emby_users(server_key)
    if not isinstance(users, list):
        return None

    username = username.strip().lower()
    for user in users:
        if isinstance(user, dict) and user.get('Name', '').lower() == username:
            return user
    return None


def create_emby_user(username, server_key=None):
    """创建 Emby 用户"""
    server = get_emby_server(server_key)
    if not server:
        print(f"未知 Emby 服务器: {server_key}")
        return None

    try:
        url = f"{server['server_url']}/emby/Users/New"
        headers = _headers(server)
        data = {
            'Name': username
        }
        response = requests.post(url, headers=headers, json=data, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        print(f"成功创建 {server['display_name']} 用户: {username}")
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"创建 {server['display_name']} 用户 {username} 失败: {e}")
        return None


def _update_user_policy_fields(user_id, updates, server_key=None):
    """获取完整用户策略，合并指定字段后提交。"""
    server = get_emby_server(server_key)
    if not server:
        print(f"未知 Emby 服务器: {server_key}")
        return False

    headers = _headers(server)
    try:
        # 1. 获取完整的用户信息
        user_url = f"{server['server_url']}/emby/Users/{user_id}"
        user_response = requests.get(user_url, headers=headers, timeout=REQUEST_TIMEOUT)
        user_response.raise_for_status()
        user_data = user_response.json()
        
        # 2. 提取并修改策略
        policy = user_data.get('Policy', {})
        policy.update(updates)
        
        # 3. 提交更新后的策略
        policy_url = f"{server['server_url']}/emby/Users/{user_id}/Policy"
        update_response = requests.post(policy_url, headers=headers, json=policy, timeout=REQUEST_TIMEOUT)
        update_response.raise_for_status()

        print(f"成功更新 {server['display_name']} 用户策略 ID: {user_id}")
        return True
        
    except requests.exceptions.RequestException as e:
        print(f"更新 {server['display_name']} 用户策略 ID {user_id} 失败: {e}")
        return False


def _update_user_policy(user_id, is_disabled, server_key=None):
    """
    更新用户启用/禁用状态。
    is_disabled: bool, True 表示禁用, False 表示启用。
    """
    action = "禁用" if is_disabled else "启用"
    if _update_user_policy_fields(user_id, {'IsDisabled': is_disabled}, server_key):
        server = get_emby_server(server_key)
        server_name = server['display_name'] if server else 'Emby'
        print(f"成功{action} {server_name} 用户 ID: {user_id}")
        return True

    server = get_emby_server(server_key)
    server_name = server['display_name'] if server else 'Emby'
    print(f"{action} {server_name} 用户 ID {user_id} 失败")
    return False


def disable_emby_user(user_id, server_key=None):
    """禁用 Emby 用户 (使用官方推荐的标准方法)"""
    return _update_user_policy(user_id, True, server_key)


def enable_emby_user(user_id, server_key=None):
    """启用 Emby 用户 (使用官方推荐的标准方法)"""
    return _update_user_policy(user_id, False, server_key)


def disable_emby_downloads(user_id, server_key=None):
    """兼容旧调用：应用 Bot 默认用户限制策略。"""
    return apply_bot_default_user_policy(user_id, server_key)


def apply_bot_default_user_policy(user_id, server_key=None):
    """应用 Bot 创建账号的默认限制策略。"""
    return _update_user_policy_fields(user_id, BOT_DEFAULT_USER_POLICY, server_key)


def user_policy_matches_bot_default(emby_user):
    """检查已获取的 Emby 用户对象是否满足 Bot 默认限制策略。"""
    if not isinstance(emby_user, dict):
        return False

    policy = emby_user.get('Policy')
    if not isinstance(policy, dict):
        return False

    return all(policy.get(key) == value for key, value in BOT_DEFAULT_USER_POLICY.items())


def reset_emby_password(user_id, new_password, server_key=None):
    """重置 Emby 用户密码"""
    server = get_emby_server(server_key)
    if not server:
        print(f"未知 Emby 服务器: {server_key}")
        return False

    try:
        url = f"{server['server_url']}/emby/Users/{user_id}/Password"
        headers = _headers(server)
        data = {
            'Id': user_id,
            'NewPw': new_password,
            'Reset': False
        }
        response = requests.post(url, headers=headers, json=data, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        print(f"成功重置 {server['display_name']} 用户 {user_id} 的密码。")
        return True
    except requests.exceptions.RequestException as e:
        print(f"重置 {server['display_name']} 用户 {user_id} 的密码失败: {e}")
        return False


def delete_emby_user(user_id, server_key=None):
    """删除 Emby 用户"""
    server = get_emby_server(server_key)
    if not server:
        print(f"未知 Emby 服务器: {server_key}")
        return False

    try:
        url = f"{server['server_url']}/emby/Users/{user_id}"
        headers = {
            'X-Emby-Token': server['api_key']
        }
        response = requests.delete(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        print(f"成功删除 {server['display_name']} 用户: {user_id}")
        return True
    except requests.exceptions.RequestException as e:
        print(f"删除 {server['display_name']} 用户 {user_id} 失败: {e}")
        return False
