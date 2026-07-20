#!/usr/bin/env python3
"""
视频号内容采集 Pipeline

依赖本地 wx_video_download 服务（默认 http://127.0.0.1:2022）：
  - 搜索账号 / 拉主页视频列表 / 详情&互动 / 创建下载任务
  - 下载走 MITM 代理（CHANNELS_PROXY），自动解密 DRM

复用上级 scripts/ 的 config / feishu_utils / transcribe / ai_classify。

用法:
  # 按关键词搜索账号 → 拉视频 → 写入飞书（去重）
  python channels_collector.py search_and_import --keyword "遗嘱" --accounts 3 --per-account 10

  # 关键词直接搜视频（带筛选，推荐）
  python channels_collector.py video_search --keyword "遗嘱" --sort 最新 --time-range 七天 --max-videos 30
  python channels_collector.py video_search --keyword "继承纠纷" --sort 最热 --time-range 半年 --count 10

  # 指定视频号 username 导入
  python channels_collector.py import_creator --username "v2_xxx@finder" --count 20

  # 下载本地视频（state 中待下载 / 指定 id）
  python channels_collector.py download --limit 5
  python channels_collector.py download --ids "id1,id2"

  # 转写 + AI 分类 + 回写飞书
  python channels_collector.py transcribe --limit 5

  # 全流程（默认关键词搜视频 → 并行流水线：下载/抽音频预取 ∥ 转写 ∥ 分类/写飞书）
  python channels_collector.py full --keyword "遗嘱" --count 5
  python channels_collector.py full --keyword "遗嘱" --mode account --accounts 2 --per-account 5

  # 探测 API / 仅搜索账号
  python channels_collector.py search --keyword "遗嘱"
  python channels_collector.py doctor
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import re
import signal
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any, Optional

# 保证可 import 上级 scripts/
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from config import (  # noqa: E402
    CHANNELS_API_BASE,
    CHANNELS_DOWNLOAD_DIR,
    CHANNELS_PROXY,
    CHANNELS_STATE,
    DATA_DIR,
    IO_WORKERS,
    PREPARE_WORKERS,
    SHEET_ID,
    SPREADSHEET_TOKEN,
    extract_hashtags,
    resolve_sheet_id,
)
from feishu_utils import (  # noqa: E402
    append_rows,
    get_column_map,
    get_last_row,
    write_row_fields,
)

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

LOG = logging.getLogger("channels")


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="[%(asctime)s] %(levelname)s %(message)s",
            datefmt="%H:%M:%S",
        )
    else:
        root.setLevel(level)
    LOG.setLevel(level)


# ---------------------------------------------------------------------------
# Ctrl+C 保护（二次确认才退出）
# ---------------------------------------------------------------------------
# 必须在 FunASR daemon 子进程启动之前注册，否则子进程也可能收到 SIGINT。

_interrupt_count = 0
_interrupt_requested = False  # 主循环可检查此 flag 做优雅退出


def _sigint_handler(sig, frame):
    """第一次 Ctrl+C 提示；第二次强制退出。"""
    global _interrupt_count, _interrupt_requested
    _interrupt_count += 1
    if _interrupt_count == 1:
        _interrupt_requested = True
        print(
            "\n[提示] 收到 Ctrl+C。pipeline 不会立即中断；"
            "再按一次强制退出（可能导致转写中断、临时文件残留）。",
            flush=True,
        )
    else:
        print("\n[强制退出]", flush=True)
        os._exit(130)


def _install_sigint_guard() -> None:
    """注册 SIGINT 保护。SIGINT 二次确认；SIGTERM 仍立即退出（系统停机）。"""
    try:
        signal.signal(signal.SIGINT, _sigint_handler)
    except (ValueError, OSError) as e:
        LOG.warning("无法注册 SIGINT handler: %s", e)


def _check_interrupt(where: str = "") -> bool:
    """若用户已请求中断，打印位置并返回 True。"""
    if _interrupt_requested:
        loc = f" ({where})" if where else ""
        print(f"[中断] 安全停止点{loc}，结束后续处理")
        return True
    return False


# ---------------------------------------------------------------------------
# /tmp 临时文件清理（视频号 ch_*）
# ---------------------------------------------------------------------------

TMP_CH_GLOB = "/tmp/ch_*"


def cleanup_tmp_ch_files(verbose: bool = True) -> dict:
    """
    清理 /tmp/ch_* 临时文件（ffmpeg 抽出的 wav）。
    返回 {"count": N, "bytes": total_size}。
    """
    paths = glob.glob(TMP_CH_GLOB)
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
                LOG.warning("删除失败 %s: %s", p, e)
    if verbose:
        mb = total / (1024 * 1024)
        LOG.info("清理 %s 个 /tmp/ch_* 文件 (%.1f MB)", removed, mb)
    return {"count": removed, "bytes": total}


def cleanup_processed_videos(state: dict, verbose: bool = True) -> dict:
    """
    清理已成功转写入库的视频文件（文案已存飞书，本地 mp4 可删）。
    仅删除 state 中含 transcript 且 local_path 仍存在的条目；
    未转写成功的视频保留以便重试。返回 {"count": N, "bytes": total_size}。
    """
    total = 0
    removed = 0
    for oid, info in (state.get("items") or {}).items():
        if not info.get("transcript"):
            continue  # 没转写成功的保留
        local_path = info.get("local_path") or ""
        if local_path and os.path.isfile(local_path):
            try:
                sz = os.path.getsize(local_path)
                os.remove(local_path)
                total += sz
                removed += 1
            except OSError as e:
                if verbose:
                    LOG.warning("删除视频失败 %s: %s", local_path, e)
    if verbose:
        mb = total / (1024 * 1024)
        LOG.info("清理 %s 个已转写视频 (%.1f MB)", removed, mb)
    return {"count": removed, "bytes": total}


# ---------------------------------------------------------------------------
# HTTP 客户端（视频号本地 API）
# ---------------------------------------------------------------------------

# API 走本机，不走系统代理；下载 finder 域名时再显式用 CHANNELS_PROXY
_API_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    urllib.request.HTTPHandler(),
)

_DOWNLOAD_SSL = ssl.create_default_context()
try:
    _DOWNLOAD_SSL.check_hostname = False
    _DOWNLOAD_SSL.verify_mode = ssl.CERT_NONE
except Exception:
    pass


class ChannelsAPIError(RuntimeError):
    """视频号本地 API 调用失败。"""


def api_request(
    method: str,
    path: str,
    params: Optional[dict] = None,
    body: Optional[dict] = None,
    timeout: int = 60,
    max_retries: int = 3,
) -> dict:
    """
    调用 wx_video_download HTTP API。
    path 以 / 开头；成功时返回完整 JSON（含 code/msg/data）。
    """
    base = CHANNELS_API_BASE.rstrip("/")
    url = f"{base}{path}"
    if params:
        # 过滤 None
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        if clean:
            url = f"{url}?{urllib.parse.urlencode(clean)}"

    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    last_err: Optional[Exception] = None
    for attempt in range(max_retries):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with _API_OPENER.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                if not raw:
                    return {}
                result = json.loads(raw)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:400]
            if e.code in (429, 500, 502, 503, 504):
                wait = 2 ** attempt
                LOG.warning("API %s %s → %s，%ss 后重试 (%s/%s)", method, path, e.code, wait, attempt + 1, max_retries)
                last_err = e
                time.sleep(wait)
                continue
            raise ChannelsAPIError(f"API {method} {path} HTTP {e.code}: {err_body}") from e
        except urllib.error.URLError as e:
            wait = 2 ** attempt
            LOG.warning("API 网络错误: %s，%ss 后重试 (%s/%s)", e.reason, wait, attempt + 1, max_retries)
            last_err = e
            time.sleep(wait)
            continue
        except json.JSONDecodeError as e:
            raise ChannelsAPIError(f"API 响应非 JSON: {e}") from e

        code = result.get("code", 0)
        if code == 0:
            return result
        # 业务错误：部分可重试
        msg = result.get("msg", "")
        if code in (400,) and "初始化" in str(msg):
            raise ChannelsAPIError(
                f"视频号客户端未初始化（请先打开视频号页面建立 socket）: {msg}"
            )
        if code >= 500 or code == 429:
            wait = 2 ** attempt
            LOG.warning("API 业务码 %s: %s，%ss 后重试", code, msg, wait)
            last_err = ChannelsAPIError(msg)
            time.sleep(wait)
            continue
        # 409 等由调用方处理
        return result

    raise ChannelsAPIError(f"API {method} {path} 重试 {max_retries} 次后仍失败: {last_err}")


def api_get(path: str, params: Optional[dict] = None, **kw) -> dict:
    return api_request("GET", path, params=params, **kw)


def api_post(path: str, body: Optional[dict] = None, **kw) -> dict:
    return api_request("POST", path, body=body, **kw)


# ---------------------------------------------------------------------------
# 状态管理（与抖音 pipeline 同构）
# ---------------------------------------------------------------------------

def load_state(path: str = CHANNELS_STATE) -> dict:
    p = Path(path)
    if p.exists():
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            LOG.warning("状态文件损坏，从空白开始: %s", e)
    return {"items": {}, "last_import_row": 0}


def save_state(state: dict, path: str = CHANNELS_STATE) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(p) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 视频号 API 封装
# ---------------------------------------------------------------------------

def search_accounts(keyword: str, max_pages: int = 1) -> list[dict]:
    """搜索视频号账号，返回 [{username, nickname, signature, headUrl}, ...]。"""
    results: list[dict] = []
    next_marker = ""
    for page in range(max_pages):
        params = {"keyword": keyword}
        if next_marker:
            params["next_marker"] = next_marker
        resp = api_get("/api/channels/contact/search", params=params, timeout=30)
        if resp.get("code") != 0:
            raise ChannelsAPIError(f"搜索失败: {resp.get('msg')}")
        data = ((resp.get("data") or {}).get("data") or {})
        info_list = data.get("infoList") or []
        for it in info_list:
            contact = it.get("contact") or {}
            if not contact.get("username"):
                continue
            results.append({
                "username": contact.get("username", ""),
                "nickname": contact.get("nickname", ""),
                "signature": contact.get("signature", ""),
                "headUrl": contact.get("headUrl", ""),
                "profession": it.get("highlightProfession", "") or "",
            })
        next_marker = data.get("lastBuffer") or data.get("nextMarker") or ""
        continue_flag = data.get("continueFlag", 0)
        LOG.info("搜索「%s」第%s页: +%s 账号 (累计 %s)", keyword, page + 1, len(info_list), len(results))
        if not continue_flag or not next_marker:
            break
        time.sleep(0.4)
    return results


def list_feeds(username: str, max_count: int = 30, max_pages: int = 10) -> list[dict]:
    """获取账号视频列表（原始 object 列表）。"""
    if username and not username.endswith("@finder"):
        username = username + "@finder"

    objects: list[dict] = []
    next_marker = ""
    for page in range(max_pages):
        params = {"username": username}
        if next_marker:
            params["next_marker"] = next_marker
        resp = api_get("/api/channels/contact/feed/list", params=params, timeout=45)
        if resp.get("code") != 0:
            raise ChannelsAPIError(f"拉列表失败 ({username}): {resp.get('msg')}")
        data = ((resp.get("data") or {}).get("data") or {})
        batch = data.get("object") or []
        objects.extend(batch)
        LOG.info("feed %s 第%s页: +%s (累计 %s, feedsCount=%s)",
                 username[:24], page + 1, len(batch), len(objects), data.get("feedsCount"))
        if len(objects) >= max_count:
            objects = objects[:max_count]
            break
        next_marker = data.get("lastBuffer") or ""
        if not data.get("continueFlag") or not next_marker or not batch:
            break
        time.sleep(0.5)
    return objects


# scene 值说明：
#   19 = PC 主搜（账号 + 视频，默认）
#   13 = 直播
#   21 = 仅账号
#   22 = 长视频
#   23 = 直播（PC）
SEARCH_SCENES = {
    "video": 19,       # 视频 + 账号（默认）
    "account": 21,     # 仅账号
    "live": 23,        # 直播
    "long_video": 22,  # 长视频
}


def search_videos(
    keyword: str,
    *,
    scene: int = 19,
    max_videos: int = 50,
    max_pages: int = 3,
    sort: Optional[int] = None,
    time_range: Optional[int] = None,
    scope: Optional[int] = None,
) -> list[dict]:
    """按关键词搜索视频号视频（scene=19 返回 objectList）。

    筛选参数（本地+透传双层生效）：
      sort:       0 综合 / 1 最新 / 2 最热
      time_range: 0 不限 / 1 一天 / 2 七天 / 3 半年
      scope:      0 不限 / 1 已关注 / 2 最近看过 / 3 朋友赞过

    返回原始 video object 列表，可直接传给 normalize_feed()。
    """
    objects: list[dict] = []
    next_marker = ""
    offset = 0
    for page in range(max_pages):
        params: dict[str, Any] = {"keyword": keyword, "scene": scene}
        if sort is not None:
            params["sort"] = sort
        if time_range is not None:
            params["time_range"] = time_range
        if scope is not None:
            params["scope"] = scope
        if next_marker:
            params["next_marker"] = next_marker
        if offset:
            params["offset"] = offset
        resp = api_get("/api/channels/video/search", params=params, timeout=30)
        if resp.get("code") != 0:
            raise ChannelsAPIError(f"视频搜索失败: {resp.get('msg')}")
        data = ((resp.get("data") or {}).get("data") or {})
        batch = data.get("objectList") or []
        # 读本地过滤信息（首次打印）
        lf = data.get("_localFilter")
        if lf and page == 0:
            LOG.info("本地筛选: %s", lf)
        objects.extend(batch)
        LOG.info(
            "视频搜索「%s」scene=%s 第%s页: +%s 视频 (累计 %s, 账号 %s)",
            keyword, scene, page + 1, len(batch), len(objects),
            len(data.get("infoList") or []),
        )
        if len(objects) >= max_videos:
            objects = objects[:max_videos]
            break
        # 分页：同时传 lastBuff + offset
        next_marker = data.get("lastBuff") or ""
        obj_continue = data.get("objectContinueFlag", 0)
        new_offset = data.get("offset", 0)
        if isinstance(new_offset, (int, float)) and new_offset > 0:
            offset = int(new_offset) + len(batch)
        if not obj_continue or not batch:
            break
        time.sleep(0.5)
    return objects


def get_engagement(oid: str, nid: str) -> dict:
    """
    通过评论接口取互动数据 countInfo。
    返回 {likeCount, favCount, forwardCount, commentCount}，失败返回 {}。
    """
    if not oid:
        LOG.warning("engagement 跳过: oid 为空")
        return {}
    if not nid:
        LOG.warning("engagement 跳过: nid 为空 oid=%s", oid)
        return {}
    try:
        resp = api_get(
            "/api/channels/feed/comment/list",
            params={"oid": oid, "nid": nid},
            timeout=30,
            max_retries=2,
        )
        if resp.get("code") != 0:
            LOG.warning(
                "engagement 失败 oid=%s nid=%s…: code=%s msg=%s",
                oid, str(nid)[:24], resp.get("code"), resp.get("msg"),
            )
            return {}
        data = (resp.get("data") or {}).get("data") or {}
        count = data.get("countInfo") or {}
        if not count:
            mono = data.get("monotonicData") or {}
            count = mono.get("countInfo") or {}
        if not count:
            LOG.warning("engagement 响应无 countInfo oid=%s", oid)
            return {}
        result = {
            "likeCount": int(count.get("likeCount") or 0),
            "favCount": int(count.get("favCount") or 0),
            "forwardCount": int(count.get("forwardCount") or 0),
            "commentCount": int(count.get("commentCount") or 0),
        }
        LOG.debug("engagement oid=%s → %s", oid, result)
        return result
    except Exception as e:
        LOG.warning("获取互动数据失败 oid=%s: %s", oid, e)
        return {}


def _engagement_from_mapping(src: dict) -> dict:
    """从任意 dict 中尽量抽出互动四元组（缺的字段不填）。"""
    if not isinstance(src, dict) or not src:
        return {}
    out: dict = {}
    mapping = {
        "likeCount": ("likeCount", "likecount", "like_count"),
        "favCount": ("favCount", "favcount", "fav_count", "collectCount"),
        "forwardCount": ("forwardCount", "forwardcount", "forward_count", "shareCount"),
        "commentCount": ("commentCount", "commentcount", "comment_count"),
    }
    for dst, keys in mapping.items():
        for k in keys:
            if k in src and src[k] is not None and src[k] != "":
                try:
                    out[dst] = int(src[k] or 0)
                except (TypeError, ValueError):
                    continue
                break
    return out


def get_engagement_via_profile(oid: str, nid: str = "") -> tuple[dict, str]:
    """
    用 feed/profile 作为互动数据备选。
    返回 (eng_dict, resolved_nid)。
    profile 本身常只有 commentCount；若能解析出 nid，会再打一次 comment/list。
    """
    if not oid:
        return {}, ""
    try:
        profile = get_feed_profile(oid, nid)
    except Exception as e:
        LOG.warning("feed/profile 备选失败 oid=%s: %s", oid, e)
        return {}, nid or ""

    obj = profile.get("object") or {}
    resolved_nid = nid or str(obj.get("objectNonceId") or "")

    # 仅当补到新的 nid 时再打 comment/list（避免对已知失败的 nid 重复请求）
    if resolved_nid and resolved_nid != (nid or ""):
        eng = get_engagement(oid, resolved_nid)
        if eng:
            return eng, resolved_nid

    # 退而求其次：从 profile / object 拼凑
    eng = _engagement_from_mapping(obj)
    if profile.get("commentCount") is not None:
        try:
            eng.setdefault("commentCount", int(profile.get("commentCount") or 0))
        except (TypeError, ValueError):
            pass
    for nest_name in ("objectExtend", "monotonicData", "object_extend"):
        nest = obj.get(nest_name) or profile.get(nest_name) or {}
        if isinstance(nest, dict):
            eng = {**_engagement_from_mapping(nest), **eng}
            ci = nest.get("countInfo") or nest.get("countinfo") or {}
            if isinstance(ci, dict):
                eng = {**_engagement_from_mapping(ci), **eng}

    if eng:
        LOG.info("feed/profile 备选互动 oid=%s → %s", oid, eng)
    else:
        LOG.warning("feed/profile 仍无互动数据 oid=%s", oid)
    return eng, resolved_nid


def get_share_url(oid: str, nid: str = "") -> str:
    """获取分享短链 https://weixin.qq.com/sph/..."""
    try:
        params = {"oid": oid}
        if nid:
            params["nid"] = nid
        resp = api_get("/api/channels/feed/share_url", params=params, timeout=20, max_retries=2)
        if resp.get("code") != 0:
            return ""
        data = ((resp.get("data") or {}).get("data") or {})
        url = data.get("feedH5Url") or ""
        if not url:
            url_list = data.get("urlList") or []
            if url_list:
                url = (url_list[0] or {}).get("feedH5Url") or ""
        return url
    except Exception as e:
        LOG.debug("share_url 失败 oid=%s: %s", oid, e)
        return ""


