#!/usr/bin/env python3
"""
飞书电子表格写入工具
用法:
    python3 write_to_feishu.py --data data/content-library.json --token <spreadsheet_token> --sheet-id <sheet_id> --range A2:N81
    python3 write_to_feishu.py --data data/content-library.json --token <spreadsheet_token> --sheet-id <sheet_id> --append

核心 workaround：lark-cli api --data 参数中的空格会被 cobra 解析器拆成多个 argv，
解决办法是将 JSON 中所有空格替换为 \\u0020 Unicode escape。
"""

import json
import subprocess
import sys
import argparse


def sanitize_json_for_larkcli(body: str) -> str:
    """将 JSON 字符串中的空格替换为 \\u0020，绕过 lark-cli cobra 参数解析 bug。"""
    return body.replace(' ', '\\u0020')


def write_to_sheet(token: str, sheet_id: str, range_str: str, values: list, identity: str = "user") -> dict:
    """
    写入数据到飞书电子表格。
    
    Args:
        token: spreadsheet token
        sheet_id: 工作表 ID
        range_str: 写入范围 (如 A2:N81)，不含 sheet_id 前缀
        values: 二维数组
        identity: user 或 bot
    
    Returns:
        API 响应 dict
    """
    full_range = f"{sheet_id}!{range_str}"
    body = json.dumps(
        {"valueRange": {"range": full_range, "values": values}},
        ensure_ascii=False, separators=(',', ':')
    )
    body = sanitize_json_for_larkcli(body)
    
    result = subprocess.run(
        ["lark-cli", "api", "PUT",
         f"/open-apis/sheets/v2/spreadsheets/{token}/values",
         "--data", body, "--as", identity],
        capture_output=True, text=True, timeout=120
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"lark-cli 写入失败: {result.stderr[:500]}")
    
    return json.loads(result.stdout)


def append_to_sheet(token: str, sheet_id: str, values: list, identity: str = "user") -> dict:
    """
    追加数据到飞书电子表格末尾。
    
    Args:
        token: spreadsheet token
        sheet_id: 工作表 ID
        values: 二维数组
        identity: user 或 bot
    
    Returns:
        API 响应 dict
    """
    body = json.dumps(
        {"valueRange": {"range": f"{sheet_id}", "values": values}},
        ensure_ascii=False, separators=(',', ':')
    )
    body = sanitize_json_for_larkcli(body)
    
    result = subprocess.run(
        ["lark-cli", "api", "POST",
         f"/open-apis/sheets/v2/spreadsheets/{token}/values_append",
         "--data", body, "--as", identity],
        capture_output=True, text=True, timeout=120
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"lark-cli 追加失败: {result.stderr[:500]}")
    
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description="写入数据到飞书电子表格")
    parser.add_argument("--data", required=True, help="JSON 数据文件路径 (二维数组)")
    parser.add_argument("--token", required=True, help="飞书电子表格 spreadsheet token")
    parser.add_argument("--sheet-id", required=True, help="工作表 ID")
    parser.add_argument("--range", dest="range_str", help="写入范围 (如 A2:N81)，覆盖模式")
    parser.add_argument("--append", action="store_true", help="追加模式 (写到末尾)")
    parser.add_argument("--as", dest="identity", default="user", choices=["user", "bot"], help="身份类型")
    parser.add_argument("--dry-run", action="store_true", help="仅预览不执行")
    
    args = parser.parse_args()
    
    if not args.range_str and not args.append:
        parser.error("必须指定 --range 或 --append")
    
    # 读取数据
    with open(args.data) as f:
        data = json.load(f)
    
    print(f"数据: {len(data)} 行 × {len(data[0]) if data else 0} 列")
    
    if args.dry_run:
        print(f"[DRY-RUN] Token: {args.token}")
        print(f"[DRY-RUN] Sheet ID: {args.sheet_id}")
        if args.range_str:
            print(f"[DRY-RUN] Range: {args.sheet_id}!{args.range_str}")
        else:
            print(f"[DRY-RUN] Mode: append")
        return
    
    if args.append:
        resp = append_to_sheet(args.token, args.sheet_id, data, args.identity)
        print(f"追加成功: {resp['data']['updatedRange']}")
    else:
        resp = write_to_sheet(args.token, args.sheet_id, args.range_str, data, args.identity)
        print(f"写入成功: {resp['data']['updatedRange']}")
    
    print(f"  行数: {resp['data']['updatedRows']}")
    print(f"  列数: {resp['data']['updatedColumns']}")
    print(f"  单元格: {resp['data']['updatedCells']}")


if __name__ == "__main__":
    main()
