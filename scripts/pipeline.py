#!/usr/bin/env python3
"""
爆款内容库 Pipeline - 采集→导入→转写→AI分类→写回飞书

用法:
    # 1. 搜索采集
    python pipeline.py crawl --keywords "遗嘱怎么写,立遗嘱" --count 10

    # 2. 导入到飞书 (仅基础字段)
    python pipeline.py import --file data/douyin/json/search_contents_2026-07-18.json

    # 3. 处理已导入条目 (转写+AI分类+回写)
    python pipeline.py process --state data/pipeline_state.json

    # 4. 完整流程 (采集→导入→处理)
    python pipeline.py full --keywords "遗嘱怎么写" --count 5

    # 5. 用户主页采集
    python pipeline.py crawl --type creator --creator-id "MS4wLjABAAA..."

    # 6. 单独转写一个视频
    python pipeline.py transcribe --url "https://www.douyin.com/aweme/v1/play/..."

环境要求:
    - lark-cli 已安装且已认证
    - MediaCrawler 在 tools/MediaCrawler/ 且 .venv 已配置
    - ffmpeg 已安装
    - FunASR 已安装在 MediaCrawler .venv 中
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

# 确保能导入同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    SPREADSHEET_TOKEN, SHEET_ID, MEDIACRAWLER_DIR, VENV_PYTHON,
    DATA_DIR, FIELDS,
    IDX_BIZ, IDX_SUB, IDX_FORM, IDX_TOPIC, IDX_ANGLE,
    IDX_AUDIENCE, IDX_EMOTION, IDX_TAGS, IDX_TRANSCRIPT,
    IDX_TITLE, IDX_PLATFORM, IDX_URL, IDX_CREATOR,
    IDX_LIKES, IDX_COLLECTS, IDX_SHARES, IDX_COMMENTS, IDX_DATE,
    IDX_HASHTAGS, extract_hashtags,
)
from feishu_utils import append_rows, write_cells, write_row_fields, get_last_row


# ==================== 状态管理 ====================

def load_state(path: str) -> dict:
    """加载 pipeline 状态文件。"""
    if Path(path).exists():
        with open(path) as f:
            return json.load(f)
    return {"items": {}, "last_import_row": 0}


def save_state(state: dict, path: str):
    """保存 pipeline 状态文件。"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


DEFAULT_STATE = f"{DATA_DIR}/pipeline_state.json"


# ==================== 采集 ====================

def cmd_crawl(args):
    """运行 MediaCrawler 采集抖音视频。"""
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
            print("主页采集需要 --creator_id 参数 (sec_uid 或主页 URL)")
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

    # 清除代理（MediaCrawler 的 Playwright 需要直连）
    env = os.environ.copy()
    for var in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"]:
        env.pop(var, None)

    result = subprocess.run(
        cmd, cwd=MEDIACRAWLER_DIR, env=env,
        capture_output=False,  # 让 MediaCrawler 输出到终端
    )

    if result.returncode != 0:
        print(f"[采集] 退出码: {result.returncode}")
        sys.exit(1)

    # 找到最新的 JSON 文件
    json_files = sorted(Path(output_dir).glob("*.json"), key=os.path.getmtime, reverse=True)
    if json_files:
        print(f"\n[采集] 输出文件: {json_files[0]}")
        with open(json_files[0]) as f:
            items = json.load(f)
        print(f"[采集] 共 {len(items)} 条视频")
        return str(json_files[0])
    else:
        print("[采集] 未找到输出 JSON 文件")
        return None


# ==================== 导入飞书 ====================

def crawled_item_to_row(item: dict) -> list:
    """将 MediaCrawler 输出的一条数据转为 19 列行数组。"""
    row = [""] * 19
    raw_title = item.get("title", "") or item.get("desc", "")
    # 从标题中剥离 #标签
    clean_title, hashtags = extract_hashtags(raw_title)
    row[IDX_TITLE] = clean_title
    row[IDX_PLATFORM] = "抖音"
    row[IDX_URL] = item.get("aweme_url", "")
    row[IDX_CREATOR] = item.get("nickname", "")
    row[IDX_LIKES] = item.get("liked_count", "")
    row[IDX_COLLECTS] = item.get("collected_count", "")
    row[IDX_SHARES] = item.get("share_count", "")
    row[IDX_COMMENTS] = item.get("comment_count", "")
    row[IDX_DATE] = str(date.today())
    row[IDX_HASHTAGS] = ", ".join(hashtags) if hashtags else ""
    return row