def get_feed_profile(oid: str, nid: str = "") -> dict:
    """获取视频详情 object（可能不含互动数字）。"""
    params: dict[str, str] = {}
    if oid:
        params["oid"] = oid
    if nid:
        params["nid"] = nid
    resp = api_get("/api/channels/feed/profile", params=params, timeout=40)
    if resp.get("code") != 0:
        raise ChannelsAPIError(f"详情失败: {resp.get('msg')}")
    return ((resp.get("data") or {}).get("data") or {})


def _media0(obj: dict) -> dict:
    media = ((obj.get("objectDesc") or {}).get("media") or [])
    return media[0] if media else {}


def _build_download_url(media: dict, use_spec: bool = True) -> str:
    """拼接可下载地址：url + urlToken，可选附加 X-snsvideoflag。"""
    base = (media.get("url") or "") + (media.get("urlToken") or "")
    if not base:
        return ""
    if use_spec:
        specs = media.get("spec") or []
        if specs:
            fmt = (specs[0] or {}).get("fileFormat") or ""
            if fmt and "X-snsvideoflag=" not in base:
                sep = "&" if "?" in base else "?"
                base = f"{base}{sep}X-snsvideoflag={fmt}"
    return base


def normalize_feed(
    obj: dict,
    *,
    fetch_engagement: bool = True,
    fetch_share: bool = True,
) -> dict:
    """
    将视频号 object 规范为内部 item（兼容 pipeline.build_row_data 字段名）。
    """
    oid = str(obj.get("id") or "")
    nid = obj.get("objectNonceId") or ""
    contact = obj.get("contact") or {}
    desc_obj = obj.get("objectDesc") or {}
    description = desc_obj.get("description") or ""
    # 清除搜索高亮 HTML 标签（<em class="highlight">...</em>）
    # extract_hashtags 内部也会剥一次，这里保证 title/desc 本身干净
    description = re.sub(r'<[^>]+>', '', description)
    media = _media0(obj)

    # 优先使用列表自带的互动数据（feed list 已返回 likeCount 等），避免逐条请求
    obj_eng = _engagement_from_mapping(obj)
    eng: dict = {}
    if obj_eng.get("likeCount"):
        eng = obj_eng
    elif fetch_engagement and oid:
        if nid:
            eng = get_engagement(oid, nid)
        if not eng:
            # nid 为空，或 comment/list 失败 → feed/profile 备选（可能补全 nid 再拉互动）
            if not nid:
                LOG.warning("nid 为空，尝试 feed/profile 补全互动 oid=%s", oid)
            else:
                LOG.warning("comment/list 无互动数据，尝试 feed/profile 备选 oid=%s", oid)
            eng, nid_resolved = get_engagement_via_profile(oid, nid)
            if nid_resolved and not nid:
                nid = nid_resolved
        time.sleep(0.25)

    share = ""
    if fetch_share and oid:
        share = get_share_url(oid, nid)
        if not share:
            # 兜底：构造 feed 页（未必能外开，但可作标识）
            share = f"https://channels.weixin.qq.com/web/pages/feed?oid={oid}"
        time.sleep(0.15)

    download_url = _build_download_url(media)
    decode_key = media.get("decodeKey") or ""
    try:
        decode_key_int = int(decode_key) if str(decode_key).strip() else 0
    except (TypeError, ValueError):
        decode_key_int = 0

    # eng 已包含最佳互动数据（列表自带 > comment/list > profile 备选）
    like = eng.get("likeCount") or obj.get("likeCount") or 0
    fav = eng.get("favCount") or obj.get("favCount") or 0
    forward = eng.get("forwardCount") or obj.get("forwardCount") or 0
    comment = eng.get("commentCount") or obj.get("commentCount") or 0

    clean_title, hashtags = extract_hashtags(description)

    return {
        # 兼容 pipeline / build_row_data
        "aweme_id": oid,
        "title": description,
        "desc": description,
        "aweme_url": share,
        "nickname": contact.get("nickname") or "",
        "liked_count": like or 0,
        "collected_count": fav or 0,
        "share_count": forward or 0,
        "comment_count": comment or 0,
        "video_download_url": download_url,
        "platform": "视频号",
        # 视频号专有
        "object_id": oid,
        "object_nonce_id": nid,
        "username": contact.get("username") or "",
        "decode_key": decode_key_int,
        "cover_url": media.get("coverUrl") or "",
        "file_size": media.get("fileSize") or 0,
        "duration": media.get("videoPlayLen") or 0,
        "width": media.get("width") or 0,
        "height": media.get("height") or 0,
        "createtime": obj.get("createtime") or 0,
        "clean_title": clean_title,
        "hashtags": hashtags,
    }


