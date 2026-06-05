# detect_exit_ips.py
import argparse
import base64
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from urllib.parse import parse_qs, unquote, urlparse

import requests


USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
DEFAULT_IP_CHECK_URL = 'https://api.ipify.org'


def _load_subscription_urls_from_config():
    try:
        import config
    except ImportError:
        return []

    urls = getattr(config, 'SUBSCRIPTION_URLS', [])
    if isinstance(urls, str):
        urls = [urls]

    return [
        str(url).strip()
        for url in urls
        if str(url).strip() and 'example.com' not in str(url)
    ]


def _b64decode_text(value):
    raw = value.strip()
    if isinstance(raw, str):
        raw = raw.encode()
    raw += b'=' * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw).decode('utf-8', errors='replace')


def _decode_subscription(content_bytes, fallback_text):
    try:
        decoded = _b64decode_text(content_bytes)
        if any(proto in decoded for proto in ('ss://', 'vmess://', 'vless://')):
            return decoded
    except Exception:
        pass
    return fallback_text


def _split_host_port(value):
    value = value.strip()
    if value.startswith('['):
        host, rest = value[1:].split(']', 1)
        port = rest.lstrip(':')
        return host, int(port)

    host, port = value.rsplit(':', 1)
    return host, int(port)


def _node_name_from_link(link, fallback):
    if '#' not in link:
        return fallback
    name = unquote(link.rsplit('#', 1)[1]).strip()
    return name or fallback


def _transport_from_params(params):
    network = (params.get('type') or params.get('net') or [''])[0]
    if network == 'ws':
        transport = {
            'type': 'ws',
            'path': (params.get('path') or ['/'])[0] or '/',
        }
        host = (params.get('host') or [''])[0]
        if host:
            transport['headers'] = {'Host': host}
        return transport

    if network == 'grpc':
        service_name = (params.get('serviceName') or params.get('service_name') or [''])[0]
        transport = {'type': 'grpc'}
        if service_name:
            transport['service_name'] = service_name
        return transport

    if network == 'http':
        host = (params.get('host') or [''])[0]
        path = (params.get('path') or ['/'])[0] or '/'
        transport = {'type': 'http', 'path': path}
        if host:
            transport['host'] = [host]
        return transport

    return None


def _tls_from_params(params, server):
    security = (params.get('security') or [''])[0]
    if security not in ('tls', 'reality'):
        return None

    tls = {
        'enabled': True,
        'server_name': (params.get('sni') or params.get('serverName') or [server])[0] or server,
    }

    allow_insecure = (params.get('allowInsecure') or params.get('skip-cert-verify') or [''])[0]
    if str(allow_insecure).lower() in ('1', 'true', 'yes'):
        tls['insecure'] = True

    fingerprint = (params.get('fp') or [''])[0]
    if fingerprint:
        tls['utls'] = {
            'enabled': True,
            'fingerprint': fingerprint,
        }

    if security == 'reality':
        public_key = (params.get('pbk') or params.get('publicKey') or [''])[0]
        short_id = (params.get('sid') or params.get('shortId') or [''])[0]
        reality = {'enabled': True}
        if public_key:
            reality['public_key'] = public_key
        if short_id:
            reality['short_id'] = short_id
        tls['reality'] = reality

    return tls


def parse_ss_link(link):
    body = link[len('ss://'):].split('#', 1)[0]
    body = body.split('?', 1)[0]
    body = unquote(body)

    if '@' in body:
        userinfo, server_part = body.rsplit('@', 1)
        if ':' not in userinfo:
            userinfo = _b64decode_text(userinfo)
        method, password = userinfo.split(':', 1)
    else:
        decoded = _b64decode_text(body)
        userinfo, server_part = decoded.rsplit('@', 1)
        method, password = userinfo.split(':', 1)

    server, port = _split_host_port(server_part)
    return {
        'name': _node_name_from_link(link, f'ss-{server}'),
        'protocol': 'ss',
        'server': server,
        'outbound': {
            'type': 'shadowsocks',
            'tag': 'proxy',
            'server': server,
            'server_port': port,
            'method': method,
            'password': password,
        },
    }


