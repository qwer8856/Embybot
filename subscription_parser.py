# subscription_parser.py
import requests
import base64
import json
import socket
import logging
from urllib.parse import urlparse, unquote

def get_ip_from_domain(domain):
    """将域名解析为IP地址"""
    try:
        # 使用 socket.gethostbyname 获取 IPv4 地址
        ip_address = socket.gethostbyname(domain)
        logging.info(f"域名 {domain} 成功解析为 IP: {ip_address}")
        return ip_address
    except socket.gaierror:
        logging.warning(f"无法解析域名: {domain}")
        return None

def parse_vmess_link(link):
    """解析 Vmess 链接"""
    try:
        # Vmess 链接是 base64 编码的 JSON
        decoded_part = base64.b64decode(link[8:]).decode('utf-8')
        vmess_config = json.loads(decoded_part)
        return vmess_config.get('add')
    except Exception as e:
        logging.error(f"解析 Vmess 链接失败: {link}, 错误: {e}")
        return None

def parse_ss_link(link):
    """解析 Shadowsocks (SS) 链接"""
    try:
        # 格式: ss://method:password@server:port
        # 先 URL 解码
        decoded_link = unquote(link)
        # 移除协议头 ss://
        main_part = decoded_link[5:]
        # 分割 @ 前后的部分
        at_parts = main_part.split('@')
        if len(at_parts) == 2:
            # server:port 部分
            server_part = at_parts[1]
            # 分割 server 和 port
            if ':' in server_part:
                return server_part.split(':')[0]
        return None
    except Exception as e:
        logging.error(f"解析 SS 链接失败: {link}, 错误: {e}")
        return None

def parse_trojan_link(link):
    """解析 Trojan 链接"""
    try:
        # 格式: trojan://password@server:port
        parsed_url = urlparse(link)
        return parsed_url.hostname
    except Exception as e:
        logging.error(f"解析 Trojan 链接失败: {link}, 错误: {e}")
        return None

def generate_ip_list_from_subscription(url: str):
    """
    从订阅链接获取内容，解析出所有服务器IP，并生成 txt 文件。
    返回 (是否成功, 消息或IP列表)
    """
    try:
        logging.info(f"正在从 {url} 获取订阅内容...")
        # 设置超时和 User-Agent 模拟浏览器
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36'}
        response = requests.get(url, timeout=15, headers=headers)
        response.raise_for_status()
        
        # 尝试 Base64 解码
        try:
            content = base64.b64decode(response.content).decode('utf-8')
        except Exception:
            # 如果解码失败，可能本身就是明文
            content = response.text
        
        server_links = content.strip().split('\n')
        logging.info(f"订阅中包含 {len(server_links)} 个服务器链接。")
        
        addresses = set()
        for link in server_links:
            link = link.strip()
            address = None
            if link.startswith('vmess://'):
                address = parse_vmess_link(link)
            elif link.startswith('ss://'):
                address = parse_ss_link(link)
            elif link.startswith('trojan://'):
                address = parse_trojan_link(link)
            
            if address:
                addresses.add(address)

        logging.info(f"解析出 {len(addresses)} 个唯一的服务器地址/域名。")
        
        ip_list = set()
        for addr in addresses:
            # 判断是IP还是域名
            try:
                # 如果能转成IP，说明本身就是IP
                socket.inet_aton(addr)
                ip_list.add(addr)
            except socket.error:
                # 不是IP，当作域名解析
                ip = get_ip_from_domain(addr)
                if ip:
                    ip_list.add(ip)
        
        if not ip_list:
            return False, "无法从订阅链接中解析出任何有效的服务器IP地址。"

        # 写入文件
        file_path = 'server_ips.txt'
        with open(file_path, 'w') as f:
            for ip in sorted(list(ip_list)):
                f.write(ip + '\n')
        
        logging.info(f"成功将 {len(ip_list)} 个IP地址写入到 {file_path}")
        return True, sorted(list(ip_list))

    except requests.exceptions.RequestException as e:
        logging.error(f"获取订阅链接失败: {e}")
        return False, f"无法访问订阅链接，请检查链接是否正确或网络是否有问题。错误: {e}"
    except Exception as e:
        logging.error(f"处理订阅时发生未知错误: {e}", exc_info=True)
        return False, f"处理订阅时发生未知错误: {e}"