# ---------------------------------------------------------------------------
# 飞书行构建
# ---------------------------------------------------------------------------

def build_channels_row(
    item: dict,
    transcript: str = "",
    classification: Optional[dict] = None,
    business_default: str = "传承",
) -> dict:
    """构建飞书行 dict（列名 → 值）。"""
    raw_title = item.get("title") or item.get("desc") or ""
    clean_title, hashtags = extract_hashtags(raw_title)
    if not clean_title:
        clean_title = item.get("clean_title") or raw_title

    data: dict[str, Any] = {
        "业务方向": business_default,
        "标题": clean_title,
        "平台": "视频号",
        "原始链接": item.get("aweme_url") or "",
        "创作者": item.get("nickname") or "",
        "点赞": int(item.get("liked_count") or 0),
        "收藏": int(item.get("collected_count") or 0),
        "转发": int(item.get("share_count") or 0),
        "评论": int(item.get("comment_count") or 0),
        "入库日期": str(date.today()),
        "原视频标签": ", ".join(hashtags or item.get("hashtags") or []),
    }
    if transcript:
        data["文案全文"] = transcript
    if classification:
        for field_name in [
            "业务方向", "细分领域", "内容形式", "选题方向",
            "内容切入角度", "目标人群", "情绪钩子", "特征标签",
        ]:
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


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------

def _safe_filename(name: str, max_len: int = 80) -> str:
    name = re.sub(r'[\\/:*?"<>|\n\r\t]+', "_", name).strip(" .")
    if not name:
        name = "video"
    if len(name) > max_len:
        name = name[:max_len]
    return name


def create_download_task_channels(oid: str, nid: str) -> dict:
    """
    POST /api/task/create_channels — 服务端拉详情并创建带解密 key 的下载任务。
    返回 data: {id, path, name, file_path}
    """
    resp = api_post(
        "/api/task/create_channels",
        body={"oid": str(oid), "nid": str(nid)},
        timeout=90,
        max_retries=2,
    )
    code = resp.get("code", -1)
    if code == 409:
        LOG.info("下载任务已存在 oid=%s", oid)
        return {"existing": True, "raw": resp}
    if code != 0:
        raise ChannelsAPIError(f"create_channels 失败: {resp.get('msg')} ({code})")
    return resp.get("data") or {}


def create_download_task_batch(item: dict) -> dict:
    """
    POST /api/task/create_batch — 直接用 media url + decodeKey。
    """
    oid = str(item.get("object_id") or item.get("aweme_id") or "")
    url = item.get("video_download_url") or ""
    if not oid or not url:
        raise ChannelsAPIError("create_batch 缺少 id 或 url")
    key = int(item.get("decode_key") or 0)
    title = item.get("title") or oid
    filename = _safe_filename(f"{oid}_{item.get('nickname') or 'ch'}")
    body = {
        "feeds": [{
            "id": oid,
            "nonce_id": item.get("object_nonce_id") or "",
            "url": url,
            "title": title[:200],
            "filename": filename,
            "key": key,
            "suffix": ".mp4",
        }]
    }
    resp = api_post("/api/task/create_batch", body=body, timeout=60)
    if resp.get("code") != 0:
        raise ChannelsAPIError(f"create_batch 失败: {resp.get('msg')}")
    ids = ((resp.get("data") or {}).get("ids")) or []
    return {"ids": ids, "filename": filename + ".mp4"}


def list_tasks() -> list[dict]:
    resp = api_get("/api/task/list", timeout=20)
    if resp.get("code") != 0:
        return []
    return ((resp.get("data") or {}).get("list")) or []


def find_task_for_oid(oid: str) -> Optional[dict]:
    for t in list_tasks():
        labels = (((t.get("meta") or {}).get("req") or {}).get("labels")) or {}
        if str(labels.get("id") or "") == str(oid):
            return t
        name = t.get("name") or ""
        if str(oid) in name:
            return t
    return None


