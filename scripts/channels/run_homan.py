#!/usr/bin/env python3
"""
Homan校长账号矩阵采集：所有昵称含"Homan"的账号，点赞>=2000，下载+转写+写入情感表。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from channels_collector import (
    search_accounts,
    collect_from_username,
    dedupe_items,
    load_state,
    save_state,
    _run_channels_pipeline,
    _warmup_resources_channels,
    _save_raw_items,
    _install_sigint_guard,
    _setup_logging,
    cleanup_tmp_ch_files,
    cleanup_processed_videos,
)
from config import CHANNELS_STATE, DATA_DIR
from datetime import date

_setup_logging(verbose=False)
_install_sigint_guard()

MIN_LIKES = 2000
BUSINESS = "情感"
KEYWORD = "Homan校长"
# 博主名过滤关键词（昵称须包含此字符串）
NAME_FILTER = "Homan"

print(f"=== {KEYWORD} 矩阵采集 (点赞>={MIN_LIKES}, 业务={BUSINESS}) ===", flush=True)

# 1. 搜索账号，过滤昵称
accounts = search_accounts(KEYWORD, max_pages=2)
matched = [a for a in accounts if NAME_FILTER.lower() in a.get("nickname", "").lower()]
print(f"搜到 {len(accounts)} 个账号，匹配 {len(matched)} 个:", flush=True)
for a in matched:
    print(f"  - {a['nickname']}", flush=True)

# 2. 逐账号采集（min_likes 过滤）
all_items = []
for i, acc in enumerate(matched):
    nick = acc["nickname"]
    uname = acc["username"]
    print(f"\n[{i+1}/{len(matched)}] 采集: {nick}", flush=True)
    try:
        items = collect_from_username(
            uname,
            count=200,
            fetch_engagement=False,
            min_likes=MIN_LIKES,
        )
        print(f"  → {len(items)} 条>={MIN_LIKES}赞", flush=True)
        all_items.extend(items)
    except Exception as e:
        print(f"  ✗ 失败: {e}", flush=True)
    time.sleep(0.5)

print(f"\n合计采集: {len(all_items)} 条", flush=True)

# 3. 去重
state = load_state(CHANNELS_STATE)
new_items = dedupe_items(all_items, state)
print(f"去重后: {len(new_items)} 条新视频", flush=True)

if not new_items:
    print("没有新数据需要处理", flush=True)
    sys.exit(0)

# 保存原始数据
raw_path = Path(DATA_DIR) / f"channels_homan_{date.today()}.json"
_save_raw_items(new_items, raw_path)

# 4. 并行 pipeline
print(f"\n=== 启动 pipeline ({len(new_items)} 条) ===", flush=True)
col_map, last_row = _warmup_resources_channels()
print(f"飞书列映射 {len(col_map)} 列，当前末行 {last_row}", flush=True)

n = _run_channels_pipeline(
    new_items,
    state,
    CHANNELS_STATE,
    col_map,
    last_row + 1,
    business_default=BUSINESS,
    download_method="channels",
    force_route=True,
)
print(f"\n=== 完成: 成功写入 {n}/{len(new_items)} 条 ===", flush=True)
cleanup_tmp_ch_files()

# 5. 自动清理已转写入库的视频文件（文案已存飞书，释放磁盘）
state = load_state(CHANNELS_STATE)
cleanup_processed_videos(state)
