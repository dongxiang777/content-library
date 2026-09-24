"""从维基文库裁判文书镜像采集农业保险理赔案例，写入保险理赔真实判例表。"""
from __future__ import annotations

import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, str(__file__).rsplit("/scripts/", 1)[0] + "/scripts")
from config import SPREADSHEET_TOKEN
from feishu_utils import append_rows, read_sheet

API = "https://zh.wikisource.org/w/api.php"
SHEET = "MkMOih"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36 Codex research/1.0"

GROUPS = {
    "玉米保险理赔": ["玉米保险", "玉米种植保险", "玉米完全成本保险", "玉米 农业保险"],
    "水稻保险理赔": ["水稻种植保险", "水稻保险", "稻谷保险", "水稻 农业保险"],
    "养殖保险理赔": ["肉牛养殖保险", "生猪养殖保险", "仔猪养殖保险", "育肥猪保险", "蛋鸡养殖保险", "奶牛养殖保险", "养殖保险"],
    "中药材种植保险理赔": ["中药材保险", "中药材种植保险", "中草药保险", "中药材 农业保险"],
}

def api(params):
    req = urllib.request.Request(API + "?" + urllib.parse.urlencode(params), headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read())
        except Exception as exc:
            if attempt == 4: raise
            # 维基文库对搜索接口限流较严，宁可退避也不高频重试。
            time.sleep(15 + attempt * 10 if "429" in str(exc) else 4 + attempt * 4)

def search(term, limit=100):
    found = {}
    cont = {}
    while len(found) < limit:
        p = {"action":"query", "list":"search", "srsearch":term, "srnamespace":0, "srlimit":50, "format":"json"}
        p.update(cont)
        d = api(p)
        for x in d.get("query", {}).get("search", []):
            title = x.get("title", "")
            if "判决书" in title or "裁定书" in title:
                found[int(x["pageid"])] = title
        if "continue" not in d: break
        cont = d["continue"]
    return list(found.items())

def fetch(ids):
    if not ids: return {}
    d = api({"action":"query", "pageids":"|".join(map(str, ids)), "prop":"revisions", "rvprop":"content", "rvslots":"main", "format":"json"})
    out = {}
    for pid, page in d.get("query", {}).get("pages", {}).items():
        revs = page.get("revisions", [])
        if revs:
            out[int(pid)] = revs[0].get("slots", {}).get("main", {}).get("*", "")
    return out

def field(text, key):
    m = re.search(r"\|\s*" + re.escape(key) + r"\s*=\s*([^\n|}]+)", text)
    return m.group(1).strip() if m else ""

def clean(text):
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\[\[([^]|]+)\|([^]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^]]+)\]\]", r"\1", text)
    text = html.unescape(text).replace("{{gap}}", "")
    return re.sub(r"\s+", "", text).strip()

def section(text, start, end, limit):
    a = text.find(start)
    if a < 0: return ""
    a += len(start)
    b = text.find(end, a) if end else len(text)
    if b < 0: b = len(text)
    return clean(text[a:b])[:limit]

def classify(group, title, body):
    hay = title + body
    if group == "养殖保险理赔":
        animal = next((x for x in ["猪", "牛", "鸡", "鸭", "马", "羊"] if x in hay), "养殖")
        return f"养殖保险理赔；{animal}；保险合同纠纷；查勘定损"
    if group == "中药材种植保险理赔":
        herbs = [x for x in ["人参", "黄芪", "当归", "柴胡", "金银花", "板蓝根", "中药材"] if x in hay]
        return "中药材种植保险理赔；" + ("、".join(herbs[:3]) or "中药材") + "；自然灾害；保险合同纠纷"
    crop = "玉米" if group.startswith("玉米") else "水稻"
    return f"{crop}保险理赔；农业保险；自然灾害；查勘定损；保险合同纠纷"

def make_row(group, pid, title, text, seq):
    court, case_no, year, month, day = (field(text, k) for k in ("court", "案号", "year", "month", "day"))
    if not court or not case_no or "判决如下" not in text or "本院认为" not in text:
        return None
    # 搜索词可能只命中正文中的旁及事实；必须确认案件本身属于目标保险语境。
    core = title + text
    if "保险" not in title: return None
    if group == "玉米保险理赔" and not re.search(r"(?:玉米).{0,80}保险|保险.{0,80}(?:玉米)", core): return None
    if group == "水稻保险理赔" and not re.search(r"(?:水稻|稻谷).{0,80}保险|保险.{0,80}(?:水稻|稻谷)", core): return None
    if group == "养殖保险理赔" and not any(x in core for x in ["生猪养殖保险", "育肥猪保险", "能繁母猪保险", "肉牛养殖保险", "奶牛养殖保险", "肉鸡养殖保险", "蛋鸡养殖保险", "养殖保险"]): return None
    if group == "中药材种植保险理赔" and not any(x in core for x in ["中药材种植保险", "中草药种植保险", "中药材保险"]): return None
    facts = section(text, "经审理查明", "本院认为", 900)
    if not facts:
        facts = section(text, "事实和理由：", "本院认为", 900) or section(text, "事实及理由：", "本院认为", 900)
    reason = section(text, "本院认为", "判决如下", 520)
    judgment = section(text, "判决如下", "如果未按", 360)
    if not facts or not reason or not judgment: return None
    tags = classify(group, title, facts + reason)
    title_clean = clean(title).replace("民事民事", "民事")
    detail = f"案情：{facts}；法院认为：{reason}；裁判结果：{judgment}"
    summary = f"{year}年，{court}审理{title_clean}，核心争议是{tags.split('；')[0]}中的保险责任、损失认定或赔偿金额。"
    date = f"{year}年{month}月{day}日"
    # 当前表没有要求新闻来源；该列按用户要求留空。
    return [f"insurance-case-20260815-{seq:03d}", group, tags, detail, summary, case_no, "裁判文书（维基文库镜像）", "", date]

def main(per_group=12, write=True):
    existing = read_sheet("A1:I200", SPREADSHEET_TOKEN, SHEET)
    existing_ids = {str(r[0]) for r in existing[1:] if r and r[0]}
    rows, seen_case = [], set()
    for group, terms in GROUPS.items():
        candidates = {}
        for term in terms:
            try:
                for pid, title in search(term, 100): candidates[pid] = title
            except Exception as exc:
                print(f"skip search {term}: {exc}", file=sys.stderr, flush=True)
            # 每类优先用前几个结果，避免触发站点限流。
            if len(candidates) >= per_group * 8: break
            time.sleep(8)
        ids = list(candidates)
        for i in range(0, len(ids), 8):
            pages = fetch(ids[i:i+8])
            for pid, text in pages.items():
                if len([r for r in rows if r[1] == group]) >= per_group: break
                title = candidates[pid]
                row = make_row(group, pid, title, text, len(rows)+1)
                if row and row[5] not in seen_case and row[0] not in existing_ids:
                    rows.append(row); seen_case.add(row[5])
            time.sleep(1.5)
            if len([r for r in rows if r[1] == group]) >= per_group: break
        print(group, len([r for r in rows if r[1] == group]), flush=True)
    if write and rows:
        print(json.dumps(append_rows(SPREADSHEET_TOKEN, SHEET, rows), ensure_ascii=False))
    print(json.dumps({"count":len(rows), "by_group":{g:sum(r[1]==g for r in rows) for g in GROUPS}, "rows":rows}, ensure_ascii=False))

if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv)>1 else 12, write="--dry-run" not in sys.argv)