def parse_vmess_link(link):
    data = json.loads(_b64decode_text(link[len('vmess://'):]))
    server = data.get('add')
    port = int(data.get('port') or 443)
    outbound = {
        'type': 'vmess',
        'tag': 'proxy',
        'server': server,
        'server_port': port,
        'uuid': data.get('id'),
        'security': data.get('scy') or 'auto',
        'alter_id': int(data.get('aid') or 0),
    }

    if data.get('tls') == 'tls':
        outbound['tls'] = {
            'enabled': True,
            'server_name': data.get('sni') or data.get('host') or server,
        }

    params = {
        'type': [data.get('net') or ''],
        'path': [data.get('path') or '/'],
        'host': [data.get('host') or ''],
        'serviceName': [data.get('path') or ''],
    }
    transport = _transport_from_params(params)
    if transport:
        outbound['transport'] = transport

    return {
        'name': data.get('ps') or f'vmess-{server}',
        'protocol': 'vmess',
        'server': server,
        'outbound': outbound,
    }


def parse_vless_link(link):
    parsed = urlparse(link)
    params = parse_qs(parsed.query)
    server = parsed.hostname
    outbound = {
        'type': 'vless',
        'tag': 'proxy',
        'server': server,
        'server_port': int(parsed.port or 443),
        'uuid': parsed.username,
    }

    flow = (params.get('flow') or [''])[0]
    if flow:
        outbound['flow'] = flow

    tls = _tls_from_params(params, server)
    if tls:
        outbound['tls'] = tls

    transport = _transport_from_params(params)
    if transport:
        outbound['transport'] = transport

    return {
        'name': _node_name_from_link(link, f'vless-{server}'),
        'protocol': 'vless',
        'server': server,
        'outbound': outbound,
    }


def parse_subscription(url):
    response = requests.get(url, timeout=20, headers={'User-Agent': USER_AGENT})
    response.raise_for_status()
    content = _decode_subscription(response.content, response.text)
    nodes = []
    errors = []

    for index, line in enumerate(content.splitlines(), start=1):
        link = line.strip()
        if not link:
            continue

        try:
            if link.startswith('ss://'):
                nodes.append(parse_ss_link(link))
            elif link.startswith('vmess://'):
                nodes.append(parse_vmess_link(link))
            elif link.startswith('vless://'):
                nodes.append(parse_vless_link(link))
        except Exception as exc:
            errors.append(f'第 {index} 行解析失败: {exc}')

    return nodes, errors


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _sing_box_config(outbound, listen_port):
    return {
        'log': {'level': 'info'},
        'inbounds': [
            {
                'type': 'socks',
                'tag': 'socks-in',
                'listen': '127.0.0.1',
                'listen_port': listen_port,
            }
        ],
        'outbounds': [
            outbound,
            {'type': 'direct', 'tag': 'direct'},
        ],
        'route': {
            'final': 'proxy',
        },
    }


def _curl_ip(listen_port, ip_url, timeout):
    command = [
        'curl',
        '-fsS',
        '--max-time',
        str(timeout),
        '--socks5-hostname',
        f'127.0.0.1:{listen_port}',
        ip_url,
    ]
    result = subprocess.run(command, text=True, capture_output=True, timeout=timeout + 3)
    if result.returncode != 0:
        return None, (result.stderr or result.stdout).strip()
    return result.stdout.strip(), None


def _read_process_stream(stream, lines):
    try:
        for line in iter(stream.readline, ''):
            line = line.strip()
            if line:
                lines.append(line)
                del lines[:-30]
    except Exception:
        pass


