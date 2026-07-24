#!/usr/bin/env python3
"""
自动检测本机系统代理，写入视频号工具的 config.yaml（upstreamProxy 字段）。

- Windows：读注册表 HKCU\\...\\Internet Settings\\ProxyServer
- macOS：解析 scutil --proxy 输出
- 检测不到代理时写空字符串（直连，不影响视频号采集）

用法：
    python scripts/detect_proxy.py          # 检测并写入
    python scripts/detect_proxy.py --dry    # 只打印检测结果，不写文件
"""

import os
import re
import subprocess
import sys

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPTS_DIR)
CONFIG_YAML = os.path.join(PROJECT_DIR, "tools", "wx_channels_download", "config.yaml")


def detect_proxy_windows() -> str:
    """从 Windows 注册表读取系统代理。返回 'host:port' 或空字符串。"""
    try:
        import winreg
    except ImportError:
        return ""

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
        enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
        if not enable:
            winreg.CloseKey(key)
            return ""
        server, _ = winreg.QueryValueEx(key, "ProxyServer")
        winreg.CloseKey(key)
    except OSError:
        return ""

    if not server:
        return ""

    # ProxyServer 格式可能是：
    #   "127.0.0.1:7890"
    #   "http=127.0.0.1:7890;https=127.0.0.1:7890;socks=127.0.0.1:7891"
    if "=" in server:
        # 取 http= 或 https= 后面的地址，没有就取第一个
        parts = dict(
            seg.split("=", 1) for seg in server.split(";") if "=" in seg
        )
        addr = parts.get("http") or parts.get("https") or next(iter(parts.values()), "")
    else:
        addr = server

    return addr.strip()


def detect_proxy_macos() -> str:
    """从 macOS scutil --proxy 解析系统代理。返回 'host:port' 或空字符串。"""
    try:
        out = subprocess.check_output(["scutil", "--proxy"], text=True, timeout=5)
    except (subprocess.SubprocessError, FileNotFoundError):
        return ""

    # 优先 HTTP 代理，其次 SOCKS
    http_enable = re.search(r"HTTPEnable\s*:\s*1", out)
    if http_enable:
        host_m = re.search(r"HTTPProxy\s*:\s*(\S+)", out)
        port_m = re.search(r"HTTPPort\s*:\s*(\d+)", out)
        if host_m and port_m:
            return f"{host_m.group(1)}:{port_m.group(1)}"

    socks_enable = re.search(r"SOCKSEnable\s*:\s*1", out)
    if socks_enable:
        host_m = re.search(r"SOCKSProxy\s*:\s*(\S+)", out)
        port_m = re.search(r"SOCKSPort\s*:\s*(\d+)", out)
        if host_m and port_m:
            return f"{host_m.group(1)}:{port_m.group(1)}"

    return ""


def detect_proxy() -> str:
    """跨平台入口：返回 'host:port' 或空字符串。"""
    if os.name == "nt":
        return detect_proxy_windows()
    elif sys.platform == "darwin":
        return detect_proxy_macos()
    # Linux 等其他平台：尝试读环境变量
    for var in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        val = os.environ.get(var, "")
        if val:
            # 去掉协议前缀 http:// 或 socks5://
            addr = re.sub(r"^[a-z0-9+]+://", "", val).rstrip("/")
            return addr
    return ""


def write_config(proxy_addr: str) -> None:
    """把检测到的代理写入 config.yaml 的 upstreamProxy 字段。"""
    if not os.path.isfile(CONFIG_YAML):
        print(f"[!] 未找到 {CONFIG_YAML}，跳过写入")
        return

    with open(CONFIG_YAML, "r", encoding="utf-8") as f:
        content = f.read()

    if proxy_addr:
        new_value = f'"http://{proxy_addr}"'
    else:
        new_value = '""'

    # 替换 upstreamProxy 行（保留缩进）
    pattern = r'(\s*upstreamProxy:\s*)"[^"]*"'
    if re.search(pattern, content):
        content = re.sub(pattern, rf'\g<1>{new_value}', content)
    else:
        # 如果没找到（极端情况），在 proxy: 块末尾追加
        content = content.rstrip("\n") + f"\n  upstreamProxy: {new_value}\n"

    with open(CONFIG_YAML, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"[OK] upstreamProxy 已更新为 {new_value}")


def main():
    dry_run = "--dry" in sys.argv

    proxy_addr = detect_proxy()

    if proxy_addr:
        print(f"[i] 检测到系统代理：{proxy_addr}")
    else:
        print("[i] 未检测到系统代理，将使用直连模式（不影响视频号采集）")

    if dry_run:
        print("[dry-run] 不写入文件")
        return

    write_config(proxy_addr)


if __name__ == "__main__":
    main()
