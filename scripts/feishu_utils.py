"""
飞书电子表格工具函数 — 直接 HTTP API 版本
不依赖 lark-cli，使用 urllib.request 直接调飞书 Open API。
认证方式：tenant_access_token（机器人身份），需配置 app_id + app_secret。

线程安全：不修改 os.environ 代理变量，改用无代理 opener，支持流水线并行写入。
"""

import json
import os
import ssl
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

# macOS Homebrew Python 缺少根证书，手动加载系统证书包
_SSL_CTX = ssl.create_default_context()
try:
    import certifi
    _SSL_CTX.load_verify_locations(certifi.where())
except ImportError:
    _SSL_CTX.load_verify_locations("/etc/ssl/cert.pem")

# 飞书国内站直连：用空 ProxyHandler，避免改 os.environ（线程不安全）
_NO_PROXY_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=_SSL_CTX),
    urllib.request.HTTPHandler(),
)

from config import SPREADSHEET_TOKEN, SHEET_ID

# ===== 飞书应用凭证 =====
_FEISHU_BASE = "https://open.feishu.cn"
_TOKEN_CACHE = {"token": None, "expires_at": 0}
_TOKEN_LOCK = threading.Lock()


def _get_app_credentials() -> tuple:
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        raise RuntimeError(
            "缺少飞书应用凭证。请在环境变量或项目 .env 文件中设置:\n"
            "  FEISHU_APP_ID=cli_xxxxx\n"
            "  FEISHU_APP_SECRET=xxxxx"
        )
    return app_id, app_secret


def _get_tenant_token() -> str:
    now = time.time()
    with _TOKEN_LOCK:
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
        with _NO_PROXY_OPENER.open(req, timeout=15) as resp:
            data = json.loads(resp.read())

        if data.get("code") != 0:
            raise RuntimeError(f"获取 tenant_access_token 失败: {data.get('msg', data)}")

        token = data.get("tenant_access_token")
        if not token:
            raise RuntimeError("获取 tenant_access_token 失败: 响应中缺少 token 字段")
        expire = data.get("expire", 7200)
        _TOKEN_CACHE["token"] = token
        _TOKEN_CACHE["expires_at"] = now + expire
        return token


def _http_request(method: str, path: str, body: Optional[dict] = None,
                  timeout: int = 30, max_retries: int = 3) -> dict:
    token = _get_tenant_token()
    url = f"{_FEISHU_BASE}{path}" if path.startswith("/") else path

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }

    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")

    last_err = None
    for attempt in range(max_retries):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with _NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
                resp_body = resp.read().decode("utf-8")
                if not resp_body:
                    return {}
                return json.loads(resp_body)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:400]
            if e.code == 429 or e.code >= 500:
                wait = 2 ** attempt
                print(f"[飞书] {method} {path} 返回 {e.code}，{wait}s 后重试 ({attempt+1}/{max_retries})")
                last_err = e
                time.sleep(wait)
                continue
            raise RuntimeError(
                f"飞书 API {method} {path} 返回 {e.code}:\n{err_body}"
            ) from e
        except urllib.error.URLError as e:
            wait = 2 ** attempt
            print(f"[飞书] 网络错误: {e.reason}，{wait}s 后重试 ({attempt+1}/{max_retries})")
            last_err = e
            time.sleep(wait)
            continue

    raise RuntimeError(f"飞书 API {method} {path} 重试 {max_retries} 次后仍失败: {last_err}")


# ===== 高层接口 =====


def read_sheet(range_str: str, token: str = SPREADSHEET_TOKEN,
               sheet_id: str = SHEET_ID) -> list:
    """读取飞书表格指定范围，返回二维数组。"""
    full_range = f"{sheet_id}!{range_str}" if "!" not in range_str else range_str
    encoded = urllib.parse.quote(full_range, safe="")
    path = f"/open-apis/sheets/v2/spreadsheets/{token}/values/{encoded}"
    data = _http_request("GET", path)
    return data.get("data", {}).get("valueRange", {}).get("values", [])


def get_column_map(token: str = SPREADSHEET_TOKEN,
                   sheet_id: str = SHEET_ID) -> dict:
    """
    动态读取表头行，返回 {列名: 0-based索引}。
    不再硬编码列位置 — 飞书表格里加列、调列顺序，代码自动适配。
    """
    from config import FIELD_NAMES
    rows = read_sheet("A1:Z1", token, sheet_id)
    if not rows:
        raise RuntimeError("飞书表格为空或无法读取表头")
    header = rows[0]

    col_map = {}
    for i, h in enumerate(header):
        h_str = str(h).strip() if h else ""
        if h_str:
            col_map[h_str] = i

    missing = [f for f in FIELD_NAMES if f not in col_map]
    if missing:
        print(f"[警告] 飞书表格缺少以下列: {', '.join(missing)}")

    return col_map


def _col_letter(idx: int) -> str:
    """将 0-based 列索引转为飞书列字母 (0→A, 25→Z, 26→AA, ...)。"""
    result = ""
    while True:
        result = chr(ord("A") + idx % 26) + result
        idx = idx // 26 - 1
        if idx < 0:
            break
    return result


def get_last_row(token: str = SPREADSHEET_TOKEN, sheet_id: str = SHEET_ID) -> int:
    """获取表格当前最后一行行号（1-indexed）。分页读取，不受 500 行限制。"""
    batch = 500
    offset = 0
    while True:
        start_letter = "A"
        end_letter = "Z"
        range_str = f"{start_letter}{offset + 1}:{end_letter}{offset + batch}"
        values = read_sheet(range_str, token, sheet_id)
        if not values:
            return offset

        last_nonempty = -1
        for i, row in enumerate(values):
            if any(c for c in row if c):
                last_nonempty = i

        if last_nonempty >= 0:
            offset += last_nonempty + 1
            if len(values) < batch:
                return offset
        else:
            return offset


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
    """追加行到表格末尾。注意：并发 append 可能乱序，pipeline 侧串行化写入。"""
    body = {"valueRange": {"range": sheet_id, "values": values}}
    return _http_request(
        "POST",
        f"/open-apis/sheets/v2/spreadsheets/{token}/values_append",
        body
    )


def write_row_fields(token: str, sheet_id: str, row_num: int,
                     field_values: dict, col_map: dict) -> dict:
    """
    写入某一行的指定字段。
    field_values: {列名(str): 值}，按列名定位，不依赖硬编码索引。
    col_map: get_column_map() 的返回值。
    """
    if not field_values:
        return {}

    num_cols = max(col_map.values()) + 1
    row = [""] * num_cols

    for field_name, value in field_values.items():
        idx = col_map.get(field_name)
        if idx is not None:
            row[idx] = value

    non_empty = [i for i, v in enumerate(row) if v]
    if not non_empty:
        return {}

    start = min(non_empty)
    end = max(non_empty)
    start_col = _col_letter(start)
    end_col = _col_letter(end)
    range_str = f"{start_col}{row_num}:{end_col}{row_num}"
    vals = [row[start:end + 1]]

    return write_cells(token, sheet_id, range_str, vals)


if __name__ == "__main__":
    print("测试动态列映射...")
    col_map = get_column_map()
    print(f"列映射: {col_map}")
    print(f"\n测试读取最后一行...")
    last = get_last_row()
    print(f"当前表格最后一行: {last}")
