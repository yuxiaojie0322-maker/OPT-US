#!/usr/bin/env python3
"""
从 VLESS 链接生成 Xray 配置文件
"""

import urllib.parse
import json
import os
import sys

def parse_vless(vless_url):
    """解析 VLESS 链接"""
    if not vless_url or not vless_url.startswith('vless://'):
        print("❌ 无效的 VLESS 链接", file=sys.stderr)
        sys.exit(1)
    
    # 移除协议头
    url = vless_url[8:]
    
    # 提取节点备注名称
    if '#' in url:
        url, name = url.split('#', 1)
        name = urllib.parse.unquote(name)
    else:
        name = "VLESS-Node"
    
    # 分离 UUID 和地址部分
    if '@' not in url:
        print("❌ 缺少 @ 分隔符", file=sys.stderr)
        sys.exit(1)
    uuid, rest = url.split('@', 1)
    
    # 分离地址端口和查询参数
    if '?' in rest:
        address_port, query = rest.split('?', 1)
        params = dict(urllib.parse.parse_qsl(query))
    else:
        address_port = rest
        params = {}
    
    # 分离地址和端口（支持 IPv6 [::1]:443 与常规 domain:port）
    if address_port.startswith('[') and ']' in address_port:
        address, rest_port = address_port.split(']', 1)
        address = address + ']'
        if rest_port.startswith(':'):
            port = int(rest_port[1:])
        else:
            port = 443
    elif ':' in address_port:
        address, port = address_port.rsplit(':', 1)
        port = int(port)
    else:
        address = address_port
        port = 443

    security = (params.get('security') or '').strip().lower()
    if not security:
        security = 'none'

    network = (params.get('type') or 'tcp').strip().lower()
    ws_host = params.get('host') or address
    sni = params.get('sni') or address
    ws_path = params.get('path') or '/'
    
    return {
        "name": name,
        "uuid": uuid.strip(),
        "address": address.strip(),
        "port": port,
        "encryption": params.get('encryption', 'none'),
        "security": security,
        "network": network,
        "ws_path": ws_path,
        "ws_host": ws_host,
        "sni": sni,
        "fingerprint": params.get('fp', 'chrome'),
        "flow": params.get('flow', ''),
        "pbk": params.get('pbk', ''),
        "sid": params.get('sid', ''),
        "raw": vless_url
    }

def generate_xray_config(vless_url):
    """生成 Xray 配置文件"""
    node = parse_vless(vless_url)
    
    config = {
        "log": {
            "loglevel": "warning"
        },
        "inbounds": [
            {
                "listen": "127.0.0.1",
                "port": 10808,
                "protocol": "socks",
                "settings": {
                    "auth": "noauth",
                    "udp": True
                },
                "streamSettings": {
                    "network": "tcp"
                }
            }
        ],
        "outbounds": [
            {
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {
                            "address": node["address"],
                            "port": node["port"],
                            "users": [
                                {
                                    "id": node["uuid"],
                                    "encryption": node["encryption"]
                                }
                            ]
                        }
                    ]
                },
                "streamSettings": {
                    "network": node["network"],
                    "security": node["security"]
                }
            }
        ]
    }
    
    # 添加 flow（XTLS / Vision）
    if node["flow"]:
        config["outbounds"][0]["settings"]["vnext"][0]["users"][0]["flow"] = node["flow"]
    
    # 传输层配置
    stream = config["outbounds"][0]["streamSettings"]
    if node["network"] == "ws":
        stream["wsSettings"] = {
            "path": node["ws_path"],
            "headers": {
                "Host": node["ws_host"]
            }
        }
    elif node["network"] == "grpc":
        stream["grpcSettings"] = {
            "serviceName": node["ws_path"].lstrip("/") if node["ws_path"] else ""
        }
    
    # 安全层配置
    if node["security"] == "tls":
        stream["tlsSettings"] = {
            "serverName": node["sni"],
            "fingerprint": node["fingerprint"],
            "allowInsecure": False
        }
    elif node["security"] == "reality":
        reality = {
            "serverName": node["sni"],
            "fingerprint": node["fingerprint"],
            "publicKey": node.get("pbk", "")
        }
        if node.get("sid"):
            reality["shortId"] = node["sid"]
        stream["realitySettings"] = reality
    
    return config

def main():
    vless_url = os.environ.get('VLESS_NODE', '').strip()
    
    if not vless_url:
        print("❌ 错误: 未设置 VLESS_NODE 环境变量", file=sys.stderr)
        sys.exit(1)
    
    try:
        config = generate_xray_config(vless_url)
        print(json.dumps(config, indent=2))
    except Exception as e:
        print(f"❌ 生成配置失败: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
