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
    python pipeline.py cleanup                            # 清理 /tmp/dy_* 临时文件

性能：流水线并行
  - 下载/ffmpeg 预取下一条，与当前 FunASR 转写重叠
  - AI 分类与下一条的 prepare/转写重叠
  - 飞书写入与分类拆分，写入串行保序；分类异步
  - 启动时并行：FunASR warmup + 飞书列映射/末行
  - 启动时清理上次残留的 /tmp/dy_* 临时文件

注意（昵称遮蔽历史限制）:
  早期 MediaCrawler store/douyin 的 mask_nickname() 会把昵称中间字换成星号
  （如"小***师"）。代码侧已移除 mask，但历史 JSON 里仍是星号昵称。
  现有数据只能保持星号；下次重新爬取同一视频/创作者后，import 会用完整
  昵称回写飞书「创作者」列并更新 pipeline_state。
"""

import argparse
import glob
import json
import os
import signal
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


# ==================== Ctrl+C 保护（二次确认才退出） ====================
# 必须在 FunASR daemon 子进程启动之前注册，否则子进程也可能收到 SIGINT。

_interrupt_count = 0
_interrupt_requested = False  # 主循环可检查此 flag 做优雅退出


def _sigint_handler(sig, frame):
    """第一次 Ctrl+C 提示；第二次强制退出。忽略单纯的 SIGINT 误触。"""
    global _interrupt_count, _interrupt_requested
    _interrupt_count += 1
    if _interrupt_count == 1:
        _interrupt_requested = True
        print("\n[提示] 收到 Ctrl+C。pipeline 不会立即中断；"
              "再按一次强制退出（可能导致转写中断、临时文件残留）。",
              flush=True)
    else:
        print("\n[强制退出]", flush=True)
        # 用 os._exit 避免卡在线程池/daemon 清理上
        os._exit(130)


def _install_sigint_guard():
    """注册 SIGINT 保护。SIGINT 二次确认；SIGTERM 仍立即退出（系统停机）。"""
    try:
        signal.signal(signal.SIGINT, _sigint_handler)
    except (ValueError, OSError) as e:
        # 非主线程或环境不支持时忽略
        print(f"[警告] 无法注册 SIGINT handler: {e}")


def _check_interrupt(where: str = "") -> bool:
    """若用户已请求中断，打印位置并返回 True。"""
    if _interrupt_requested:
        loc = f" ({where})" if where else ""
        print(f"[中断] 安全停止点{loc}，结束后续处理")
        return True
    return False


# ==================== /tmp 临时文件清理 ====================

TMP_DY_GLOB = "/tmp/dy_*"


def cleanup_tmp_dy_files(verbose: bool = True) -> dict:
    """
    清理 /tmp/dy_* 临时文件（pipeline 下载的 mp4 + ffmpeg 抽出的 wav）。
    返回 {"count": N, "bytes": total_size}。
    """
    paths = glob.glob(TMP_DY_GLOB)
    total = 0
    removed = 0
    for p in paths:
        try:
            sz = os.path.getsize(p) if os.path.isfile(p) else 0
            os.remove(p)
            total += sz
            removed += 1
        except OSError as e:
            if verbose:
                print(f"[清理] 删除失败 {p}: {e}")
    if verbose:
        mb = total / (1024 * 1024)
        print(f"[清理] 删除 {removed} 个 /tmp/dy_* 文件 ({mb:.1f} MB)")
    return {"count": removed, "bytes": total}


def cmd_cleanup(args):
    """手动清理所有 /tmp/dy_* 临时文件。"""
    result = cleanup_tmp_dy_files(verbose=True)
    if result["count"] == 0:
        print("[清理] 没有需要清理的临时文件")


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
CRAWL_JSON_DIR = f"{MEDIACRAWLER_DIR}/data/douyin/json"


# ==================== 昵称工具 ====================
#
# 历史限制：早期爬取时 MediaCrawler 的 mask_nickname() 会把昵称中间字换成 '*'
# （如"小***师"）。代码侧已移除 mask，但历史 JSON / 飞书行仍是星号。
# 现有被遮蔽的数据只能等下次重新爬取拿到完整昵称后再回写更新。

def _nickname_is_better(new: str, old: str) -> bool:
    """判断 new 是否比 old 更完整（优先无星号遮蔽的昵称）。"""
    new = (new or "").strip()
    old = (old or "").strip()
    if not new:
        return False
    if not old:
        return True
    new_masked = "*" in new
    old_masked = "*" in old
    if old_masked and not new_masked:
        return True
    if new_masked and not old_masked:
        return False
    # 两边都无星号且不同 → 视为改名，用新值
    if not new_masked and new != old:
        return True
    return False


def load_nickname_index(json_dir: str = CRAWL_JSON_DIR) -> dict:
    """
    从 MediaCrawler 输出 JSON 按 aweme_id 建昵称索引。
    同一 aweme_id 出现多次时，优先保留更完整（无星号）的昵称。
    返回 {aweme_id: {"nickname": str, "creator_hash": str}}
    """
    index = {}
    path = Path(json_dir)
    if not path.is_dir():
        return index
    for fp in sorted(path.glob("*.json"), key=os.path.getmtime):
        try:
            with open(fp) as f:
                items = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[警告] 读取爬取 JSON 失败 {fp.name}: {e}")
            continue
        if not isinstance(items, list):
            continue
        for it in items:
            aweme_id = it.get("aweme_id")
            if not aweme_id:
                continue
            nick = (it.get("nickname") or "").strip()
            chash = (it.get("creator_hash") or "").strip()
            prev = index.get(aweme_id)
            if prev is None or _nickname_is_better(nick, prev.get("nickname", "")):
                index[aweme_id] = {"nickname": nick, "creator_hash": chash}
            elif chash and not prev.get("creator_hash"):
                prev["creator_hash"] = chash
    return index


def backfill_state_nicknames(state: dict, json_dir: str = CRAWL_JSON_DIR) -> int:
    """
    给 pipeline_state 每个 item 补全 nickname / creator_hash 字段。
    从原始爬取 JSON 按 aweme_id 匹配；历史数据可能仍是星号遮蔽昵称。
    返回更新条数。
    """
    index = load_nickname_index(json_dir)
    updated = 0
    for aweme_id, info in state.get("items", {}).items():
        meta = index.get(aweme_id)
        if not meta:
            # 至少保证字段存在，便于后续结构一致
            if "nickname" not in info:
                info["nickname"] = ""
                updated += 1
            continue
        changed = False
        new_nick = meta.get("nickname", "")
        if _nickname_is_better(new_nick, info.get("nickname", "")) or "nickname" not in info:
            if info.get("nickname") != new_nick:
                info["nickname"] = new_nick
                changed = True
            elif "nickname" not in info:
                info["nickname"] = new_nick
                changed = True
        if meta.get("creator_hash") and info.get("creator_hash") != meta["creator_hash"]:
            info["creator_hash"] = meta["creator_hash"]
            changed = True
        if changed:
            updated += 1
    return updated


def refresh_nicknames_from_items(items: list, state: dict, col_map: dict = None,
                                 write_feishu: bool = True) -> int:
    """
    用新爬取数据中的完整昵称，回写更新 state + 飞书旧行的「创作者」字段。

    场景：历史数据被 mask 成"小***师"，下次重新爬取同一视频/创作者后
    nickname 变为完整值，此处按 aweme_id（及同 creator_hash）更新。

    限制：若新 JSON 里仍是星号昵称，则无法改善，只能等真正拿到完整昵称。
    """
    if not items:
        return 0

    # 1) 按 aweme_id / creator_hash 收集更优昵称
    better_by_id = {}
    better_by_hash = {}
    for it in items:
        aweme_id = it.get("aweme_id")
        nick = (it.get("nickname") or "").strip()
        chash = (it.get("creator_hash") or "").strip()
        if not nick:
            continue
        if aweme_id:
            old = better_by_id.get(aweme_id, "")
            if _nickname_is_better(nick, old):
                better_by_id[aweme_id] = nick
        # 只有无星号的完整昵称才用于同创作者批量回写
        if chash and "*" not in nick:
            better_by_hash[chash] = nick

    if not better_by_id and not better_by_hash:
        return 0

    updated = 0
    for aweme_id, info in state.get("items", {}).items():
        old_nick = info.get("nickname", "")
        new_nick = better_by_id.get(aweme_id, "")
        if not new_nick:
            chash = info.get("creator_hash", "")
            if chash and chash in better_by_hash:
                new_nick = better_by_hash[chash]
        if not _nickname_is_better(new_nick, old_nick):
            continue

        info["nickname"] = new_nick
        # 同步 creator_hash（若新数据有）
        for it in items:
            if it.get("aweme_id") == aweme_id and it.get("creator_hash"):
                info["creator_hash"] = it["creator_hash"]
                break

        row_num = info.get("row") or 0
        if write_feishu and row_num and col_map and "创作者" in col_map:
            try:
                write_row_fields(
                    SPREADSHEET_TOKEN, SHEET_ID, row_num,
                    {"创作者": new_nick}, col_map,
                )
                print(f"[昵称] 行{row_num} 创作者已更新: {old_nick!r} → {new_nick!r}")
            except Exception as e:
                print(f"[昵称] 行{row_num} 回写失败: {e}")
        else:
            print(f"[昵称] state {aweme_id} 更新: {old_nick!r} → {new_nick!r}")
        updated += 1

    return updated


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

    启动时先清理上次异常退出残留的 /tmp/dy_* 临时文件。
    """
    from transcribe import ensure_daemon

    # 异常退出残留的 mp4/wav 可能占数 GB，预热前先清掉
    cleanup_tmp_dy_files(verbose=True)

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
            # nickname 可能仍是历史 mask 星号（见模块注释）；结构上必须写入，
            # 待下次重新爬取拿到完整昵称后再由 refresh_nicknames_from_items 回写。
            state["items"][aweme_id] = {
                "row": row_hint if write_ok else 0,
                "video_url": video_url,
                "title": raw_title,
                "desc": item.get("desc", ""),
                "nickname": item.get("nickname", "") or "",
                "creator_hash": item.get("creator_hash", "") or "",
                "platform": item.get("platform", "抖音") or "抖音",
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
            if _check_interrupt(f"import {i}/{n}"):
                break

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
    2. 对已存在条目：用新爬取的完整昵称回写飞书「创作者」（若比旧值更好）
    3. 流水线：下载/ffmpeg ∥ FunASR 转写 ∥ AI分类 ∥ 飞书写入
    4. 逐条写入飞书（不攒批），每条写完原子更新状态（含 nickname）
    """
    with open(args.file) as f:
        items = json.load(f)

    print(f"[导入] 读取 {len(items)} 条数据")

    state = load_state(args.state)

    # 并行预热 daemon + 飞书元数据（含清理残留临时文件）
    col_map, last_row = _warmup_resources()
    print(f"[导入] 飞书列映射: {len(col_map)}列")
    print(f"[导入] 当前最后一行: {last_row}")

    # 即使全部是重复数据，也尝试用新昵称回写旧行
    # （历史 mask 星号 → 重新爬取后的完整昵称）
    nick_updated = refresh_nicknames_from_items(
        items, state, col_map=col_map, write_feishu=True
    )
    if nick_updated:
        save_state(state, args.state)
        print(f"[导入] 已用新昵称回写 {nick_updated} 条旧数据")

    # 去重
    existing_ids = set(state.get("items", {}).keys())
    new_items = [it for it in items if it.get("aweme_id") not in existing_ids]
    skipped = len(items) - len(new_items)
    if skipped:
        print(f"[导入] 跳过 {skipped} 条重复数据（已尝试昵称回写）")
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

    # 清理上次异常退出残留的临时文件
    cleanup_tmp_dy_files(verbose=True)

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
        # 平台字段：build_row_data 默认写「抖音」，但 process 路径手写 field_values
        # 时曾遗漏，导致部分行（如 115-122）平台为空。此处强制补上。
        field_values["平台"] = info.get("platform") or "抖音"
        if transcript:
            field_values["文案全文"] = transcript
            info["transcript"] = transcript

        # 若 state 里已有更完整昵称，一并回写创作者列
        nick = (info.get("nickname") or "").strip()
        if nick:
            field_values["创作者"] = nick

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
        info["platform"] = field_values.get("平台", "抖音")
        save_state(state, args.state)
        return True

    try:
        for idx, (aweme_id, info) in enumerate(pending_list):
            if _check_interrupt(f"process {idx}/{len(pending_list)}"):
                break

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
    # 尽早注册 SIGINT 保护（须在 FunASR daemon 启动前），避免误触 Ctrl+C 杀进程
    _install_sigint_guard()

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
  %(prog)s cleanup
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

    sub.add_parser("cleanup", help="清理 /tmp/dy_* 临时文件（mp4/wav）")

    args = parser.parse_args()
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

    cmds = {
        "crawl": cmd_crawl,
        "import": cmd_import,
        "process": cmd_process,
        "full": cmd_full,
        "transcribe": cmd_transcribe,
        "cleanup": cmd_cleanup,
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()