def wait_task_done(
    task_id: Optional[str] = None,
    oid: Optional[str] = None,
    timeout: int = 300,
    poll: float = 1.5,
) -> Optional[dict]:
    """轮询任务直到 done/error 或超时。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = None
        if task_id:
            for t in list_tasks():
                if t.get("id") == task_id:
                    task = t
                    break
        elif oid:
            task = find_task_for_oid(oid)
        if task:
            status = (task.get("status") or "").lower()
            if status in ("done", "complete", "completed", "success"):
                return task
            if status in ("error", "failed", "fail"):
                LOG.error("下载任务失败: %s status=%s", task.get("id"), status)
                return task
        time.sleep(poll)
    LOG.warning("等待下载超时 task_id=%s oid=%s", task_id, oid)
    return None


def resolve_local_path(task: Optional[dict], preferred_name: str = "") -> str:
    """从任务元数据或下载目录推断本地文件路径。"""
    candidates: list[str] = []
    if task:
        meta = task.get("meta") or {}
        opts = meta.get("opts") or {}
        # gopeed 常见字段
        for key in ("path", "file_path", "filePath"):
            if task.get(key):
                candidates.append(str(task[key]))
        name = task.get("name") or preferred_name
        path = opts.get("path") or opts.get("Path") or ""
        if path and name:
            candidates.append(os.path.join(str(path), str(name)))
        if name:
            candidates.append(os.path.join(CHANNELS_DOWNLOAD_DIR, str(name)))
            # create_channels 实测可能落到 ~/Downloads
            home_dl = os.path.expanduser(f"~/Downloads/{name}")
            candidates.append(home_dl)
        # SingleFilepath 类字段
        res = meta.get("res") or {}
        if isinstance(res, dict):
            for k in ("name", "path"):
                if res.get(k):
                    candidates.append(str(res[k]))

    for c in candidates:
        if c and os.path.isfile(c) and os.path.getsize(c) > 50_000:
            return c

    # 扫下载目录匹配 oid
    if preferred_name:
        oid_part = preferred_name.split("_")[0]
        for base in (CHANNELS_DOWNLOAD_DIR, os.path.expanduser("~/Downloads")):
            if not os.path.isdir(base):
                continue
            try:
                for fn in os.listdir(base):
                    if oid_part and oid_part in fn and fn.endswith((".mp4", ".MP4")):
                        fp = os.path.join(base, fn)
                        if os.path.isfile(fp) and os.path.getsize(fp) > 50_000:
                            return fp
            except OSError:
                pass
    return ""


def download_one(item: dict, prefer: str = "channels") -> dict:
    """
    下载单条视频。
    prefer: channels | batch | direct
    返回 {ok, path, task_id, error}
    """
    oid = str(item.get("object_id") or item.get("aweme_id") or "")
    nid = item.get("object_nonce_id") or ""
    result = {"ok": False, "path": "", "task_id": "", "error": None}

    # 已有本地文件
    existing = item.get("local_path") or ""
    if existing and os.path.isfile(existing) and os.path.getsize(existing) > 50_000:
        result.update(ok=True, path=existing)
        return result

    Path(CHANNELS_DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)

    try:
        if prefer == "channels" and oid and nid:
            data = create_download_task_channels(oid, nid)
            if data.get("existing"):
                task = find_task_for_oid(oid)
                task_id = (task or {}).get("id")
            else:
                task_id = data.get("id") or ""
                file_path = data.get("file_path") or ""
                if file_path and os.path.isfile(file_path) and os.path.getsize(file_path) > 50_000:
                    # 已同步完成
                    dest = _ensure_in_download_dir(file_path, oid)
                    result.update(ok=True, path=dest, task_id=task_id)
                    return result
            task = wait_task_done(task_id=task_id or None, oid=oid, timeout=300)
            path = ""
            if data.get("file_path") and os.path.isfile(data["file_path"]):
                path = data["file_path"]
            if not path:
                path = resolve_local_path(task, preferred_name=oid)
            if path:
                dest = _ensure_in_download_dir(path, oid)
                result.update(ok=True, path=dest, task_id=task_id or (task or {}).get("id", ""))
                return result
            result["error"] = "任务完成但未找到文件"
            # fallthrough to batch

        if prefer in ("channels", "batch") and item.get("video_download_url"):
            LOG.info("尝试 create_batch 下载 oid=%s", oid)
            batch = create_download_task_batch(item)
            ids = batch.get("ids") or []
            task_id = ids[0] if ids else ""
            task = wait_task_done(task_id=task_id or None, oid=oid, timeout=300)
            path = resolve_local_path(task, preferred_name=batch.get("filename") or oid)
            if path:
                dest = _ensure_in_download_dir(path, oid)
                result.update(ok=True, path=dest, task_id=task_id)
                return result
            if not result.get("error"):
                result["error"] = "batch 任务完成但未找到文件"

        if prefer == "direct" or not result["ok"]:
            # 直连 MITM 代理下载（可能仍为加密流；依赖代理解密）
            url = item.get("video_download_url") or ""
            if not url:
                result["error"] = result.get("error") or "无下载 URL"
                return result
            dest = os.path.join(CHANNELS_DOWNLOAD_DIR, f"{oid}.mp4")
            if _direct_download(url, dest):
                result.update(ok=True, path=dest)
                return result
            result["error"] = result.get("error") or "direct 下载失败"

    except Exception as e:
        LOG.exception("下载异常 oid=%s", oid)
        result["error"] = str(e)
    return result


def _ensure_in_download_dir(src: str, oid: str) -> str:
    """若文件不在 CHANNELS_DOWNLOAD_DIR，复制一份过去便于统一管理。"""
    if not src or not os.path.isfile(src):
        return src
    try:
        src_real = os.path.realpath(src)
        dest_dir = os.path.realpath(CHANNELS_DOWNLOAD_DIR)
        if src_real.startswith(dest_dir + os.sep) or src_real == dest_dir:
            return src
        Path(CHANNELS_DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)
        ext = os.path.splitext(src)[1] or ".mp4"
        dest = os.path.join(CHANNELS_DOWNLOAD_DIR, f"{oid}{ext}")
        if os.path.realpath(src) != os.path.realpath(dest):
            import shutil
            shutil.copy2(src, dest)
            LOG.info("已复制到 %s (%.1fMB)", dest, os.path.getsize(dest) / 1024 / 1024)
        return dest
    except OSError as e:
        LOG.warning("复制到下载目录失败: %s", e)
        return src


def _direct_download(url: str, dest: str, timeout: int = 180) -> bool:
    """经 CHANNELS_PROXY 下载（finder.video.qq.com MITM）。"""
    LOG.info("direct 下载 via proxy %s → %s", CHANNELS_PROXY, dest)
    handlers = [
        urllib.request.ProxyHandler({"http": CHANNELS_PROXY, "https": CHANNELS_PROXY}),
        urllib.request.HTTPSHandler(context=_DOWNLOAD_SSL),
        urllib.request.HTTPHandler(),
    ]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            "Referer": "https://channels.weixin.qq.com/",
        },
    )
    t0 = time.time()
    try:
        with opener.open(req, timeout=timeout) as resp:
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
    except Exception as e:
        LOG.error("direct 下载失败: %s", e)
        try:
            if os.path.isfile(dest):
                os.remove(dest)
        except OSError:
            pass
        return False

    size = os.path.getsize(dest) if os.path.isfile(dest) else 0
    if size < 50_000:
        LOG.error("direct 下载文件过小: %.1fKB", size / 1024)
        return False
    LOG.info("direct 下载完成 %.1fMB (%.1fs)", size / 1024 / 1024, time.time() - t0)
    return True


# ---------------------------------------------------------------------------
# 核心业务
# ---------------------------------------------------------------------------

def collect_from_keyword(
    keyword: str,
    *,
    max_accounts: int = 5,
    per_account: int = 10,
    fetch_engagement: bool = True,
) -> list[dict]:
    """搜索关键词相关账号并汇总视频 item 列表。"""
    accounts = search_accounts(keyword, max_pages=1)
    if not accounts:
        LOG.warning("未搜到账号: %s", keyword)
        return []
    accounts = accounts[:max_accounts]
    items: list[dict] = []
    seen_ids: set[str] = set()
    for i, acc in enumerate(accounts):
        LOG.info("[%s/%s] 账号 %s (%s)", i + 1, len(accounts), acc["nickname"], acc["username"][:40])
        try:
            feeds = list_feeds(acc["username"], max_count=per_account)
        except Exception as e:
            LOG.error("拉列表失败 %s: %s", acc["nickname"], e)
            continue
        for obj in feeds:
            oid = str(obj.get("id") or "")
            if not oid or oid in seen_ids:
                continue
            seen_ids.add(oid)
            try:
                item = normalize_feed(obj, fetch_engagement=fetch_engagement, fetch_share=True)
                items.append(item)
            except Exception as e:
                LOG.warning("规范化失败 id=%s: %s", oid, e)
        time.sleep(0.5)
    LOG.info("关键词「%s」共收集 %s 条视频", keyword, len(items))
    return items


def collect_from_video_search(
    keyword: str,
    *,
    scene: int = 19,
    max_videos: int = 50,
    max_pages: int = 3,
    sort: Optional[int] = None,
    time_range: Optional[int] = None,
    scope: Optional[int] = None,
    fetch_engagement: bool = True,
) -> list[dict]:
    """按关键词直接搜索视频并规范化（支持筛选）。"""
    objects = search_videos(
        keyword, scene=scene, max_videos=max_videos, max_pages=max_pages,
        sort=sort, time_range=time_range, scope=scope,
    )
    if not objects:
        LOG.warning("视频搜索无结果: %s", keyword)
        return []
    items: list[dict] = []
    seen_ids: set[str] = set()
    for obj in objects:
        oid = str(obj.get("id") or "")
        if not oid or oid in seen_ids:
            continue
        seen_ids.add(oid)
        try:
            item = normalize_feed(obj, fetch_engagement=fetch_engagement, fetch_share=True)
            items.append(item)
        except Exception as e:
            LOG.warning("规范化失败 id=%s: %s", oid, e)
    LOG.info("视频搜索「%s」规范化后 %s 条", keyword, len(items))
    return items


def collect_from_username(
    username: str,
    *,
    count: int = 20,
    fetch_engagement: bool = True,
    min_likes: int = 0,
) -> list[dict]:
    feeds = list_feeds(username, max_count=count)
    items = []
    skipped_low = 0
    for obj in feeds:
        # 列表自带 likeCount，先过滤低互动视频
        if min_likes > 0:
            obj_like = int(obj.get("likeCount") or 0)
            if obj_like < min_likes:
                skipped_low += 1
                continue
        try:
            items.append(normalize_feed(obj, fetch_engagement=fetch_engagement, fetch_share=True))
        except Exception as e:
            LOG.warning("规范化失败: %s", e)
    if skipped_low:
        LOG.info("点赞过滤: 跳过 %s 条 (< %s)", skipped_low, min_likes)
    return items


def dedupe_items(items: list[dict], state: dict) -> list[dict]:
    """按 object_id / aweme_id 去重（对照 state）。"""
    existing = set(state.get("items", {}).keys())
    new_items = []
    for it in items:
        oid = str(it.get("object_id") or it.get("aweme_id") or "")
        if not oid:
            continue
        if oid in existing:
            continue
        new_items.append(it)
    skipped = len(items) - len(new_items)
    if skipped:
        LOG.info("去重跳过 %s 条已入库", skipped)
    return new_items


def write_items_to_feishu(
    items: list[dict],
    state: dict,
    state_path: str,
    *,
    business_default: str = "传承",
    with_transcript: bool = False,
    download_method: str = "channels",
) -> int:
    """
    将 items 追加写入飞书并更新 state。

    - with_transcript=False（默认）: 纯元数据写入，供 video_search / search_and_import 等使用
    - with_transcript=True: 串行 download → transcribe → classify → 写飞书
      （cmd_full 走并行流水线 _run_channels_pipeline，不经过此路径）

    返回成功写入条数。
    """
    if not items:
        return 0

    col_map = get_column_map()
    last_row = get_last_row()
    next_row = last_row + 1
    LOG.info("飞书列映射 %s 列，当前末行 %s", len(col_map), last_row)

    daemon_started = False
    if with_transcript:
        try:
            from transcribe import ensure_daemon
            ensure_daemon()
            daemon_started = True
        except Exception as e:
            LOG.warning("FunASR daemon 启动失败，将继续尝试逐条转写: %s", e)

    written = 0
    try:
        for i, item in enumerate(items):
            oid = str(item.get("object_id") or item.get("aweme_id") or "")
            clean = item.get("clean_title") or (item.get("title") or "")[:50]
            LOG.info("[%s/%s] 处理: %s", i + 1, len(items), clean)

            transcript = ""
            classification = None
            local_path = item.get("local_path") or ""

            if with_transcript:
                # 串行完整链路：download → audio → transcribe → classify
                LOG.info("  [1/4] 下载…")
                dl = download_one(item, prefer=download_method)
                if dl.get("ok"):
                    local_path = dl["path"]
                    item["local_path"] = local_path
                    LOG.info("  [2/4] 转写…")
                    transcript = _transcribe_local(local_path, ensure=not daemon_started)
                    if transcript:
                        LOG.info("  [3/4] 分类… (%s 字)", len(transcript))
                        try:
                            from ai_classify import classify_item
                            classification = classify_item(
                                title=item.get("clean_title") or item.get("title") or "",
                                desc=item.get("desc") or "",
                                transcript=transcript,
                            )
                            if classification:
                                LOG.info(
                                    "  分类: %s / %s",
                                    classification.get("业务方向"),
                                    classification.get("细分领域"),
                                )
                        except Exception as e:
                            LOG.warning("分类失败: %s", e)
                    else:
                        LOG.warning("  转写无结果，仍写入元数据")
                else:
                    LOG.warning("  下载失败，跳过转写: %s", dl.get("error"))

            LOG.info("  [%s] 写飞书…", "4/4" if with_transcript else "写")
            row_data = build_channels_row(
                item, transcript=transcript,
                classification=classification,
                business_default=business_default,
            )
            row_arr = _row_data_to_array(row_data, col_map)

            write_ok = False
            try:
                append_rows(SPREADSHEET_TOKEN, SHEET_ID, [row_arr])
                LOG.info("  已写入飞书行 %s", next_row)
                write_ok = True
                written += 1
            except Exception as e:
                LOG.error("飞书写入失败: %s", e)

            state.setdefault("items", {})[oid] = {
                "row": next_row if write_ok else 0,
                "video_url": item.get("video_download_url") or "",
                "share_url": item.get("aweme_url") or "",
                "title": item.get("title") or "",
                "desc": item.get("desc") or "",
                "nickname": item.get("nickname") or "",
                "username": item.get("username") or "",
                "object_nonce_id": item.get("object_nonce_id") or "",
                "decode_key": item.get("decode_key") or 0,
                "platform": "视频号",
                "liked_count": item.get("liked_count", 0),
                "collected_count": item.get("collected_count", 0),
                "share_count": item.get("share_count", 0),
                "comment_count": item.get("comment_count", 0),
                "local_path": local_path,
                "transcript": transcript,
                "classified": classification is not None,
                "processed": bool(transcript) and write_ok,
                "downloaded": bool(local_path and os.path.isfile(local_path)),
            }
            if write_ok:
                state["last_import_row"] = next_row
                next_row += 1
            save_state(state, state_path)
    finally:
        if daemon_started:
            try:
                from transcribe import stop_daemon
                stop_daemon()
            except Exception:
                pass

    return written


def _warmup_resources_channels() -> tuple[dict, int]:
    """
    并行预热：FunASR 模型加载 + 飞书列映射 + 末行。
    启动时清理上次异常退出残留的 /tmp/ch_* 临时文件。
    返回 (col_map, last_row)。
    """
    from transcribe import ensure_daemon

    cleanup_tmp_ch_files(verbose=True)

    col_map_holder: dict = {}
    last_row_holder: dict = {"v": 0}
    errors: list = []

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
    LOG.info("预热: 并行启动 FunASR daemon + 飞书元数据...")
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="warmup") as pool:
        f1 = pool.submit(_daemon)
        f2 = pool.submit(_cols)
        f3 = pool.submit(_last)
        f1.result()
        f2.result()
        f3.result()

    if errors:
        for name, err in errors:
            raise RuntimeError(f"预热失败 ({name}): {err}") from err

    LOG.info("预热完成 (%.1fs)", time.time() - t0)
    return col_map_holder["v"], last_row_holder["v"]


def _prepare_channels_audio(item: dict, download_method: str = "channels") -> dict:
    """
    视频号 prepare 阶段（可与 FunASR 转写并行预取）:
      download_one → extract_audio → 返回 {ok, audio_path, video_path, cleanup, error}

    视频文件保留在 CHANNELS_DOWNLOAD_DIR；仅 wav 进 cleanup（转写后删除）。
    """
    from transcribe import cleanup_paths, extract_audio

    oid = str(item.get("object_id") or item.get("aweme_id") or "")
    result: dict = {
        "ok": False,
        "audio_path": None,
        "video_path": "",
        "cleanup": [],
        "error": None,
    }

    try:
        dl = download_one(item, prefer=download_method)
        if not dl.get("ok"):
            result["error"] = dl.get("error") or "download failed"
            return result

        video_path = dl["path"]
        result["video_path"] = video_path
        item["local_path"] = video_path

        # 线程 id + oid 防并发路径冲突
        tid = (abs(hash(video_path)) + (threading.get_ident() % 1000)) % 100000
        audio_path = f"/tmp/ch_{tid}_{oid[:8] if oid else 'x'}.wav"
        cleanup = [audio_path]

        if not extract_audio(video_path, audio_path):
            cleanup_paths(cleanup)
            result["error"] = "ffmpeg failed"
            return result

        if not os.path.isfile(audio_path):
            cleanup_paths(cleanup)
            result["error"] = "no audio track"
            return result

        result.update(ok=True, audio_path=audio_path, cleanup=cleanup)
        return result
    except Exception as e:
        LOG.exception("prepare 异常 oid=%s", oid)
        cleanup_paths(result.get("cleanup") or [])
        result["error"] = str(e)
        result["cleanup"] = []
        return result


def _run_channels_pipeline(
    new_items: list[dict],
    state: dict,
    state_path: str,
    col_map: dict,
    start_row: int,
    *,
    business_default: str = "传承",
    download_method: str = "channels",
    force_route: bool = False,
) -> int:
    """
    视频号并行流水线（对齐抖音 pipeline._run_import_pipeline）。

    时间线（理想）:
      prep0
      tr0 | prep1
      cls0 | tr1 | prep2
      write0 | cls1 | tr2 | prep3
      write1 | cls2 | tr3

    - prepare（download + ffmpeg）: ThreadPool 预取
    - FunASR 转写: 主循环串行（daemon 单通道）
    - AI 分类: IO 线程，与下一条 prep/tr 重叠
    - 飞书写入: 串行保序（append_rows 不能并发），异步提交

    返回成功写入条数。
    """
    from ai_classify import classify_item
    from transcribe import cleanup_paths, stop_daemon, transcribe

    n = len(new_items)
    written = 0
    # 多子表路由：per-sheet 行号追踪（懒初始化）
    sheet_next_rows: dict[str, int] = {}
    state_lock = threading.Lock()
    t_pipeline = time.time()

    def _get_sheet_next_row(sheet_id: str) -> int:
        """获取指定子表的下一个可用行号（首次调用时查询飞书）。"""
        if sheet_id not in sheet_next_rows:
            sheet_next_rows[sheet_id] = get_last_row(SPREADSHEET_TOKEN, sheet_id) + 1
            LOG.info("[路由] 子表 %s 当前末行 %s", sheet_id, sheet_next_rows[sheet_id] - 1)
        return sheet_next_rows[sheet_id]

    prep_pool = ThreadPoolExecutor(max_workers=PREPARE_WORKERS, thread_name_prefix="prep")
    io_pool = ThreadPoolExecutor(max_workers=IO_WORKERS, thread_name_prefix="io")

    def submit_prep(item: dict) -> Future:
        return prep_pool.submit(_prepare_channels_audio, item, download_method)

    # look-ahead prepare：最多同时预取 PREPARE_WORKERS 条
    look_ahead: dict[int, Future] = {}
    for j in range(min(PREPARE_WORKERS, n)):
        look_ahead[j] = submit_prep(new_items[j])

    prev_classify_f: Optional[Future] = None
    prev_write_f: Optional[Future] = None
    prev_meta: Optional[dict] = None

    def do_classify(title: str, desc: str, transcript: str):
        if not transcript:
            return None
        return classify_item(title=title, desc=desc, transcript=transcript)

    def do_write(meta: dict, classification: Optional[dict]) -> bool:
        """逐条写入飞书 + 更新状态。根据业务方向路由到对应子表。"""
        nonlocal written
        item = meta["item"]
        oid = meta["oid"]
        transcript = meta["transcript"]
        local_path = meta["local_path"]

        row_data = build_channels_row(
            item,
            transcript=transcript,
            classification=classification,
            business_default=business_default,
        )

        # 根据业务方向路由到对应子表
        biz = business_default if force_route else row_data.get("业务方向", business_default)
        target_sheet = resolve_sheet_id(biz)
        row_hint = _get_sheet_next_row(target_sheet)

        row_arr = _row_data_to_array(row_data, col_map)

        write_ok = False
        try:
            append_rows(SPREADSHEET_TOKEN, target_sheet, [row_arr])
            LOG.info("[写入] 行%s → 子表[%s] (%s)", row_hint, target_sheet, biz)
            write_ok = True
        except Exception as e:
            LOG.error("[写入] 行%s 写入失败: %s", row_hint, e)

        with state_lock:
            state.setdefault("items", {})[oid] = {
                "row": row_hint if write_ok else 0,
                "sheet_id": target_sheet,
                "video_url": item.get("video_download_url") or "",
                "share_url": item.get("aweme_url") or "",
                "title": item.get("title") or "",
                "desc": item.get("desc") or "",
                "nickname": item.get("nickname") or "",
                "username": item.get("username") or "",
                "object_nonce_id": item.get("object_nonce_id") or "",
                "decode_key": item.get("decode_key") or 0,
                "platform": "视频号",
                "liked_count": item.get("liked_count", 0),
                "collected_count": item.get("collected_count", 0),
                "share_count": item.get("share_count", 0),
                "comment_count": item.get("comment_count", 0),
                "local_path": local_path,
                "transcript": transcript,
                "classified": classification is not None,
                "processed": bool(transcript) and write_ok,
                "downloaded": bool(local_path and os.path.isfile(local_path)),
            }
            if write_ok:
                state["last_import_row"] = row_hint
                sheet_next_rows[target_sheet] = row_hint + 1
                written += 1
            save_state(state, state_path)

        return write_ok

    try:
        for i, item in enumerate(new_items):
            if _check_interrupt(f"channels {i}/{n}"):
                break

            oid = str(item.get("object_id") or item.get("aweme_id") or f"unknown_{i}")
            clean = item.get("clean_title") or (item.get("title") or "")[:50]
            LOG.info("[%s/%s] 处理: %s", i + 1, n, clean)
            item_t0 = time.time()

            # --- 1. 取当前 prepare 结果（可能已在后台跑完）---
            prep = None
            fut = look_ahead.pop(i, None)
            if fut is not None:
                try:
                    prep = fut.result()
                except Exception as e:
                    LOG.warning("[准备] 异常: %s", e)
                    prep = None
            else:
                prep = _prepare_channels_audio(item, download_method)

            # --- 2. 预取后续条目 ---
            j = i + PREPARE_WORKERS
            if j < n and j not in look_ahead:
                look_ahead[j] = submit_prep(new_items[j])

            # --- 3. 转写（daemon 串行；与上一条 classify、下一条 prepare 重叠）---
            transcript = ""
            local_path = (prep or {}).get("video_path") or item.get("local_path") or ""
            cleanup = (prep or {}).get("cleanup") or []
            if prep and prep.get("ok") and prep.get("audio_path"):
                try:
                    transcript = transcribe(prep["audio_path"]) or ""
                    if transcript:
                        LOG.info("[转写] %s 字", len(transcript))
                    else:
                        LOG.warning("[转写] 无结果")
                except Exception as e:
                    LOG.warning("[转写] 失败: %s", e)
                finally:
                    cleanup_paths(cleanup)
            else:
                err = (prep or {}).get("error") or "unknown"
                LOG.warning("[准备] 失败，跳过转写: %s", err)

            # --- 4. 收尾上一条：等写入完成 → 取分类 → 提交写入 ---
            if prev_write_f is not None:
                try:
                    prev_write_f.result()
                except Exception as e:
                    LOG.error("[写入] 上一条异步写入异常: %s", e)
                prev_write_f = None

            if prev_meta is not None:
                classification = None
                if prev_classify_f is not None:
                    try:
                        classification = prev_classify_f.result()
                        if classification and classification.get("业务方向"):
                            LOG.info(
                                "[分类] %s / %s",
                                classification.get("业务方向"),
                                classification.get("细分领域"),
                            )
                    except Exception as e:
                        LOG.warning("[分类] 异常: %s", e)
                    prev_classify_f = None

                prev_write_f = io_pool.submit(do_write, prev_meta, classification)
                prev_meta = None

            # --- 5. 启动当前条分类（与下一条 prep/tr 重叠）---
            prev_meta = {
                "item": item,
                "oid": oid,
                "transcript": transcript,
                "local_path": local_path,
            }
            if transcript:
                title = item.get("clean_title") or item.get("title") or ""
                prev_classify_f = io_pool.submit(
                    do_classify, title, item.get("desc") or "", transcript
                )
            else:
                prev_classify_f = None

            LOG.info(
                "[耗时] 本条 prepare+转写 %.1fs (分类/写入异步进行中)",
                time.time() - item_t0,
            )

        # --- 排空最后一条 ---
        if prev_write_f is not None:
            try:
                prev_write_f.result()
            except Exception as e:
                LOG.error("[写入] 异步写入异常: %s", e)
            prev_write_f = None

        if prev_meta is not None:
            classification = None
            if prev_classify_f is not None:
                try:
                    classification = prev_classify_f.result()
                    if classification and classification.get("业务方向"):
                        LOG.info(
                            "[分类] %s / %s",
                            classification.get("业务方向"),
                            classification.get("细分领域"),
                        )
                except Exception as e:
                    LOG.warning("[分类] 异常: %s", e)
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
        try:
            stop_daemon()
        except Exception:
            pass

    LOG.info("流水线总耗时 %.1fs", time.time() - t_pipeline)
    return written


def _transcribe_local(video_path: str, *, ensure: bool = True) -> str:
    """本地视频 → 抽音频 → FunASR。"""
    from transcribe import extract_audio, transcribe, cleanup_paths, ensure_daemon

    if ensure:
        ensure_daemon()
    tid = abs(hash(video_path)) % 100000
    audio_path = f"/tmp/ch_{tid}.wav"
    cleanup = [audio_path]
    try:
        if not extract_audio(video_path, audio_path):
            return ""
        text = transcribe(audio_path)
        return text or ""
    except Exception as e:
        LOG.error("转写失败 %s: %s", video_path, e)
        return ""
    finally:
        cleanup_paths(cleanup)


# ---------------------------------------------------------------------------
# CLI 命令
# ---------------------------------------------------------------------------

def cmd_doctor(args) -> None:
    """检查本地 API 是否可用。"""
    print(f"API_BASE = {CHANNELS_API_BASE}")
    print(f"PROXY    = {CHANNELS_PROXY}")
    print(f"DL_DIR   = {CHANNELS_DOWNLOAD_DIR}")
    print(f"STATE    = {CHANNELS_STATE}")
    try:
        resp = api_get("/api/status", timeout=10, max_retries=1)
        print("status OK:", json.dumps(resp, ensure_ascii=False)[:300])
    except Exception as e:
        # status 可能不存在，尝试搜索
        print(f"/api/status: {e}")
    try:
        accs = search_accounts("遗嘱", max_pages=1)
        print(f"search OK: {len(accs)} accounts, e.g. {accs[0]['nickname'] if accs else '-'}")
    except Exception as e:
        print(f"search FAIL: {e}")
        sys.exit(1)
    print("doctor: OK")


def cmd_search(args) -> None:
    accs = search_accounts(args.keyword, max_pages=args.pages)
    for i, a in enumerate(accs, 1):
        print(f"{i:2d}. {a['nickname']}")
        print(f"    username: {a['username']}")
        sig = (a.get("signature") or "").replace("\n", " ")[:80]
        if sig:
            print(f"    bio: {sig}")
    out = args.out
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(accs, f, ensure_ascii=False, indent=2)
        print(f"\n已保存 {len(accs)} 条 → {out}")


def cmd_search_and_import(args) -> None:
    state = load_state(args.state)
    items = collect_from_keyword(
        args.keyword,
        max_accounts=args.accounts,
        per_account=args.per_account,
        fetch_engagement=not args.skip_engagement,
    )
    # 落盘原始 JSON 便于复查
    raw_path = Path(DATA_DIR) / f"channels_search_{date.today()}.json"
    try:
        # 追加合并
        prev = []
        if raw_path.exists():
            with open(raw_path, encoding="utf-8") as f:
                prev = json.load(f)
                if not isinstance(prev, list):
                    prev = []
        by_id = {str(x.get("object_id") or x.get("aweme_id")): x for x in prev}
        for it in items:
            by_id[str(it.get("object_id"))] = it
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(list(by_id.values()), f, ensure_ascii=False, indent=2)
        LOG.info("原始数据已写 %s (%s 条)", raw_path, len(by_id))
    except OSError as e:
        LOG.warning("保存 JSON 失败: %s", e)

    new_items = dedupe_items(items, state)
    if args.count:
        new_items = new_items[: args.count]
    if not new_items:
        LOG.info("没有新数据需要导入")
        return
    n = write_items_to_feishu(
        new_items, state, args.state,
        business_default=args.business,
        with_transcript=args.with_transcript,
    )
    LOG.info("search_and_import 完成: 写入 %s/%s", n, len(new_items))


# 筛选参数映射表（CLI 友好名 → API 值）
SORT_MAP = {"综合": 0, "最新": 1, "最热": 2}
TIME_MAP = {"不限": 0, "一天": 1, "七天": 2, "半年": 3}
SCOPE_MAP = {"不限": 0, "已关注": 1, "最近看过": 2, "朋友赞过": 3}


def cmd_video_search(args) -> None:
    """按关键词搜索视频（带筛选）→ 写入飞书。默认拉取互动数据（可用 --skip-engagement 关闭）。"""
    sort, time_range, scope = _parse_filter_args(args)

    LOG.info(
        "视频搜索: keyword=%s sort=%s time_range=%s scope=%s max=%s engagement=%s",
        args.keyword, sort, time_range, scope, args.max_videos,
        not args.skip_engagement,
    )

    state = load_state(args.state)
    items = collect_from_video_search(
        args.keyword,
        scene=19,
        max_videos=args.max_videos,
        max_pages=args.pages,
        sort=sort,
        time_range=time_range,
        scope=scope,
        fetch_engagement=not args.skip_engagement,
    )

    raw_path = Path(DATA_DIR) / f"channels_vsearch_{date.today()}.json"
    _save_raw_items(items, raw_path)

    new_items = dedupe_items(items, state)
    if args.count:
        new_items = new_items[:args.count]
    if not new_items:
        LOG.info("没有新数据需要导入")
        return
    n = write_items_to_feishu(
        new_items, state, args.state,
        business_default=args.business,
        with_transcript=args.with_transcript,
        download_method=getattr(args, "method", "channels") or "channels",
    )
    LOG.info("video_search 完成: 写入 %s/%s", n, len(new_items))


def cmd_import_creator(args) -> None:
    state = load_state(args.state)
    items = collect_from_username(
        args.username,
        count=args.count,
        fetch_engagement=not args.skip_engagement,
        min_likes=getattr(args, "min_likes", 0) or 0,
    )
    new_items = dedupe_items(items, state)
    if not new_items:
        LOG.info("没有新数据")
        return
    n = write_items_to_feishu(
        new_items, state, args.state,
        business_default=args.business,
        with_transcript=args.with_transcript,
    )
    LOG.info("import_creator 完成: 写入 %s/%s", n, len(new_items))


def cmd_download(args) -> None:
    state = load_state(args.state)
    items_map = state.get("items") or {}

    targets: list[tuple[str, dict]] = []
    if args.ids:
        for oid in [x.strip() for x in args.ids.split(",") if x.strip()]:
            info = items_map.get(oid) or {
                "object_nonce_id": "",
                "video_url": "",
            }
            # 允许仅用 id+nid 下载
            targets.append((oid, info))
    else:
        for oid, info in items_map.items():
            if info.get("downloaded") and info.get("local_path") and os.path.isfile(info["local_path"]):
                continue
            if not info.get("video_url") and not info.get("object_nonce_id"):
                continue
            targets.append((oid, info))

    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        LOG.info("没有待下载条目")
        return

    ok = 0
    for i, (oid, info) in enumerate(targets):
        item = {
            "object_id": oid,
            "aweme_id": oid,
            "object_nonce_id": info.get("object_nonce_id") or "",
            "video_download_url": info.get("video_url") or "",
            "decode_key": info.get("decode_key") or 0,
            "title": info.get("title") or oid,
            "nickname": info.get("nickname") or "",
            "local_path": info.get("local_path") or "",
        }
        LOG.info("[%s/%s] 下载 %s %s", i + 1, len(targets), oid, (info.get("title") or "")[:40])
        result = download_one(item, prefer=args.method)
        if result.get("ok"):
            ok += 1
            info["local_path"] = result["path"]
            info["downloaded"] = True
            info["task_id"] = result.get("task_id") or ""
            items_map[oid] = info
            save_state(state, args.state)
            LOG.info("  → %s", result["path"])
        else:
            LOG.error("  失败: %s", result.get("error"))
        time.sleep(0.3)
    LOG.info("download 完成: %s/%s", ok, len(targets))


def cmd_transcribe(args) -> None:
    """下载（如需要）+ 转写 + AI 分类 + 回写飞书。"""
    from transcribe import ensure_daemon, stop_daemon
    from ai_classify import classify_item

    state = load_state(args.state)
    items_map = state.get("items") or {}

    pending = []
    for oid, info in items_map.items():
        if info.get("processed") and info.get("transcript"):
            continue
        pending.append((oid, info))
    if args.limit:
        pending = pending[: args.limit]
    if not pending:
        LOG.info("没有待转写条目")
        return

    col_map = get_column_map()
    ensure_daemon()
    success = 0
    try:
        for i, (oid, info) in enumerate(pending):
            LOG.info("[%s/%s] 转写 行%s %s", i + 1, len(pending), info.get("row"), (info.get("title") or "")[:40])

            local = info.get("local_path") or ""
            if not (local and os.path.isfile(local)):
                item = {
                    "object_id": oid,
                    "aweme_id": oid,
                    "object_nonce_id": info.get("object_nonce_id") or "",
                    "video_download_url": info.get("video_url") or "",
                    "decode_key": info.get("decode_key") or 0,
                    "title": info.get("title") or "",
                    "nickname": info.get("nickname") or "",
                }
                dl = download_one(item, prefer=args.method)
                if not dl.get("ok"):
                    LOG.error("下载失败，跳过: %s", dl.get("error"))
                    continue
                local = dl["path"]
                info["local_path"] = local
                info["downloaded"] = True

            transcript = info.get("transcript") or _transcribe_local(local)
            if not transcript:
                LOG.warning("无转写结果，跳过")
                save_state(state, args.state)
                continue

            info["transcript"] = transcript
            clean_title, hashtags = extract_hashtags(info.get("title") or "")
            field_values: dict[str, Any] = {
                "文案全文": transcript,
                "平台": "视频号",
            }
            if clean_title:
                field_values["标题"] = clean_title
            if hashtags:
                field_values["原视频标签"] = ", ".join(hashtags)
            if info.get("nickname"):
                field_values["创作者"] = info["nickname"]

            if not info.get("classified"):
                try:
                    cls = classify_item(
                        title=clean_title or info.get("title") or "",
                        desc=info.get("desc") or "",
                        transcript=transcript,
                    )
                    if cls:
                        for fn in [
                            "业务方向", "细分领域", "内容形式", "选题方向",
                            "内容切入角度", "目标人群", "情绪钩子", "特征标签",
                        ]:
                            if cls.get(fn):
                                field_values[fn] = cls[fn]
                        info["classified"] = True
                        LOG.info("分类: %s / %s", cls.get("业务方向"), cls.get("细分领域"))
                except Exception as e:
                    LOG.warning("分类失败: %s", e)

            row_num = info.get("row") or 0
            if row_num and field_values:
                try:
                    write_row_fields(SPREADSHEET_TOKEN, SHEET_ID, row_num, field_values, col_map)
                    LOG.info("已回写飞书行 %s (%s 字段)", row_num, len(field_values))
                except Exception as e:
                    LOG.error("回写失败: %s", e)
                    save_state(state, args.state)
                    continue

            info["processed"] = True
            items_map[oid] = info
            save_state(state, args.state)
            success += 1
    finally:
        stop_daemon()

    LOG.info("transcribe 完成: %s/%s", success, len(pending))


def _parse_filter_args(args) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """解析 CLI 的 sort / time_range / scope 为 API 整型值。"""
    sort = SORT_MAP.get(getattr(args, "sort", None) or "综合")
    sort_raw = getattr(args, "sort", None)
    if sort_raw is not None and str(sort_raw).isdigit():
        sort = int(sort_raw)
    time_range = TIME_MAP.get(getattr(args, "time_range", None) or "不限")
    tr_raw = getattr(args, "time_range", None)
    if tr_raw is not None and str(tr_raw).isdigit():
        time_range = int(tr_raw)
    scope = None
    scope_raw = getattr(args, "scope", None)
    if scope_raw:
        scope = SCOPE_MAP.get(scope_raw)
        if str(scope_raw).isdigit():
            scope = int(scope_raw)
    return sort, time_range, scope


def _save_raw_items(items: list[dict], path: Path) -> None:
    """追加合并规范化 items 到 JSON 文件。"""
    try:
        prev = []
        if path.exists():
            with open(path, encoding="utf-8") as f:
                prev = json.load(f)
                if not isinstance(prev, list):
                    prev = []
        by_id = {str(x.get("object_id") or x.get("aweme_id")): x for x in prev}
        for it in items:
            by_id[str(it.get("object_id"))] = it
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(list(by_id.values()), f, ensure_ascii=False, indent=2)
        LOG.info("原始数据已写 %s (%s 条)", path, len(by_id))
    except OSError as e:
        LOG.warning("保存 JSON 失败: %s", e)


def cmd_full(args) -> None:
    """
    完整流程（并行流水线）:
      1) 采集：默认 video_search（关键词搜视频）；--mode account 走搜账号
      2) 并行流水线：prepare(download+ffmpeg) 预取 ∥ FunASR 转写 ∥ 分类/写飞书
    """
    mode = getattr(args, "mode", "video") or "video"
    fetch_eng = not getattr(args, "skip_engagement", False)
    method = getattr(args, "method", "channels") or "channels"

    LOG.info("=== full Step 1/2 采集 (mode=%s) ===", mode)
    state = load_state(args.state)

    if mode == "account":
        items = collect_from_keyword(
            args.keyword,
            max_accounts=getattr(args, "accounts", 2) or 2,
            per_account=getattr(args, "per_account", 5) or 5,
            fetch_engagement=fetch_eng,
        )
        raw_path = Path(DATA_DIR) / f"channels_search_{date.today()}.json"
    else:
        sort, time_range, scope = _parse_filter_args(args)
        LOG.info(
            "视频搜索: keyword=%s sort=%s time_range=%s scope=%s max=%s",
            args.keyword, sort, time_range, scope,
            getattr(args, "max_videos", 50),
        )
        items = collect_from_video_search(
            args.keyword,
            scene=19,
            max_videos=getattr(args, "max_videos", 50) or 50,
            max_pages=getattr(args, "pages", 3) or 3,
            sort=sort,
            time_range=time_range,
            scope=scope,
            fetch_engagement=fetch_eng,
        )
        raw_path = Path(DATA_DIR) / f"channels_vsearch_{date.today()}.json"

    _save_raw_items(items, raw_path)

    # Python 侧二次排序：Go 后端搜索时没有互动数据，"最热"排序无法生效
    sort_val = _parse_filter_args(args)[0]
    if sort_val == 2:  # 最热 → 按点赞降序
        items.sort(key=lambda x: int(x.get("liked_count") or 0), reverse=True)
        LOG.info("Python 侧按点赞降序排列 (最热)")
    elif sort_val == 1:  # 最新 → 按 createtime 降序
        items.sort(key=lambda x: int(x.get("createtime") or 0), reverse=True)
        LOG.info("Python 侧按时间降序排列 (最新)")

    new_items = dedupe_items(items, state)
    if args.count:
        new_items = new_items[: args.count]
    if not new_items:
        LOG.info("没有新数据需要处理")
        return

    LOG.info(
        "=== full Step 2/2 并行 pipeline "
        "(prep∥transcribe∥classify∥飞书, prepare_workers=%s, io_workers=%s) %s 条 ===",
        PREPARE_WORKERS, IO_WORKERS, len(new_items),
    )
    col_map, last_row = _warmup_resources_channels()
    LOG.info("飞书列映射 %s 列，当前末行 %s", len(col_map), last_row)
    n = _run_channels_pipeline(
        new_items,
        state,
        args.state,
        col_map,
        last_row + 1,
        business_default=args.business,
        download_method=method,
    )
    LOG.info("=== full 完成: 写入 %s/%s ===", n, len(new_items))


def cmd_list_state(args) -> None:
    state = load_state(args.state)
    items = state.get("items") or {}
    print(f"state: {args.state}")
    print(f"items: {len(items)}, last_import_row: {state.get('last_import_row')}")
    n = 0
    for oid, info in items.items():
        n += 1
        if args.limit and n > args.limit:
            print(f"... 其余 {len(items) - args.limit} 条省略")
            break
        flags = []
        if info.get("downloaded"):
            flags.append("dl")
        if info.get("processed"):
            flags.append("ok")
        if info.get("transcript"):
            flags.append(f"t{len(info['transcript'])}")
        print(f"  {oid} row={info.get('row')} [{' '.join(flags)}] {(info.get('title') or '')[:40]}")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="视频号内容采集 Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="调试日志")
    parser.add_argument(
        "--state",
        default=CHANNELS_STATE,
        help=f"状态文件 (默认 {CHANNELS_STATE})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_doc = sub.add_parser("doctor", help="检查本地 API 连通性")
    p_doc.set_defaults(func=cmd_doctor)

    p_search = sub.add_parser("search", help="仅搜索账号")
    p_search.add_argument("--keyword", required=True)
    p_search.add_argument("--pages", type=int, default=1)
    p_search.add_argument("--out", help="保存 JSON 路径")
    p_search.set_defaults(func=cmd_search)

    p_si = sub.add_parser("search_and_import", help="搜索账号→拉视频→写飞书")
    p_si.add_argument("--keyword", required=True)
    p_si.add_argument("--accounts", type=int, default=3, help="最多处理账号数")
    p_si.add_argument("--per-account", type=int, default=10, help="每账号最多视频数")
    p_si.add_argument("--count", type=int, help="导入总上限")
    p_si.add_argument("--business", default="传承", help="默认业务方向")
    p_si.add_argument("--skip-engagement", action="store_true", help="跳过互动数据请求")
    p_si.add_argument("--with-transcript", action="store_true", help="导入时同步下载转写（慢）")
    p_si.set_defaults(func=cmd_search_and_import)

    p_vs = sub.add_parser("video_search", help="关键词搜视频（带筛选）→ 写飞书")
    p_vs.add_argument("--keyword", required=True, help="搜索关键词")
    p_vs.add_argument("--sort", default="综合", help="排序: 综合/最新/最热 (或 0/1/2)")
    p_vs.add_argument("--time-range", dest="time_range", default="不限", help="时间: 不限/一天/七天/半年 (或 0/1/2/3)")
    p_vs.add_argument("--scope", default=None, help="范围: 不限/已关注/最近看过/朋友赞过 (或 0/1/2/3)")
    p_vs.add_argument("--max-videos", type=int, default=50, help="最多视频数")
    p_vs.add_argument("--pages", type=int, default=3, help="最多翻页数")
    p_vs.add_argument("--count", type=int, help="导入总上限")
    p_vs.add_argument("--business", default="传承", help="默认业务方向")
    p_vs.add_argument("--skip-engagement", action="store_true", help="跳过互动数据")
    p_vs.add_argument("--with-transcript", action="store_true", help="导入时同步转写")
    p_vs.set_defaults(func=cmd_video_search)

    p_ic = sub.add_parser("import_creator", help="指定 username 导入")
    p_ic.add_argument("--username", required=True, help="视频号 username (v2_xxx@finder)")
    p_ic.add_argument("--count", type=int, default=20)
    p_ic.add_argument("--business", default="传承")
    p_ic.add_argument("--skip-engagement", action="store_true")
    p_ic.add_argument("--with-transcript", action="store_true")
    p_ic.add_argument("--min-likes", type=int, default=0, help="最低点赞数过滤")
    p_ic.set_defaults(func=cmd_import_creator)

    p_dl = sub.add_parser("download", help="下载视频到本地")
    p_dl.add_argument("--ids", help="逗号分隔 object_id")
    p_dl.add_argument("--limit", type=int)
    p_dl.add_argument(
        "--method",
        choices=["channels", "batch", "direct"],
        default="channels",
        help="下载方式: create_channels / create_batch / MITM直下",
    )
    p_dl.set_defaults(func=cmd_download)

    p_tr = sub.add_parser("transcribe", help="转写+分类+回写飞书")
    p_tr.add_argument("--limit", type=int)
    p_tr.add_argument(
        "--method",
        choices=["channels", "batch", "direct"],
        default="channels",
    )
    p_tr.set_defaults(func=cmd_transcribe)

    p_full = sub.add_parser(
        "full",
        help="完整流程: 搜视频→并行流水线下载/转写/分类/写飞书（默认 video 模式）",
    )
    p_full.add_argument("--keyword", required=True)
    p_full.add_argument(
        "--mode",
        choices=["video", "account"],
        default="video",
        help="video=关键词搜视频(默认); account=搜账号再拉列表",
    )
    # video 模式筛选
    p_full.add_argument("--sort", default="综合", help="排序: 综合/最新/最热 (或 0/1/2)")
    p_full.add_argument(
        "--time-range", dest="time_range", default="不限",
        help="时间: 不限/一天/七天/半年 (或 0/1/2/3)",
    )
    p_full.add_argument("--scope", default=None, help="范围: 不限/已关注/最近看过/朋友赞过")
    p_full.add_argument("--max-videos", type=int, default=50, help="最多搜视频数")
    p_full.add_argument("--pages", type=int, default=3, help="最多翻页数")
    # account 模式
    p_full.add_argument("--accounts", type=int, default=2, help="account 模式: 最多账号数")
    p_full.add_argument("--per-account", type=int, default=5, help="account 模式: 每账号视频数")
    # 公共
    p_full.add_argument("--count", type=int, default=5, help="处理上限")
    p_full.add_argument("--business", default="传承")
    p_full.add_argument(
        "--skip-engagement",
        action="store_true",
        help="跳过互动数据请求（默认会拉取点赞/收藏/转发/评论）",
    )
    p_full.add_argument("--method", choices=["channels", "batch", "direct"], default="channels")
    p_full.set_defaults(func=cmd_full)

    p_ls = sub.add_parser("list", help="查看 state 状态")
    p_ls.add_argument("--limit", type=int, default=30)
    p_ls.set_defaults(func=cmd_list_state)

    args = parser.parse_args()
    _setup_logging(args.verbose)
    _install_sigint_guard()
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    Path(CHANNELS_DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)

    try:
        args.func(args)
    except ChannelsAPIError as e:
        LOG.error("%s", e)
        sys.exit(2)
    except KeyboardInterrupt:
        LOG.warning("用户中断")
        sys.exit(130)


if __name__ == "__main__":
    main()