def detect_node_exit_ip(node, ip_url, timeout):
    listen_port = _free_port()
    config = _sing_box_config(node['outbound'], listen_port)

    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = os.path.join(tmpdir, 'sing-box-node.json')
        with open(config_path, 'w', encoding='utf-8') as file:
            json.dump(config, file, ensure_ascii=False, indent=2)

        process = subprocess.Popen(
            ['sing-box', 'run', '-c', config_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        process_logs = []
        stdout_thread = threading.Thread(target=_read_process_stream, args=(process.stdout, process_logs), daemon=True)
        stderr_thread = threading.Thread(target=_read_process_stream, args=(process.stderr, process_logs), daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        try:
            deadline = time.time() + min(timeout, 8)
            while time.time() < deadline:
                if process.poll() is not None:
                    return None, f'sing-box 启动失败: {" | ".join(process_logs[-8:])}'

                exit_ip, error = _curl_ip(listen_port, ip_url, timeout)
                if exit_ip:
                    return exit_ip, None
                time.sleep(0.8)

            log_tail = ' | '.join(process_logs[-8:])
            if log_tail:
                return None, f'{error or "检测超时"}；sing-box: {log_tail}'
            return None, error or '检测超时'
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()


def main():
    parser = argparse.ArgumentParser(description='检测订阅节点的落地 IP')
    parser.add_argument('subscription_url', nargs='?', help='VPN/机场订阅链接；不填则读取 config.py 的 SUBSCRIPTION_URLS')
    parser.add_argument('--ip-url', default=DEFAULT_IP_CHECK_URL, help='查 IP 接口 URL')
    parser.add_argument('--timeout', type=int, default=12, help='每个节点检测超时时间，默认 12 秒')
    parser.add_argument('--limit', type=int, default=0, help='只检测前 N 个节点，0 表示全部')
    parser.add_argument('--output', default='exit_ips.txt', help='唯一落地 IP 输出文件')
    parser.add_argument('--details', default='exit_ip_results.json', help='详细结果输出文件')
    args = parser.parse_args()

    if not shutil.which('sing-box'):
        raise SystemExit('未找到 sing-box，请先安装并确认 sing-box version 可用。')
    if not shutil.which('curl'):
        raise SystemExit('未找到 curl，请先安装 curl。')

    subscription_urls = [args.subscription_url] if args.subscription_url else _load_subscription_urls_from_config()
    if not subscription_urls:
        raise SystemExit('没有可用订阅链接。请在 config.py 的 SUBSCRIPTION_URLS 中填写，或通过命令行传入订阅链接。')

    nodes = []
    errors = []
    for url in subscription_urls:
        parsed_nodes, parsed_errors = parse_subscription(url)
        nodes.extend(parsed_nodes)
        errors.extend([f'{url}: {error}' for error in parsed_errors])

    if args.limit > 0:
        nodes = nodes[:args.limit]

    print(f'读取订阅链接数: {len(subscription_urls)}')
    print(f'解析到 {len(nodes)} 个支持的节点。')
    for error in errors:
        print(error)

    results = []
    for index, node in enumerate(nodes, start=1):
        print(f'[{index}/{len(nodes)}] {node["protocol"]} {node["name"]} ({node["server"]}) ...', flush=True)
        exit_ip, error = detect_node_exit_ip(node, args.ip_url, args.timeout)
        result = {
            'name': node['name'],
            'protocol': node['protocol'],
            'server': node['server'],
            'exit_ip': exit_ip,
            'error': error,
        }
        results.append(result)
        if exit_ip:
            print(f'  -> {exit_ip}')
        else:
            print(f'  -> 失败: {error}')

    unique_ips = sorted({item['exit_ip'] for item in results if item.get('exit_ip')})
    with open(args.output, 'w', encoding='utf-8') as file:
        for ip in unique_ips:
            file.write(ip + '\n')

    with open(args.details, 'w', encoding='utf-8') as file:
        json.dump(results, file, ensure_ascii=False, indent=2)

    print(f'\n唯一落地 IP 数: {len(unique_ips)}')
    for ip in unique_ips:
        print(ip)
    print(f'\n已写入: {args.output}')
    print(f'详细结果: {args.details}')


if __name__ == '__main__':
    main()
