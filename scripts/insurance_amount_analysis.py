"""临时分析裁判文书中的农业保险理赔金额，不写入飞书。"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter

API = "https://zh.wikisource.org/w/api.php"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36 Codex insurance analysis/1.0"

GROUP_TERMS = {
    "玉米种植保险": ["玉米种植保险", "玉米完全成本保险", "玉米农业保险", "玉米保险"],
    "水稻种植保险": ["水稻种植保险", "水稻完全成本保险", "稻谷保险", "水稻农业保险", "水稻保险"],
    "养殖保险": ["肉牛养殖保险", "生猪养殖保险", "仔猪养殖保险", "育肥猪保险", "奶牛养殖保险", "蛋鸡养殖保险", "养殖业保险", "养殖保险"],
    "中药材保险": ["中药材种植保险", "中草药种植保险", "中药材农业保险", "中药材保险", "中草药保险", "人参保险", "黄芪保险", "金银花保险", "枸杞保险", "当归保险"],
}

def api(params: dict) -> dict:
    req = urllib.request.Request(API + "?" + urllib.parse.urlencode(params), headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read())
        except Exception as exc:
            if attempt == 5:
                raise
            time.sleep(8 + attempt * 8 if "429" in str(exc) else 3 + attempt * 3)

def search_ids(term: str, limit: int) -> tuple[list[tuple[int, str]], int | None]:
    found: dict[int, str] = {}
    cont: dict = {}
    total = None
    while len(found) < limit:
        p = {"action":"query", "list":"search", "srsearch":term, "srnamespace":0, "srlimit":500, "format":"json"}
        p.update(cont)
        d = api(p)
        q = d.get("query", {})
        total = q.get("searchinfo", {}).get("totalhits", total)
        for x in q.get("search", []):
            title = x.get("title", "")
            if "判决书" in title or "裁定书" in title:
                found[int(x["pageid"])] = title
        if "continue" not in d:
            break
        cont = d["continue"]
        time.sleep(1.2)
    return list(found.items()), total

def fetch_pages(ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    d = api({"action":"query", "pageids":"|".join(map(str, ids)), "prop":"revisions", "rvprop":"content", "rvslots":"main", "format":"json"})
    out = {}
    for pid, page in d.get("query", {}).get("pages", {}).items():
        revs = page.get("revisions", [])
        if revs:
            out[int(pid)] = revs[0].get("slots", {}).get("main", {}).get("*", "")
    return out

def field(text: str, key: str) -> str:
    m = re.search(r"\|\s*" + re.escape(key) + r"\s*=\s*([^\n|}]+)", text)
    return m.group(1).strip() if m else ""

def clean(text: str) -> str:
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\[\[([^]|]+)\|([^]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^]]+)\]\]", r"\1", text)
    text = html.unescape(text).replace("{{gap}}", "")
    return re.sub(r"\s+", "", text).strip()

def cn_number(s: str) -> float:
    digits = {"零":0,"一":1,"二":2,"两":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9}
    if not s:
        return 0
    if s.isdigit():
        return float(s)
    if "亿" in s:
        a, b = s.split("亿", 1)
        return cn_number(a) * 100000000 + (cn_number(b) if b else 0)
    if "万" in s:
        a, b = s.split("万", 1)
        return cn_number(a) * 10000 + (cn_number(b) if b else 0)
    if "千" in s:
        a, b = s.split("千", 1)
        return cn_number(a) * 1000 + (cn_number(b) if b else 0)
    if "百" in s:
        a, b = s.split("百", 1)
        return cn_number(a) * 100 + (cn_number(b) if b else 0)
    if "十" in s:
        a, b = s.split("十", 1)
        return (cn_number(a) if a else 1) * 10 + (cn_number(b) if b else 0)
    return float("".join(str(digits.get(c, "")) for c in s) or 0)

def money_values(text: str) -> list[float]:
    vals = []
    for m in re.finditer(r"(?<![\d.])(\d[\d,]*(?:\.\d+)?)\s*(万)?\s*元", text):
        v = float(m.group(1).replace(",", "")) * (10000 if m.group(2) else 1)
        if 10 <= v <= 10**10:
            vals.append(v)
    for m in re.finditer(r"(?<![\d一二三四五六七八九十百千万亿两])([零一二三四五六七八九十百千万亿两]+)\s*(万)?\s*元", text):
        v = cn_number(m.group(1)) * (10000 if m.group(2) else 1)
        if 10 <= v <= 10**10:
            vals.append(v)
    return vals

def relevant(group: str, title: str, text: str) -> bool:
    core = title + text
    if "本院认为" not in text or "判决如下" not in text:
        return False
    # 仅保留案由/标题本身进入保险纠纷语境，避免把买卖、交通、人身险等旁及提及混入。
    if "保险" not in title:
        return False
    if any(x in title for x in ["机动车交通事故责任纠纷", "意外伤害保险合同纠纷", "买卖合同纠纷", "不当得利纠纷", "合伙合同纠纷", "金融借款合同纠纷", "公路货物运输合同纠纷"]):
        return False
    facts = text[:text.find("本院认为")]
    if group == "玉米种植保险":
        return bool(re.search(r"(?:玉米种植保险|玉米完全成本保险|玉米农业保险|玉米.{0,100}(?:种植保险|保单|保费|理赔)|(?:投保|保险).{0,100}玉米)", facts))
    if group == "水稻种植保险":
        return bool(re.search(r"(?:水稻种植保险|水稻完全成本保险|稻谷保险|水稻.{0,100}(?:种植保险|保单|保费|理赔)|(?:投保|保险).{0,100}(?:水稻|稻谷))", facts))
    if group == "养殖保险":
        animal = r"(?:养殖保险|养殖业保险|肉牛养殖保险|生猪养殖保险|仔猪养殖保险|育肥猪保险|奶牛养殖保险|蛋鸡养殖保险|肉鸡养殖保险|羊保险|家禽保险)"
        return bool(re.search(animal, facts)) and not any(x in title for x in ["保证保险合同", "交通事故"])
    if group == "中药材保险":
        exact = ["中药材种植保险", "中草药种植保险", "中药材农业保险", "中药材保险", "中草药保险"]
        if any(x in facts for x in exact):
            return True
        return bool(re.search(r"(?:人参|黄芪|金银花|枸杞|当归|柴胡|板蓝根|中药材|中草药).{0,80}(?:种植|保险|保单|保费|理赔)|(?:保险|保单|保费|理赔).{0,80}(?:人参|黄芪|金银花|枸杞|当归|柴胡|板蓝根|中药材|中草药)", facts))
    return False

def section(text: str, start: str, ends: tuple[str, ...], limit: int = 100000) -> str:
    a = text.find(start)
    if a < 0:
        return ""
    a += len(start)
    positions = [text.find(x, a) for x in ends if text.find(x, a) >= 0]
    b = min(positions) if positions else len(text)
    return text[a:b][:limit]

def extract_case(group: str, pid: int, title: str, raw: str) -> dict | None:
    text = clean(raw)
    if not relevant(group, title, text):
        return None
    court, case_no, year, month, day = (field(raw, k) for k in ("court", "案号", "year", "month", "day"))
    if not court or not case_no or not year:
        return None
    judgment = section(text, "判决如下", ("如果未按", "如不服", "本判决为终审判决"))
    reason = section(text, "本院认为", ("判决如下",), 30000)
    claim = section(text, "诉讼请求", ("事实和理由", "本院认为"), 20000)
    facts = text[:text.find("本院认为")] if "本院认为" in text else ""
    award_vals = []
    for sent in re.split(r"[。；]", judgment):
        if any(k in sent for k in ["赔偿", "支付", "给付", "赔付", "保险金", "理赔款", "损失"]):
            if any(k in sent for k in ["驳回", "不予支持"]):
                continue
            award_vals.extend(money_values(sent))
    if not award_vals:
        for sent in re.split(r"[。；]", reason):
            if any(k in sent for k in ["应赔偿", "应支付", "应给付", "保险金", "理赔款"]):
                if any(k in sent for k in ["主张", "请求", "不予支持"]):
                    continue
                award_vals.extend(money_values(sent))
    claim_vals = []
    for sent in re.split(r"[。；]", claim):
        if any(k in sent for k in ["请求", "要求", "判令"]):
            claim_vals.extend(money_values(sent))
    # 去掉同一段落的重复金额，保留最多十项用于审计，合计值用于分布。
    award_vals = list(dict.fromkeys(round(x, 2) for x in award_vals))[:10]
    claim_vals = list(dict.fromkeys(round(x, 2) for x in claim_vals))[:10]
    return {
        "group": group, "pageid": pid, "title": clean(title), "court": court,
        "case_no": case_no, "date": f"{year}-{month.zfill(2)}-{day.zfill(2)}" if month and day else year,
        "source": f"https://zh.wikisource.org/w/index.php?curid={pid}",
        "award_amounts": award_vals, "award_amount": round(sum(award_vals), 2) if award_vals else None,
        "claim_amounts": claim_vals, "claim_amount": round(max(claim_vals), 2) if claim_vals else None,
        "facts_excerpt": facts[:1200],
        "judgment_excerpt": clean(judgment)[:700],
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=200)
    ap.add_argument("--candidate-multiplier", type=int, default=2)
    ap.add_argument("--out", default="/tmp/insurance-amount-analysis.json")
    args = ap.parse_args()
    all_cases = []
    source_counts = {}
    for group, terms in GROUP_TERMS.items():
        candidates: dict[int, str] = {}
        for term in terms:
            try:
                pairs, total = search_ids(term, args.target * args.candidate_multiplier)
                source_counts[f"{group}:{term}"] = total
                for pid, title in pairs:
                    candidates[pid] = title
            except Exception as exc:
                print(f"search failed {group}/{term}: {exc}", file=sys.stderr, flush=True)
            time.sleep(2)
        ids = list(candidates)
        cases = []
        for i in range(0, len(ids), 40):
            pages = fetch_pages(ids[i:i+40])
            for pid, raw in pages.items():
                case = extract_case(group, pid, candidates[pid], raw)
                if case:
                    cases.append(case)
                    if len(cases) >= args.target:
                        break
            print(group, "checked", min(i+40, len(ids)), "accepted", len(cases), flush=True)
            if len(cases) >= args.target:
                break
            time.sleep(2.5)
        all_cases.extend(cases)
        print(group, "FINAL", len(cases), flush=True)
    result = {"source_counts": source_counts, "target": args.target, "cases": all_cases}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps({"out":args.out, "count":len(all_cases), "by_group":dict(Counter(x["group"] for x in all_cases))}, ensure_ascii=False))

if __name__ == "__main__":
    main()