def cmd_import(args):
    """将采集的 JSON 导入飞书表格（仅基础字段）。"""
    with open(args.file) as f:
        items = json.load(f)

    print(f"[导入] 读取 {len(items)} 条数据，从 {args.file}")

    state = load_state(args.state)

    # 获取当前最后一行
    last_row = get_last_row()
    print(f"[导入] 飞书表格当前最后一行: {last_row}")

    # 去重：跳过已导入的 aweme_id
    existing_ids = set(state.get("items", {}).keys())
    new_items = [item for item in items
                 if item.get("aweme_id") not in existing_ids]
    skipped = len(items) - len(new_items)
    if skipped:
        print(f"[导入] 跳过 {skipped} 条重复数据")
    if not new_items:
        print("[导入] 没有新数据需要导入")
        return state

    # 批量追加
    rows = [crawled_item_to_row(item) for item in new_items]
    resp = append_rows(SPREADSHEET_TOKEN, SHEET_ID, rows)
    start_row = last_row + 1
    print(f"[导入] 追加成功: 行 {start_row} ~ {start_row + len(rows) - 1}")

    # 记录状态 (aweme_id → 飞书行号)
    for i, item in enumerate(new_items):
        aweme_id = item.get("aweme_id", f"unknown_{i}")
        state["items"][aweme_id] = {
            "row": start_row + i,
            "video_url": item.get("video_download_url", ""),
            "title": item.get("title", ""),
            "desc": item.get("desc", ""),
            "processed": False,
        }

    state["last_import_row"] = start_row + len(rows) - 1
    save_state(state, args.state)
    print(f"[导入] 状态已保存到 {args.state}")
    return state


# ==================== 处理 (转写+AI分类+回写) ====================

def cmd_process(args):
    """处理已导入的条目: 下载视频→转写→AI分类→回写飞书。"""
    state = load_state(args.state)
    items = state.get("items", {})

    # 过滤待处理项
    pending = {k: v for k, v in items.items()
               if not v.get("processed") and v.get("video_url")}

    if not pending:
        print("[处理] 没有待处理的条目")
        return

    print(f"[处理] 共 {len(pending)} 条待处理")

    # 懒加载 FunASR 模型（只在第一次使用时加载）
    from transcribe import process_video
    from ai_classify import classify_item

    # 如果有 --limit 参数
    limit = args.limit if hasattr(args, 'limit') and args.limit else len(pending)
    count = 0

    for aweme_id, info in pending.items():
        if count >= limit:
            break
        count += 1

        row_num = info["row"]
        video_url = info["video_url"]
        raw_title = info.get("title", "")
        desc = info.get("desc", "")

        # 从标题中剥离 #标签
        clean_title, hashtags = extract_hashtags(raw_title)
        field_values = {}
        if hashtags:
            field_values[IDX_TITLE] = clean_title
            field_values[IDX_HASHTAGS] = ", ".join(hashtags)

        print(f"\n{'='*60}")
        print(f"[处理 {count}/{min(limit, len(pending))}] 行{row_num}: {clean_title[:50]}...")
        if hashtags:
            print(f"[标签] {', '.join(hashtags)}")

        title = clean_title  # AI分类用清理后的标题

        # Step 1: 视频转写
        if not info.get("transcript"):
            try:
                vr = process_video(video_url)
                transcript = vr.get("transcript", "")
                # 清理 FunASR 版本号前缀
                for prefix in ["funasr version: 1.3.16.", "funasr version:", "FunASR version:"]:
                    if transcript.startswith(prefix):
                        transcript = transcript[len(prefix):].lstrip("\n ")
                        break
                if transcript:
                    field_values[IDX_TRANSCRIPT] = transcript
                    info["transcript"] = transcript
                    print(f"[处理] 转写完成: {len(transcript)}字")
                else:
                    print("[处理] 转写无结果，跳过本条")
            except Exception as e:
                print(f"[处理] 转写失败: {e}")
                transcript = ""
        else:
            transcript = info["transcript"]
            print(f"[处理] 已有转写 ({len(transcript)}字)")

        # 没有文案就不分类，避免凭空瞎写
        if not transcript:
            print("[处理] 无文案，不分类、不标记完成，等待重试")
            save_state(state, args.state)
            time.sleep(1)
            continue

        # Step 2: AI 分类
        if not info.get("classified"):
            try:
                classification = classify_item(
                    title=title, desc=desc, transcript=transcript
                )
                if classification:
                    # 映射分类结果到列索引
                    field_map = {
                        "业务方向": IDX_BIZ, "细分领域": IDX_SUB,
                        "内容形式": IDX_FORM, "选题方向": IDX_TOPIC,
                        "内容切入角度": IDX_ANGLE, "目标人群": IDX_AUDIENCE,
                        "情绪钩子": IDX_EMOTION, "特征标签": IDX_TAGS,
                    }
                    for field_name, col_idx in field_map.items():
                        val = classification.get(field_name, "")
                        if val:
                            field_values[col_idx] = val
                    info["classified"] = True
                    print(f"[处理] AI分类完成: {classification.get('业务方向', '')}")
                else:
                    print("[处理] AI分类无结果 (LLM 不可用？)")
            except Exception as e:
                print(f"[处理] AI分类异常: {e}")

        # Step 3: 回写飞书
        if field_values:
            try:
                write_row_fields(SPREADSHEET_TOKEN, SHEET_ID, row_num, field_values)
                print(f"[处理] 已回写飞书行{row_num}: {len(field_values)}个字段")
            except Exception as e:
                print(f"[处理] 回写飞书失败: {e}")
        else:
            print("[处理] 无新字段需要回写")

        # 标记已处理
        info["processed"] = True
        save_state(state, args.state)

        # 避免请求过快
        time.sleep(1)

    print(f"\n[处理] 完成，共处理 {count} 条")


