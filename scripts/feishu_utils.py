"""
飞书电子表格工具函数 — 直接 HTTP API 版本
不依赖 lark-cli，使用 urllib.request 直接调飞书 Open API。
认证方式：tenant_access_token（机器人身份），需配置 app_id + app_secret。
"""

import json
import os
import ssl
import time
import urllib.parse
import urllib.request
from typing import Optional

# macOS Homebrew Python 缺少根证书，手动加载系统证书包
_SSL_CTX = ssl.create_default_context()
try:
    import certifi
    _SSL_CTX.load_verify_locations(certifi.where())
except ImportError:
    # Homebrew Python: 直接加载 macOS 系统证书包
    _SSL_CTX.load_verify_locations("/etc/ssl/cert.pem")

# 飞书是国内站，直连不需要代理。代理会劫持 HTTPS 导致证书验证失败。
_PROXY_KEYS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")
_saved_proxy = {}

def _unset_proxy():
    """临时清除代理环境变量，返回被清除的值以便恢复。"""
    global _saved_proxy
    _saved_proxy = {}
    for k in _PROXY_KEYS:
        v = os.environ.pop(k, None)
        if v is not None:
            _saved_proxy[k] = v

def _restore_proxy():
    """恢复之前清除的代理环境变量。"""
    os.environ.update(_saved_proxy)

from config import SPREADSHEET_TOKEN, SHEET_ID

# ===== 飞书应用凭证 =====
# 优先从环境变量读取，其次从项目 .env 文件读取
_FEISHU_BASE = "https://open.feishu.cn"
_TOKEN_CACHE = {"token": None, "expires_at": 0}


def _load_env_file():
    """从项目 .env 文件加载环境变量（不覆盖已有环境变量）。"""
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    env_path = os.path.normpath(env_path)
    if not os.path.isfile(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = val


_load_env_file()


def _get_app_credentials() -> tuple:
    """获取飞书应用的 app_id 和 app_secret。"""
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        raise RuntimeError(
            "缺少飞书应用凭证。请在环境变量或项目 .env 文件中设置:\n"
            "  FEISHU_APP_ID=cli_xxxxx\n"
            "  FEISHU_APP_SECRET=xxxxx\n"
            "获取方式：飞书开放平台 → 你的应用 → 凭证与基础信息"
        )
    return app_id, app_secret


def _get_tenant_token() -> str:
    """获取 tenant_access_token（机器人令牌），自动缓存和刷新。"""
    now = time.time()
    if _TOKEN_CACHE["token"] and _TOKEN_CACHE["expires_at"] > now + 60:
        return _TOKEN_CACHE["token"]

    app_id, app_secret = _get_app_credentials()
    url = f"{_FEISHU_BASE}/open-apis/auth/v3/tenant_access_token/internal"
    payload = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST"
    )
    _unset_proxy()
    try:
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
            data = json.loads(resp.read())
    finally:
        _restore_proxy()

    if data.get("code") != 0:
        raise RuntimeError(f"获取 tenant_access_token 失败: {data.get('msg', data)}")

    token = data["tenant_access_token"]
    expire = data.get("expire", 7200)
    _TOKEN_CACHE["token"] = token
    _TOKEN_CACHE["expires_at"] = now + expire
    return token


def _http_request(method: str, path: str, body: Optional[dict] = None,
                  timeout: int = 120) -> dict:
    """发送飞书 API HTTP 请求。"""
    token = _get_tenant_token()
    url = f"{_FEISHU_BASE}{path}" if path.startswith("/") else path

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }

    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        _unset_proxy()
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            resp_body = resp.read().decode("utf-8")
            if not resp_body:
                return {}
            return json.loads(resp_body)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(
            f"飞书 API {method} {path} 返回 {e.code}:\n{err_body}"
        ) from e
    finally:
        _restore_proxy()


# ===== 高层接口（与 pipeline.py 兼容，签名不变）=====


def read_sheet(range_str: str, token: str = SPREADSHEET_TOKEN,
               sheet_id: str = SHEET_ID) -> list:
    """读取飞书表格指定范围，返回二维数组。"""
    full_range = f"{sheet_id}!{range_str}" if "!" not in range_str else range_str
    encoded = urllib.parse.quote(full_range, safe="")
    path = f"/open-apis/sheets/v2/spreadsheets/{token}/values/{encoded}"
    data = _http_request("GET", path)
    return data.get("data", {}).get("valueRange", {}).get("values", [])


def get_last_row(token: str = SPREADSHEET_TOKEN, sheet_id: str = SHEET_ID) -> int:
    """获取表格当前最后一行行号（1-indexed）。"""
    values = read_sheet("A1:R500", token, sheet_id)
    # 从末尾去掉全空行
    while values and not any(c for c in values[-1] if c):
        values.pop()
    return len(values)


def write_cells(token: str, sheet_id: str, range_str: str, values: list) -> dict:
    """覆盖写入指定范围的单元格。range_str 不含 sheet_id 前缀。"""
    full_range = f"{sheet_id}!{range_str}"
    body = {"valueRange": {"range": full_range, "values": values}}
    return _http_request(
        "PUT",
        f"/open-apis/sheets/v2/spreadsheets/{token}/values",
        body
    )


def append_rows(token: str, sheet_id: str, values: list) -> dict:
    """追加行到表格末尾。"""
    body = {"valueRange": {"range": sheet_id, "values": values}}
    return _http_request(
        "POST",
        f"/open-apis/sheets/v2/spreadsheets/{token}/values_append",
        body
    )


def write_row_fields(token: str, sheet_id: str, row_num: int,
                     field_values: dict) -> dict:
    """
    写入某一行的指定字段。
    field_values: {列索引(0-based): 值}，只写有值的列。
    """
    if not field_values:
        return {}

    cols = sorted(field_values.keys())
    # 将连续列合并为 range 写入
    groups = []
    current_group = [cols[0]]
    for c in cols[1:]:
        if c == current_group[-1] + 1:
            current_group.append(c)
        else:
            groups.append(current_group)
            current_group = [c]
    groups.append(current_group)

    results = {}
    for group in groups:
        start_col = chr(ord("A") + group[0])
        end_col = chr(ord("A") + group[-1])
        range_str = f"{start_col}{row_num}:{end_col}{row_num}"
        vals = [[field_values[c] for c in group]]
        resp = write_cells(token, sheet_id, range_str, vals)
        results[range_str] = resp

    return results


if __name__ == "__main__":
    # 快速测试
    print("测试读取飞书表格...")
    last = get_last_row()
    print(f"当前表格最后一行: {last}")
    # 读取前两行验证
    rows = read_sheet("A1:R2")
    print(f"读取到 {len(rows)} 行")
    if rows:
        print(f"表头: {rows[0][:5]}...")
