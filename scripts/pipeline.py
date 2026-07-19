#!/usr/bin/env python3
"""
爆款内容库 Pipeline - 采集→转写→分类→写入飞书

用法:
    python pipeline.py crawl --keywords "遗嘱怎么写" --count 10
    python pipeline.py import --file <json> [--count N]   # 转写+分类+写入
    python pipeline.py full --keywords "遗嘱怎么写" --count 5
    python pipeline.py process [--limit N]                # 重试无文案的行
    python pipeline.py crawl --type creator --creator-id "MS4wLjABAAA..."
    python pipeline.py transcribe --url "https://..."

性能：流水线并行
  - 下载/ffmpeg 预取下一条，与当前 FunASR 转写重叠
  - AI 分类与下一条的 prepare/转写重叠
  - 飞书写入与分类拆分，写入串行保序；分类异步
  - 启动时并行：FunASR warmup + 飞书列映射/末行
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import date
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    SPREADSHEET_TOKEN, SHEET_ID, MEDIACRAWLER_DIR, VENV_PYTHON,
    DATA_DIR, FIELD_NAMES, extract_hashtags,
    PREPARE_WORKERS, IO_WORKERS,
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
    """原子写入状态文件（tmp + os.replace）。调用方负责线程安全。"""
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


def _row_data_to_array(row_data: dict, col_map: dict) -> list:
    num_cols = max(col_map.values()) + 1 if col_map else 0
    row_arr = [""] * num_cols
    for field_name, value in row_data.items():
        idx = col_map.get(field_name)
        if idx is not None:
            row_arr[idx] = value
    return row_arr


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


# ==================== 流水线并行核心 ====================

def _warmup_resources():
    """
    并行预热：FunASR 模型加载 + 飞书列映射 + 末行。
    原先串行约 15s(模型)+2s(飞书)，现在约 max(15, 2)。
    """
    from transcribe import ensure_daemon

    col_map_holder = {}
    last_row_holder = {"v": 0}
    errors = []

    def _daemon():
        try:
            ensure_daemon()
        except Exception as e:
            errors.append(("daemon", e))

    def _cols():
        try:
            col_map_holder["v"] = get_column_map()
        except Exception as e:
            errors.append(("col_map", e))

    def _last():
        try:
            last_row_holder["v"] = get_last_row()
        except Exception as e:
            errors.append(("last_row", e))

    t0 = time.time()
    print("[预热] 并行启动 FunASR daemon + 飞书元数据...")
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="warmup") as pool:
        f1 = pool.submit(_daemon)
        f2 = pool.submit(_cols)
        f3 = pool.submit(_last)
        f1.result()
        f2.result()
        f3.result()

    if errors:
        # daemon 失败必须抛；飞书失败也抛
        for name, err in errors:
            raise RuntimeError(f"预热失败 ({name}): {err}") from err

    print(f"[预热] 完成 ({time.time()-t0:.1f}s)")
    return col_map_holder["v"], last_row_holder["v"]


def _run_import_pipeline(new_items: list, state: dict, state_path: str,
                         col_map: dict, start_row: int) -> int:
    """
    流水线处理多条视频。

    时间线（理想）:
      prep0
      tr0 | prep1
      cls0 | tr1 | prep2
      write0 | cls1 | tr2 | prep3
      write1 | cls2 | tr3
      ...

    - prepare（下载+ffmpeg）: ThreadPool 预取
    - FunASR 转写: 主循环串行（daemon 单通道 + 锁）
    - AI 分类: IO 线程，与下一条 prep/tr 重叠
    - 飞书写入: 串行保序（append_rows 不能并发）
    """
    from transcribe import prepare_audio, transcribe, stop_daemon, cleanup_paths
    from ai_classify import classify_item

    n = len(new_items)
    success = 0
    next_row = start_row  # 仅在写入成功后递增
    state_lock = threading.Lock()
    t_pipeline = time.time()

    prep_pool = ThreadPoolExecutor(max_workers=PREPARE_WORKERS, thread_name_prefix="prep")
    io_pool = ThreadPoolExecutor(max_workers=IO_WORKERS, thread_name_prefix="io")

    def submit_prep(item: dict) -> Optional[Future]:
        url = item.get("video_download_url", "")
        if not url:
            return None
        return prep_pool.submit(prepare_audio, url)

    # look-ahead prepare：最多同时预取 PREPARE_WORKERS 条
    look_ahead: dict = {}
    for j in range(min(PREPARE_WORKERS, n)):
        look_ahead[j] = submit_prep(new_items[j])

    # 上一条的异步后处理
    prev_classify_f: Optional[Future] = None
    prev_write_f: Optional[Future] = None
    prev_meta: Optional[dict] = None

    def do_classify(title: str, desc: str, transcript: str):
        if not transcript:
            return None
        return classify_item(title=title, desc=desc, transcript=transcript)

    def do_write(meta: dict, classification: Optional[dict]) -> bool:
        """逐条写入飞书 + 更新状态。调用方保证串行，append 顺序稳定。"""
        nonlocal success, next_row
        item = meta["item"]
        aweme_id = meta["aweme_id"]
        transcript = meta["transcript"]
        raw_title = meta["raw_title"]
        video_url = meta["video_url"]

        # 写入瞬间分配行号（仅成功才递增，与旧逻辑一致）
        row_hint = next_row

        row_data = build_row_data(item, transcript, classification)
        row_arr = _row_data_to_array(row_data, col_map)

        write_ok = False
        try:
            append_rows(SPREADSHEET_TOKEN, SHEET_ID, [row_arr])
            print(f"[写入] 行{row_hint} 已写入飞书 ({len(row_data)}个字段)")
            write_ok = True
        except Exception as e:
            print(f"[写入] 行{row_hint} 写入失败: {e}")

        with state_lock:
            state["items"][aweme_id] = {
                "row": row_hint if write_ok else 0,
                "video_url": video_url,
                "title": raw_title,
                "desc": item.get("desc", ""),
                "transcript": transcript,
                "classified": classification is not None,
                "processed": write_ok and bool(transcript),
            }
            if write_ok:
                state["last_import_row"] = row_hint
                next_row = row_hint + 1
                if transcript:
                    success += 1
            else:
                print("[警告] 行未写入，下次 process 命令会重试")
            save_state(state, state_path)

        return write_ok

    try:
        for i, item in enumerate(new_items):
            aweme_id = item.get("aweme_id") or f"unknown_{i}"
            raw_title = item.get("title", "") or item.get("desc", "")
            clean_title, hashtags = extract_hashtags(raw_title)
            video_url = item.get("video_download_url", "")

            print(f"\n{'='*60}")
            print(f"[导入 {i+1}/{n}] (预计行{next_row}): {clean_title[:50]}...")
            if hashtags:
                print(f"[标签] {', '.join(hashtags)}")

            item_t0 = time.time()

            # --- 1. 取当前 prepare 结果（可能已在后台跑完）---
            prep = None
            fut = look_ahead.pop(i, None)
            if fut is not None:
                try:
                    prep = fut.result()
                except Exception as e:
                    print(f"[准备] 异常: {e}")
                    prep = None
            elif video_url:
                prep = prepare_audio(video_url)

            # --- 2. 预取后续条目 ---
            j = i + PREPARE_WORKERS
            if j < n and j not in look_ahead:
                look_ahead[j] = submit_prep(new_items[j])

            # --- 3. 转写（daemon 串行；与上一条 classify、下一条 prepare 重叠）---
            transcript = ""
            cleanup = []
            if prep and prep.get("ok") and prep.get("audio_path"):
                cleanup = prep.get("cleanup") or []
                try:
                    transcript = transcribe(prep["audio_path"])
                    if transcript:
                        print(f"[转写] {len(transcript)}字")
                    else:
                        print("[转写] 无结果")
                except Exception as e:
                    print(f"[转写] 失败: {e}")
                finally:
                    cleanup_paths(cleanup)
            elif video_url:
                print(f"[转写] 准备失败: {(prep or {}).get('error', 'unknown')}")
            else:
                print("[转写] 无视频URL，跳过")

            # --- 4. 收尾上一条：等写入完成 → 取分类 → 提交写入 ---
            if prev_write_f is not None:
                try:
                    prev_write_f.result()
                except Exception as e:
                    print(f"[写入] 上一条异步写入异常: {e}")
                prev_write_f = None

            if prev_meta is not None:
                classification = None
                if prev_classify_f is not None:
                    try:
                        classification = prev_classify_f.result()
                        if classification and classification.get("业务方向"):
                            print(f"[分类] {classification['业务方向']}")
                    except Exception as e:
                        print(f"[分类] 异常: {e}")
                    prev_classify_f = None

                prev_write_f = io_pool.submit(do_write, prev_meta, classification)
                prev_meta = None

            # --- 5. 启动当前条分类（与下一条 prep/tr 重叠）---
            prev_meta = {
                "item": item,
                "aweme_id": aweme_id,
                "raw_title": raw_title,
                "video_url": video_url,
                "transcript": transcript,
            }
            if transcript:
                prev_classify_f = io_pool.submit(
                    do_classify, clean_title, item.get("desc", ""), transcript
                )
            else:
                prev_classify_f = None

            print(f"[耗时] 本条 prepare+转写 {time.time()-item_t0:.1f}s "
                  f"(分类/写入异步进行中)")

        # --- 排空最后一条 ---
        if prev_write_f is not None:
            try:
                prev_write_f.result()
            except Exception as e:
                print(f"[写入] 异步写入异常: {e}")
            prev_write_f = None

        if prev_meta is not None:
            classification = None
            if prev_classify_f is not None:
                try:
                    classification = prev_classify_f.result()
                    if classification and classification.get("业务方向"):
                        print(f"[分类] {classification['业务方向']}")
                except Exception as e:
                    print(f"[分类] 异常: {e}")
            do_write(prev_meta, classification)
            prev_meta = None

    finally:
        for fut in look_ahead.values():
            if fut is None:
                continue
            fut.cancel()
            if not fut.cancelled():
                try:
                    r = fut.result(timeout=1)
                    if isinstance(r, dict):
                        cleanup_paths(r.get("cleanup") or [])
                except Exception:
                    pass
        prep_pool.shutdown(wait=False, cancel_futures=True)
        io_pool.shutdown(wait=True)
        stop_daemon()

    print(f"\n[导入] 流水线总耗时 {time.time()-t_pipeline:.1f}s")
    return success


# ==================== 导入 (流水线: 转写 ∥ 下载下一条 ∥ 分类/写入) ====================

def cmd_import(args):
    """
    导入流程：
    1. 读取采集 JSON，去重
    2. 流水线：下载/ffmpeg ∥ FunASR 转写 ∥ AI分类 ∥ 飞书写入
    3. 逐条写入飞书（不攒批），每条写完原子更新状态
    """
    with open(args.file) as f:
        items = json.load(f)

    print(f"[导入] 读取 {len(items)} 条数据")

    state = load_state(args.state)

    # 并行预热 daemon + 飞书元数据
    col_map, last_row = _warmup_resources()
    print(f"[导入] 飞书列映射: {len(col_map)}列")
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
    print(f"[导入] 待处理 {len(new_items)} 条 "
          f"(prepare workers={PREPARE_WORKERS}, io workers={IO_WORKERS})")

    next_row = last_row + 1
    success = _run_import_pipeline(new_items, state, args.state, col_map, next_row)

    print(f"\n[导入] 完成: {success}/{len(new_items)} 条有文案")
    return state


# ==================== 重试 (补转无文案的行) ====================

def cmd_process(args):
    """
    重试无文案的条目：
    - 优先从 state 文件找 processed=false 的项
    - 如果 state 没有待处理项，则扫飞书表格找有链接但无文案的行
    同样使用流水线：prepare 下一条 ∥ 转写当前 ∥ 分类/写回
    """
    state = load_state(args.state)

    from transcribe import ensure_daemon, prepare_audio, transcribe, stop_daemon, cleanup_paths
    from ai_classify import classify_item

    # 并行：daemon + col_map
    col_map_holder = {}
    err = []

    def _d():
        try:
            ensure_daemon()
        except Exception as e:
            err.append(e)

    def _c():
        try:
            col_map_holder["v"] = get_column_map()
        except Exception as e:
            err.append(e)

    with ThreadPoolExecutor(max_workers=2) as pool:
        pool.submit(_d).result()
        pool.submit(_c).result()
    if err:
        raise err[0]
    col_map = col_map_holder["v"]

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
        stop_daemon()
        return

    limit = args.limit if hasattr(args, 'limit') and args.limit else len(pending)
    pending_list = list(pending.items())[:limit]
    print(f"[处理] 共 {len(pending)} 条待处理，本次处理 {len(pending_list)} 条")

    prep_pool = ThreadPoolExecutor(max_workers=PREPARE_WORKERS, thread_name_prefix="prep")
    io_pool = ThreadPoolExecutor(max_workers=IO_WORKERS, thread_name_prefix="io")

    def submit_prep(info: dict) -> Optional[Future]:
        url = info.get("video_url", "")
        if not url or info.get("transcript"):
            return None
        return prep_pool.submit(prepare_audio, url)

    look_ahead = {}
    if pending_list:
        look_ahead[0] = submit_prep(pending_list[0][1])
    if len(pending_list) > 1:
        look_ahead[1] = submit_prep(pending_list[1][1])

    prev_post_f: Optional[Future] = None
    count = 0

    def do_post(aweme_id, info, transcript, clean_title, hashtags, field_base):
        field_values = dict(field_base)
        if transcript:
            field_values["文案全文"] = transcript
            info["transcript"] = transcript

        if transcript and not info.get("classified"):
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

        row_num = info["row"]
        if field_values and row_num:
            try:
                write_row_fields(SPREADSHEET_TOKEN, SHEET_ID, row_num,
                                 field_values, col_map)
                print(f"[处理] 已写入行{row_num}: {len(field_values)}个字段")
            except Exception as e:
                print(f"[处理] 写入失败: {e}")
                return False

        info["processed"] = bool(transcript)
        save_state(state, args.state)
        return True

    try:
        for idx, (aweme_id, info) in enumerate(pending_list):
            count += 1
            row_num = info["row"]
            video_url = info["video_url"]
            raw_title = info.get("title", "")
            clean_title, hashtags = extract_hashtags(raw_title)

            print(f"\n{'='*60}")
            print(f"[处理 {count}/{len(pending_list)}] 行{row_num}: {clean_title[:50]}...")

            field_base = {}
            if hashtags:
                field_base["标题"] = clean_title
                field_base["原视频标签"] = ", ".join(hashtags)

            # prepare
            prep = None
            fut = look_ahead.pop(idx, None)
            if fut is not None:
                try:
                    prep = fut.result()
                except Exception as e:
                    print(f"[准备] 异常: {e}")
            elif video_url and not info.get("transcript"):
                prep = prepare_audio(video_url)

            # look-ahead next
            j = idx + 2
            if j < len(pending_list) and j not in look_ahead:
                look_ahead[j] = submit_prep(pending_list[j][1])
            if idx + 1 < len(pending_list) and (idx + 1) not in look_ahead:
                look_ahead[idx + 1] = submit_prep(pending_list[idx + 1][1])

            # 等上一条 post 完成（写回飞书串行，避免 state 竞争）
            if prev_post_f is not None:
                try:
                    prev_post_f.result()
                except Exception as e:
                    print(f"[处理] 上一条后处理异常: {e}")
                prev_post_f = None

            # 转写
            transcript = info.get("transcript", "")
            cleanup = []
            if not transcript and prep and prep.get("ok"):
                cleanup = prep.get("cleanup") or []
                try:
                    transcript = transcribe(prep["audio_path"])
                    if transcript:
                        print(f"[转写] {len(transcript)}字")
                    else:
                        print("[转写] 无结果")
                except Exception as e:
                    print(f"[转写] 失败: {e}")
                finally:
                    cleanup_paths(cleanup)
            elif not transcript and video_url:
                print(f"[转写] 准备失败: {(prep or {}).get('error', 'unknown')}")

            if not transcript:
                print("[处理] 无文案，跳过")
                save_state(state, args.state)
                continue

            # 异步分类+写回，与下一条 prepare/转写重叠
            prev_post_f = io_pool.submit(
                do_post, aweme_id, info, transcript, clean_title, hashtags, field_base
            )

        if prev_post_f is not None:
            try:
                prev_post_f.result()
            except Exception as e:
                print(f"[处理] 后处理异常: {e}")

    finally:
        for fut in look_ahead.values():
            if fut is None:
                continue
            fut.cancel()
            if not fut.cancelled():
                try:
                    r = fut.result(timeout=1)
                    if isinstance(r, dict):
                        cleanup_paths(r.get("cleanup") or [])
                except Exception:
                    pass
        prep_pool.shutdown(wait=False, cancel_futures=True)
        io_pool.shutdown(wait=True)
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