# ==================== 完整流程 ====================

def cmd_full(args):
    """完整流程: 采集 → 导入 → 处理。"""
    # Step 1: 采集
    print("=" * 60)
    print("[Full] Step 1/3: 采集")
    json_file = cmd_crawl(args)
    if not json_file:
        print("[Full] 采集失败，终止")
        return

    # Step 2: 导入
    print("\n" + "=" * 60)
    print("[Full] Step 2/3: 导入飞书")
    args.file = json_file
    state = cmd_import(args)

    # Step 3: 处理
    print("\n" + "=" * 60)
    print("[Full] Step 3/3: 转写+AI分类+回写")
    cmd_process(args)

    print("\n" + "=" * 60)
    print("[Full] 全部完成!")


# ==================== 单独转写 ====================

def cmd_transcribe(args):
    """单独转写一个视频。"""
    from transcribe import process_video
    result = process_video(args.url)
    if result["success"]:
        print(f"\n--- 转写结果 ---\n{result['transcript']}")
    else:
        print("[转写] 失败")


# ==================== 入口 ====================

def main():
    parser = argparse.ArgumentParser(
        description="爆款内容库 Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s crawl --keywords "遗嘱怎么写,立遗嘱" --count 10
  %(prog)s import --file data/douyin/json/search_contents_2026-07-18.json
  %(prog)s process --state data/pipeline_state.json --limit 5
  %(prog)s full --keywords "遗嘱怎么写" --count 5
  %(prog)s crawl --type creator --creator-id "MS4wLjABAAA..."
  %(prog)s transcribe --url "https://..."
        """
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # crawl
    p_crawl = sub.add_parser("crawl", help="采集抖音视频")
    p_crawl.add_argument("--type", choices=["search", "creator"], default="search")
    p_crawl.add_argument("--keywords", help="搜索关键词，逗号分隔")
    p_crawl.add_argument("--creator-id", help="创作者 sec_uid 或主页 URL (creator 模式)")
    p_crawl.add_argument("--count", type=int, default=10, help="最大采集数")

    # import
    p_import = sub.add_parser("import", help="导入采集数据到飞书")
    p_import.add_argument("--file", required=True, help="MediaCrawler 输出的 JSON 文件")
    p_import.add_argument("--state", default=DEFAULT_STATE, help="状态文件路径")

    # process
    p_process = sub.add_parser("process", help="处理已导入条目 (转写+AI分类+回写)")
    p_process.add_argument("--state", default=DEFAULT_STATE, help="状态文件路径")
    p_process.add_argument("--limit", type=int, help="最多处理条数")
    p_process.add_argument("--no-transcribe", action="store_true", help="跳过转写 (仅AI分类)")
    p_process.add_argument("--no-classify", action="store_true", help="跳过AI分类 (仅转写)")

    # full
    p_full = sub.add_parser("full", help="完整流程: 采集→导入→处理")
    p_full.add_argument("--type", choices=["search", "creator"], default="search")
    p_full.add_argument("--keywords", help="搜索关键词")
    p_full.add_argument("--creator-id", help="创作者 sec_uid 或主页 URL")
    p_full.add_argument("--count", type=int, default=10)
    p_full.add_argument("--state", default=DEFAULT_STATE)
    p_full.add_argument("--limit", type=int, help="最多处理条数")

    # transcribe
    p_trans = sub.add_parser("transcribe", help="单独转写一个视频")
    p_trans.add_argument("--url", required=True, help="视频下载 URL")

    args = parser.parse_args()

    # 确保 data 目录存在
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
