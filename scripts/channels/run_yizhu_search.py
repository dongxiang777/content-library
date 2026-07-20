#!/usr/bin/env python3
"""视频号搜"遗嘱"：筛最近7天、点赞最高的2条，下载+转写+分类+写入传承表。"""
import sys
import time
from pathlib import Path
from datetime import date, datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from channels_collector import (
    search_videos,
    normalize_feed,
    dedupe_items,
    load_state,
    _run_channels_pipeline,
    _warmup_resources_channels,
    _save_raw_items,
    _install_sigint_guard,
    _setup_logging,
    cleanup_tmp_ch_files,
    cleanup_processed_videos,
)
from config import CHANNELS_STATE, DATA_DIR

_setup_logging(verbose=False)
_install_sigint_guard()

KEYWORD = "遗嘱"
BUSINESS = "传承"
TOP_N = 2
DAYS = 7

now = int(time.time())
threshold = now - DAYS * 86400
print(f"=== 视频号搜索采集：「{KEYWORD}」最近{DAYS}天 点赞Top{TOP_N} → {BUSINESS}表 ===", flush=True)
print(f"时间阈值: {datetime.fromtimestamp(threshold)}", flush=True)

# 1. 搜索（最热 + 七天）
objs = search_videos(KEYWORD, sort=2, time_range=2, max_videos=50, max_pages=3)
print(f"搜索返回 {len(objs)} 条", flush=True)

# 2. 筛7天内 + 按点赞排序 + 取Top N
cands = []
for o in objs:
    ct = int(o.get("createtime") or 0)
    if ct < threshold:
        continue
    like = int(o.get("likeCount") or 0)
    cands.append((like, ct, o))
cands.sort(key=lambda x: x[0], reverse=True)
top = cands[:TOP_N]

print(f"\n7天内共 {len(cands)} 条，取点赞最高 {len(top)} 条：", flush=True)
for like, ct, o in top:
    nick = (o.get("contact") or {}).get("nickname", "")
    print(f"  - {like}赞 [{datetime.fromtimestamp(ct):%m-%d %H:%M}] {nick}", flush=True)

# 3. 规范化（拉准确互动数据 + 分享链接 + 下载地址）
items = []
for like, ct, o in top:
    it = normalize_feed(o, fetch_engagement=True, fetch_share=True)
    items.append(it)
    time.sleep(0.3)

# 4. 去重
state = load_state(CHANNELS_STATE)
new_items = dedupe_items(items, state)
print(f"\n去重后: {len(new_items)} 条新视频（已入库 {len(items)-len(new_items)} 条跳过）", flush=True)

if not new_items:
    print("没有新数据需要处理", flush=True)
    cleanup_tmp_ch_files()
    sys.exit(0)

raw_path = Path(DATA_DIR) / f"channels_yizhu_{date.today()}.json"
_save_raw_items(new_items, raw_path)

# 5. 流水线（下载+转写+分类+写飞书）
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

# 6. 清理
cleanup_tmp_ch_files()
state = load_state(CHANNELS_STATE)
cleanup_processed_videos(state)
