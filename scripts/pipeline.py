#!/usr/bin/env python3
"""
爆款内容库 Pipeline - 采集→转写→分类→写入飞书

用法:
    python pipeline.py crawl --keywords "遗嘱怎么写" --count 10
    python pipeline.py import --file <json> [--count N]   # 转写+分类+一次性写入
    python pipeline.py full --keywords "遗嘱怎么写" --count 5
    python pipeline.py process [--limit N]                # 重试无文案的行
    python pipeline.py crawl --type creator --creator-id "MS4wLjABAAA..."
    python pipeline.py transcribe --url "https://..."
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    SPREADSHEET_TOKEN, SHEET_ID, MEDIACRAWLER_DIR, VENV_PYTHON,
    DATA_DIR, FIELD_NAMES, extract_hashtags,
)
from feishu_utils import (
    append_rows, write_row_fields, get_last_row, get_column_map, _col_letter,
)


# ==================== 状态管理 ====================

def load_state(path: str) -> dict:
    if Path(path).exists():
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"[警告] 状态文件损坏，从空白状态开始: {e}")
    return {"items": {}, "last_import_row": 0}


def save_state(state: dict, path: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, 'w') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


DEFAULT_STATE = f"{DATA_DIR}/pipeline_state.json"


# ==================== 行数据构建 (按列名，不依赖位置) ====================

def build_row_data(item: dict, transcript: str = "", classification: dict = None,
                   platform: str = "抖音") -> dict:
    """
    构建完整行数据 dict，key 是列名。
    传入 get_column_map() 后就知道写到哪一列，列顺序变了也不用改代码。
    """
    raw_title = item.get("title", "") or item.get("desc", "")
    clean_title, hashtags = extract_hashtags(raw_title)

    data = {}
    data["标题"] = clean_title
    data["平台"] = platform
    data["原始链接"] = item.get("aweme_url", "")
    data["创作者"] = item.get("nickname", "")
    data["点赞"] = item.get("liked_count", "")
    data["收藏"] = item.get("collected_count", "")
    data["转发"] = item.get("share_count", "")
    data["评论"] = item.get("comment_count", "")
    data["入库日期"] = str(date.today())
    data["原视频标签"] = ", ".join(hashtags) if hashtags else ""

    if transcript:
        data["文案全文"] = transcript

    if classification:
        for field_name in ["业务方向", "细分领域", "内容形式", "选题方向",
                           "内容切入角度", "目标人群", "情绪钩子", "特征标签"]:
            val = classification.get(field_name, "")
            if val:
                data[field_name] = val

    return data


# ==================== 采集 ====================

def cmd_crawl(args):
    """运行 MediaCrawler 采集抖音视频，返回 JSON 文件路径。"""
    crawl_type = args.type
    output_dir = f"{MEDIACRAWLER_DIR}/data/douyin/json"
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    cmd = [VENV_PYTHON, "main.py", "--platform", "dy"]

    if crawl_type == "search":
        if not args.keywords:
            print("搜索采集需要 --keywords 参数")
            sys.exit(1)
        cmd += ["--type", "search",
                "--keywords", args.keywords,
                "--crawler_max_notes_count", str(args.count)]
    elif crawl_type == "creator":
        if not args.creator_id:
            print("主页采集需要 --creator_id 参数")
            sys.exit(1)
        cmd += ["--type", "creator",
                "--creator_id", args.creator_id]
    else:
        print(f"未知采集类型: {crawl_type}")
        sys.exit(1)

    cmd += ["--save_data_option", "json",
            "--save_data_path", "./data",
            "--headless", "false",
            "--get_comment", "false"]

    print(f"[采集] cd {MEDIACRAWLER_DIR} && {' '.join(cmd)}")

    env = os.environ.copy()
    for var in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"]:
        env.pop(var, None)

    result = subprocess.run(cmd, cwd=MEDIACRAWLER_DIR, env=env)

    if result.returncode != 0:
        print(f"[采集] 退出码: {result.returncode}")
        sys.exit(1)

    json_files = sorted(Path(output_dir).glob("*.json"),
                        key=os.path.getmtime, reverse=True)
    if json_files:
        print(f"\n[采集] 输出文件: {json_files[0]}")
        with open(json_files[0]) as f:
            items = json.load(f)
        print(f"[采集] 共 {len(items)} 条视频")
        return str(json_files[0])
    else:
        print("[采集] 未找到输出 JSON 文件")
        return None


# ==================== 导入 (先转写，再一次性写入) ====================

def cmd_import(args):
    """
    导入流程：
    1. 读取采集 JSON，去重
    2. 逐条：下载视频 → 转写文案 → AI分类
    3. 全部完成后一次性写入飞书（每行都是完整的，含文案和分类）
    """
    with open(args.file) as f:
        items = json.load(f)

    print(f"[导入] 读取 {len(items)} 条数据")

    state = load_state(args.state)

    # 动态读取飞书列映射（不再硬编码列位置）
    col_map = get_column_map()
    print(f"[导入] 飞书列映射: {len(col_map)}列")

    last_row = get_last_row()
    print(f"[导入] 当前最后一行: {last_row}")

    # 去重
    existing_ids = set(state.get("items", {}).keys())
    new_items = [it for it in items if it.get("aweme_id") not in existing_ids]
    skipped = len(items) - len(new_items)
    if skipped:
        print(f"[导入] 跳过 {skipped} 条重复数据")
    if not new_items:
        print("[导入] 没有新数据")
        return state

    count = args.count if hasattr(args, 'count') and args.count else len(new_items)
    new_items = new_items[:count]

    # 延迟导入（只在需要时加载）
    from transcribe import process_video, stop_daemon
    from ai_classify import classify_item

    success = 0
    next_row = last_row + 1

    try:
        for i, item in enumerate(new_items):
            aweme_id = item.get("aweme_id") or f"unknown_{i}"
            raw_title = item.get("title", "") or item.get("desc", "")
            clean_title, hashtags = extract_hashtags(raw_title)

            print(f"\n{'='*60}")
            print(f"[导入 {i+1}/{len(new_items)}] 行{next_row}: {clean_title[:50]}...")
            if hashtags:
                print(f"[标签] {', '.join(hashtags)}")

            # Step 1: 转写
            video_url = item.get("video_download_url", "")
            transcript = ""
            if video_url:
                try:
                    vr = process_video(video_url)
                    transcript = vr.get("transcript", "")
                    if transcript:
                        print(f"[转写] {len(transcript)}字")
                    else:
                        print("[转写] 无结果")
                except Exception as e:
                    print(f"[转写] 失败: {e}")
            else:
                print("[转写] 无视频URL，跳过")

            # Step 2: 分类（必须有文案才分类）
            classification = None
            if transcript:
                try:
                    classification = classify_item(
                        title=clean_title,
                        desc=item.get("desc", ""),
                        transcript=transcript,
                    )
                    if classification and classification.get("业务方向"):
                        print(f"[分类] {classification['业务方向']}")
                except Exception as e:
                    print(f"[分类] 异常: {e}")

            # Step 3: 构建完整行数据并立即写入飞书
            row_data = build_row_data(item, transcript, classification)
            num_cols = max(col_map.values()) + 1
            row_arr = [""] * num_cols
            for field_name, value in row_data.items():
                idx = col_map.get(field_name)
                if idx is not None:
                    row_arr[idx] = value

            write_ok = False
            try:
                append_rows(SPREADSHEET_TOKEN, SHEET_ID, [row_arr])
                print(f"[写入] 行{next_row} 已写入飞书 ({len(row_data)}个字段)")
                write_ok = True
            except Exception as e:
                print(f"[写入] 行{next_row} 写入失败: {e}")

            # 只有写入成功才标记完成，否则下次 process 可以重试
            state["items"][aweme_id] = {
                "row": next_row if write_ok else 0,
                "video_url": video_url,
                "title": raw_title,
                "desc": item.get("desc", ""),
                "transcript": transcript,
                "classified": classification is not None,
                "processed": write_ok and bool(transcript),
            }
            if write_ok:
                state["last_import_row"] = next_row
                next_row += 1
                if transcript:
                    success += 1
            else:
                print(f"[警告] 行未写入，下次 process 命令会重试")

            # 每条写完就保存状态（防中断丢失）
            save_state(state, args.state)
            time.sleep(0.5)

    finally:
        stop_daemon()

    print(f"\n[导入] 完成: {success}/{len(new_items)} 条有文案")
    return state


# ==================== 重试 (补转无文案的行) ====================

def cmd_process(args):
    """
    重试无文案的条目：
    - 优先从 state 文件找 processed=false 的项
    - 如果 state 没有待处理项，则扫飞书表格找有链接但无文案的行
    """
    state = load_state(args.state)

    # 动态列映射
    col_map = get_column_map()

    # 1. 先从 state 找
    items = state.get("items", {})
    pending = {k: v for k, v in items.items()
               if not v.get("processed") and v.get("video_url")}

    # 2. state 没有就扫飞书表格
    if not pending:
        print("[处理] state 中无待处理项，扫描飞书表格...")
        last_row = get_last_row()
        if last_row > 1:
            all_rows = read_sheet_safe(f"A1:{_col_letter(max(col_map.values()))}{last_row}")
            if all_rows and len(all_rows) > 1:
                header = all_rows[0]
                url_col = col_map.get("原始链接")
                transcript_col = col_map.get("文案全文")
                title_col = col_map.get("标题")

                for r_idx, row in enumerate(all_rows[1:], start=2):
                    while len(row) <= max(url_col or 0, transcript_col or 0, title_col or 0):
                        row.append("")

                    url = str(row[url_col]) if url_col is not None and row[url_col] else ""
                    has_text = transcript_col is not None and row[transcript_col]
                    title = str(row[title_col]) if title_col is not None and row[title_col] else ""

                    if url and not has_text:
                        pending[f"row_{r_idx}"] = {
                            "row": r_idx,
                            "video_url": url,
                            "title": title,
                            "transcript": "",
                        }

    if not pending:
        print("[处理] 没有需要重试的条目")
        return

    limit = args.limit if hasattr(args, 'limit') and args.limit else len(pending)
    print(f"[处理] 共 {len(pending)} 条待处理，本次处理 {min(limit, len(pending))} 条")

    from transcribe import process_video, stop_daemon
    from ai_classify import classify_item

    count = 0
    for aweme_id, info in pending.items():
        if count >= limit:
            break
        count += 1

        row_num = info["row"]
        video_url = info["video_url"]
        raw_title = info.get("title", "")
        clean_title, hashtags = extract_hashtags(raw_title)

        print(f"\n{'='*60}")
        print(f"[处理 {count}/{min(limit, len(pending))}] 行{row_num}: {clean_title[:50]}...")

        field_values = {}
        if hashtags:
            field_values["标题"] = clean_title
            field_values["原视频标签"] = ", ".join(hashtags)

        # 转写
        transcript = info.get("transcript", "")
        if not transcript and video_url:
            try:
                vr = process_video(video_url)
                transcript = vr.get("transcript", "")
                if transcript:
                    field_values["文案全文"] = transcript
                    info["transcript"] = transcript
                    print(f"[转写] {len(transcript)}字")
                else:
                    print("[转写] 无结果")
            except Exception as e:
                print(f"[转写] 失败: {e}")

        if not transcript:
            print("[处理] 无文案，跳过")
            save_state(state, args.state)
            continue

        # 分类
        if not info.get("classified"):
            try:
                cls = classify_item(
                    title=clean_title,
                    desc=info.get("desc", ""),
                    transcript=transcript,
                )
                if cls and cls.get("业务方向"):
                    for fn in ["业务方向", "细分领域", "内容形式", "选题方向",
                               "内容切入角度", "目标人群", "情绪钩子", "特征标签"]:
                        val = cls.get(fn, "")
                        if val:
                            field_values[fn] = val
                    info["classified"] = True
                    print(f"[分类] {cls['业务方向']}")
            except Exception as e:
                print(f"[分类] 异常: {e}")

        # 写回飞书
        if field_values:
            try:
                write_row_fields(SPREADSHEET_TOKEN, SHEET_ID, row_num,
                                 field_values, col_map)
                print(f"[处理] 已写入行{row_num}: {len(field_values)}个字段")
            except Exception as e:
                print(f"[处理] 写入失败: {e}")

        info["processed"] = True
        save_state(state, args.state)
        time.sleep(1)

    stop_daemon()
    print(f"\n[处理] 完成，处理了 {count} 条")


def read_sheet_safe(range_str: str) -> list:
    """安全读取飞书表格，出错返回空列表。"""
    try:
        from feishu_utils import read_sheet
        return read_sheet(range_str)
    except Exception as e:
        print(f"[警告] 读取飞书表格失败: {e}")
        return []


# ==================== 完整流程 ====================

def cmd_full(args):
    """完整流程: 采集 → 转写+分类 → 写入飞书。"""
    print("=" * 60)
    print("[Full] Step 1/2: 采集")
    json_file = cmd_crawl(args)
    if not json_file:
        print("[Full] 采集失败，终止")
        return

    print("\n" + "=" * 60)
    print("[Full] Step 2/2: 转写+分类+写入飞书")
    args.file = json_file
    cmd_import(args)

    print("\n" + "=" * 60)
    print("[Full] 全部完成!")


# ==================== 单独转写 ====================

def cmd_transcribe(args):
    from transcribe import process_video, stop_daemon
    result = process_video(args.url)
    if result["success"]:
        print(f"\n--- 转写结果 ---\n{result['transcript']}")
    else:
        print("[转写] 失败")
    stop_daemon()


# ==================== 入口 ====================

def main():
    parser = argparse.ArgumentParser(
        description="爆款内容库 Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s crawl --keywords "遗嘱怎么写,立遗嘱" --count 10
  %(prog)s import --file data/douyin/json/search_contents_2026-07-18.json --count 5
  %(prog)s full --keywords "遗嘱怎么写" --count 5
  %(prog)s process --limit 5
  %(prog)s crawl --type creator --creator-id "MS4wLjABAAA..."
  %(prog)s transcribe --url "https://..."
        """
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_crawl = sub.add_parser("crawl", help="采集抖音视频")
    p_crawl.add_argument("--type", choices=["search", "creator"], default="search")
    p_crawl.add_argument("--keywords", help="搜索关键词，逗号分隔")
    p_crawl.add_argument("--creator-id", help="创作者 sec_uid 或主页 URL")
    p_crawl.add_argument("--count", type=int, default=10, help="最大采集数")

    p_import = sub.add_parser("import", help="转写+分类+写入飞书")
    p_import.add_argument("--file", required=True, help="MediaCrawler 输出的 JSON 文件")
    p_import.add_argument("--state", default=DEFAULT_STATE, help="状态文件路径")
    p_import.add_argument("--count", type=int, help="最多处理条数")

    p_process = sub.add_parser("process", help="重试无文案的行")
    p_process.add_argument("--state", default=DEFAULT_STATE, help="状态文件路径")
    p_process.add_argument("--limit", type=int, help="最多处理条数")

    p_full = sub.add_parser("full", help="完整流程: 采集→转写→写入")
    p_full.add_argument("--type", choices=["search", "creator"], default="search")
    p_full.add_argument("--keywords", help="搜索关键词")
    p_full.add_argument("--creator-id", help="创作者 sec_uid 或主页 URL")
    p_full.add_argument("--count", type=int, default=10)
    p_full.add_argument("--state", default=DEFAULT_STATE)

    p_trans = sub.add_parser("transcribe", help="单独转写一个视频")
    p_trans.add_argument("--url", required=True, help="视频下载 URL")

    args = parser.parse_args()
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

    cmds = {
        "crawl": cmd_crawl,
        "import": cmd_import,
        "process": cmd_process,
        "full": cmd_full,
        "transcribe": cmd_transcribe,
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()
