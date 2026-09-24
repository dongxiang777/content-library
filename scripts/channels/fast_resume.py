#!/usr/bin/env python3
"""快速续跑：并行预取视频，串行普通转写，先写文案，分类后置。"""

import os
import sys
import argparse
import json
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(__file__).rsplit("/scripts/", 1)[0] + "/scripts")
sys.path.insert(0, str(__file__).rsplit("/scripts/", 1)[0] + "/scripts/channels")
os.environ["FAST_TRANSCRIBE"] = "1"

from channels_collector import download_one, load_state, save_state, _transcribe_local
from config import CHANNELS_STATE, SPREADSHEET_TOKEN
from feishu_utils import get_column_map, write_row_fields
from transcribe import ensure_daemon, stop_daemon

DOWNLOAD_METHOD = "direct"

def _download(oid: str, info: dict) -> tuple[str, str]:
    local = info.get("local_path") or ""
    if local and os.path.isfile(local):
        return local, ""
    item = {
        "object_id": oid,
        "aweme_id": oid,
        "object_nonce_id": info.get("object_nonce_id") or "",
        "video_download_url": info.get("video_url") or "",
        "decode_key": info.get("decode_key") or 0,
        "title": info.get("title") or "",
        "desc": info.get("desc") or "",
        "nickname": info.get("nickname") or "",
    }
    result = download_one(item, prefer=DOWNLOAD_METHOD)
    if not result.get("ok"):
        return "", str(result.get("error") or "download failed")
    return result["path"], ""


def _remove_video(path: str) -> None:
    if not path:
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", default="data/channels_life_2026-07-31.json")
    parser.add_argument("--method", choices=["direct", "channels", "batch"], default="direct")
    args = parser.parse_args()
    global DOWNLOAD_METHOD
    DOWNLOAD_METHOD = args.method
    state = load_state(CHANNELS_STATE)
    items = state.get("items") or {}
    raw_ids = {
        str(x.get("object_id"))
        for x in json.load(open(args.raw, encoding="utf-8"))
    }
    pending = [
        (oid, info) for oid, info in items.items()
        if oid in raw_ids
        and info.get("row")
        and not info.get("transcript")
        and not info.get("skipped")
    ]
    print(f"快速续跑：{len(pending)} 条；下载并行=6；说话人分离=关闭", flush=True)
    if not pending:
        return

    colmaps = {}
    ensure_daemon()
    done = 0
    executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="fast-dl")
    futures = {}
    try:
        for oid, info in pending[:6]:
            futures[oid] = executor.submit(_download, oid, info)
        for index, (oid, info) in enumerate(pending):
            future = futures.pop(oid)
            path, error = future.result()
            next_index = index + 6
            if next_index < len(pending):
                noid, ninfo = pending[next_index]
                futures[noid] = executor.submit(_download, noid, ninfo)
            print(f"[{index + 1}/{len(pending)}] {info.get('nickname')} {(info.get('title') or '')[:40]}", flush=True)
            if error:
                print(f"  下载失败：{error}", flush=True)
                info["skipped"] = True
                info["skip_reason"] = error
                items[oid] = info
                save_state(state, CHANNELS_STATE)
                continue
            try:
                transcript = _transcribe_local(path)
            except Exception as exc:
                print(f"  转写失败：{exc}", flush=True)
                _remove_video(path)
                info["local_path"] = ""
                info["skipped"] = True
                info["skip_reason"] = str(exc)
                items[oid] = info
                save_state(state, CHANNELS_STATE)
                continue
            if not transcript:
                print("  无转写结果", flush=True)
                _remove_video(path)
                info["local_path"] = ""
                info["skipped"] = True
                info["skip_reason"] = "无转写结果或视频损坏"
                items[oid] = info
                save_state(state, CHANNELS_STATE)
                continue
            info["local_path"] = path
            info["downloaded"] = True
            info["transcript"] = transcript
            sheet = info.get("sheet_id") or "sy4S58"
            if sheet not in colmaps:
                colmaps[sheet] = get_column_map(SPREADSHEET_TOKEN, sheet)
            try:
                write_row_fields(
                    SPREADSHEET_TOKEN,
                    sheet,
                    int(info["row"]),
                    {"文案全文": transcript, "平台": "视频号"},
                    colmaps[sheet],
                )
                info["processed"] = True
                try:
                    os.remove(path)
                    info["local_path"] = ""
                    print("  已删除本地视频", flush=True)
                except OSError as exc:
                    print(f"  视频删除失败（保留路径）：{exc}", flush=True)
                done += 1
                print(f"  完成 {len(transcript)} 字", flush=True)
            except Exception as exc:
                print(f"  飞书回写失败（文案已保存在状态）：{exc}", flush=True)
            items[oid] = info
            save_state(state, CHANNELS_STATE)
    finally:
        executor.shutdown(wait=True)
        stop_daemon()
        # 失败项不留本地视频，避免坏文件和无法转写的文件持续占盘。
        for oid, info in items.items():
            if oid in raw_ids and not info.get("transcript") and info.get("local_path"):
                _remove_video(info.get("local_path") or "")
                info["local_path"] = ""
        save_state(state, CHANNELS_STATE)
    print(f"快速续跑结束：{done}/{len(pending)}", flush=True)


if __name__ == "__main__":
    main()
