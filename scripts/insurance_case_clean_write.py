"""Clean the agricultural-insurance analysis batch and optionally write it to Feishu.

This is intentionally conservative: the target sheet is a real-case library, so
keyword hits from unrelated insurance, transport, employment, medical, and
traffic judgments are excluded.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, OrderedDict

from insurance_amount_analysis import fetch_pages, field, clean, section
from feishu_utils import read_sheet, write_cells


TOKEN = "HxHmsSRjrhqsPkt8gDEcUIL8nMc"
SHEET_ID = "MkMOih"
ANALYSIS_PATH = "/tmp/insurance-amount-analysis-strict.json"

# This judgment was found by a targeted exact crop-insurance search but was
# omitted from the previous broad batch because its page title was nonstandard.
EXTRA_CASE_IDS = [4660502]  # (2023)宁05民终1368号: 宁夏地方财政补贴型枸杞种植保险

BAD_TITLE = re.compile(
    r"机动车|交通事故|人身保险|健康保险|医疗保险|工伤保险|社会保险|劳动争议|劳动合同|"
    r"养老保险|生育保险|运输合同|物流|驾驶|货车|半挂|车损|道路|买卖合同|不当得利|"
    r"追偿权|借款合同|民间借贷|合伙合同|保证保险|责任保险|安全保障|产品销售者|行政|"
    r"代位求偿|提供劳务者|工程"
)

BAD_CONTENT = re.compile(
    r"机动车交通事故|道路交通事故|人身保险|健康保险|医疗保险|工伤保险|社会保险|劳动关系|劳动争议|"
    r"机动车损失险|交强险|商业三者险|驾驶员|驾驶车辆|货物运输|运输合同|物流|仓库|仓储|火灾|"
    r"雇员|雇主|责任保险|保证保险|代位求偿|追偿权|车辆损失|车辆保险|工程施工|建筑工程"
)

DIRECT_PATTERNS = {
    "玉米种植保险": re.compile(
        r"(?:玉米).{0,300}(?:种植保险|农业保险|完全成本保险)|"
        r"(?:种植保险|农业保险|完全成本保险).{0,300}(?:玉米)|"
        r"(?:投保|购买|保单|保险标的|保险单).{0,150}(?:玉米)|"
        r"(?:玉米).{0,150}(?:投保|购买|保单|保险标的|保险单)"
    ),
    "水稻种植保险": re.compile(
        r"(?:水稻|稻谷).{0,300}(?:种植保险|农业保险|完全成本保险)|"
        r"(?:种植保险|农业保险|完全成本保险).{0,300}(?:水稻|稻谷)|"
        r"(?:投保|购买|保单|保险标的|保险单).{0,150}(?:水稻|稻谷)|"
        r"(?:水稻|稻谷).{0,150}(?:投保|购买|保单|保险标的|保险单)"
    ),
    "中药材保险": re.compile(
        r"(?:中药材|中草药|人参|黄芪|金银花|枸杞|当归|柴胡|板蓝根|药材|药苗)"
        r".{0,100}(?:种植保险|农业保险|完全成本保险|中药材保险|中草药保险|保险枸杞|保险人参)|"
        r"(?:种植保险|农业保险|完全成本保险|中药材保险|中草药保险|保险枸杞|保险人参)"
        r".{0,100}(?:中药材|中草药|人参|黄芪|金银花|枸杞|当归|柴胡|板蓝根|药材|药苗)|"
        r"(?:投保|购买|保单).{0,120}(?:中药材|中草药|人参|黄芪|金银花|枸杞|当归|柴胡|板蓝根|药材|药苗)种植"
    ),
}

GROUP_LABEL = {
    "玉米种植保险": "玉米保险理赔",
    "水稻种植保险": "水稻保险理赔",
    "中药材保险": "中药材保险理赔",
}

TAG = {
    "玉米种植保险": "玉米；种植业保险；农业保险；保险合同纠纷",
    "水稻种植保险": "水稻；种植业保险；农业保险；保险合同纠纷",
    "中药材保险": "中药材；中草药；种植业保险；农业保险；保险合同纠纷",
}


def judgment_parts(raw: str) -> tuple[str, str, str]:
    text = clean(raw)
    facts = text[: text.find("本院认为")] if "本院认为" in text else text
    reason = section(text, "本院认为", ("判决如下",), 3500)
    judgment = section(text, "判决如下", ("如果未按", "如不服", "本判决为终审判决"), 1800)
    return facts, reason, judgment


def accepted(group: str, title: str, raw: str) -> bool:
    text = clean(raw)
    if not raw or "本院认为" not in text or "判决如下" not in text:
        return False
    if BAD_TITLE.search(title):
        return False
    facts = text[: text.find("本院认为")]
    if BAD_CONTENT.search(facts):
        return False
    return bool(DIRECT_PATTERNS[group].search(facts))


def raw_case(group: str, pageid: int, title: str, raw: str) -> dict:
    facts, reason, judgment = judgment_parts(raw)
    court = field(raw, "court")
    case_no = field(raw, "案号")
    year, month, day = (field(raw, k) for k in ("year", "month", "day"))
    date = f"{year}-{month.zfill(2)}-{day.zfill(2)}" if year and month and day else year
    # Keep the fact card bounded but include enough context to distinguish the
    # insured crop, dispute, and result. The original source remains linked by
    # pageid in the platform column's source mirror convention.
    facts_card = f"案情摘要：{facts[:2200]}；裁判理由摘要：{reason[:1200]}；裁判结果：{judgment[:1600]}"
    one_line = f"{date[:4] if date else ''}年，{court}审理《{clean(title)}》，涉及{GROUP_LABEL[group]}，裁判结果：{judgment[:360]}"
    return {
        "pageid": pageid,
        "group": group,
        "label": GROUP_LABEL[group],
        "tag": TAG[group],
        "facts_card": facts_card,
        "one_line": one_line,
        "court": court,
        "case_no": case_no,
        "date": date,
        "title": clean(title),
        "source": f"https://zh.wikisource.org/w/index.php?curid={pageid}",
    }


def load_clean_cases() -> tuple[list[dict], Counter, Counter]:
    with open(ANALYSIS_PATH, encoding="utf-8") as f:
        analysis = json.load(f)
    candidates = [x for x in analysis["cases"] if x["group"] in GROUP_LABEL]
    ids = [x["pageid"] for x in candidates] + EXTRA_CASE_IDS
    pages = {}
    for i in range(0, len(ids), 40):
        pages.update(fetch_pages(ids[i : i + 40]))

    accepted_cases: list[dict] = []
    raw_counts = Counter()
    for x in candidates:
        group, pageid, title = x["group"], x["pageid"], x["title"]
        raw = pages.get(pageid, "")
        if accepted(group, title, raw):
            accepted_cases.append(raw_case(group, pageid, title, raw))
        else:
            raw_counts[group] += 1

    # Add the targeted case with a standard judgment header/title from source.
    extra_raw = pages.get(EXTRA_CASE_IDS[0], "")
    if extra_raw:
        extra_title = field(extra_raw, "title") or "中国人民财产保险股份有限公司海原支公司、海原县志平种养殖场财产保险合同纠纷民事二审民事判决书"
        extra_group = "中药材保险"
        if accepted(extra_group, clean(extra_title), extra_raw):
            accepted_cases.append(raw_case(extra_group, EXTRA_CASE_IDS[0], extra_title, extra_raw))

    # Deduplicate at judgment grain and merge multi-crop judgments into one row.
    merged: OrderedDict[str, dict] = OrderedDict()
    for case in accepted_cases:
        key = case["case_no"] or str(case["pageid"])
        if key not in merged:
            case["labels"] = [case["label"]]
            case["tags"] = [case["tag"]]
            merged[key] = case
        else:
            current = merged[key]
            if case["label"] not in current["labels"]:
                current["labels"].append(case["label"])
            if case["tag"] not in current["tags"]:
                current["tags"].append(case["tag"])

    # Stable order: planting categories first, then date and case number.
    order = {"玉米保险理赔": 0, "水稻保险理赔": 1, "中药材保险理赔": 2}
    cases = sorted(merged.values(), key=lambda x: (min(order[y] for y in x["labels"]), x["date"], x["case_no"]))
    for i, case in enumerate(cases, 1):
        case["one_line"] = (
            f"{case['date'][:4] if case['date'] else ''}年，{case['court']}审理《{case['title']}》，"
            f"涉及{'、'.join(case['labels'])}，裁判结果：{section(clean(pages.get(case['pageid'], '')), '判决如下', ('如果未按', '如不服', '本判决为终审判决'), 360)}"
        )
        case["row"] = [
            f"insurance-case-20260815-{i:03d}",
            "；".join(case["labels"]),
            "；".join(case["tags"]),
            case["facts_card"],
            case["one_line"],
            case["case_no"],
            "裁判文书（维基文库镜像）",
            "",
            case["date"],
        ]
    accepted_counts = Counter(label for case in cases for label in case["labels"])
    return cases, raw_counts, accepted_counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    cases, rejected, counts = load_clean_cases()
    print(json.dumps({"clean_rows": len(cases), "category_membership": dict(counts), "rejected_source_rows": dict(rejected)}, ensure_ascii=False))
    print("sample_case_nos", [x["case_no"] for x in cases[:5]])
    if not args.write:
        return

    # Read before write to prove target schema and current extent.
    before = read_sheet("A1:I500", TOKEN, SHEET_ID)
    print("before_nonempty_rows", sum(1 for row in before if any(row)))
    rows = [x["row"] for x in cases]
    chunk = 20
    for start in range(0, len(rows), chunk):
        end = min(start + chunk, len(rows))
        first = start + 2
        last = end + 1
        result = write_cells(TOKEN, SHEET_ID, f"A{first}:I{last}", rows[start:end])
        print("write", f"A{first}:I{last}", result.get("code"), result.get("data", {}).get("revision"))

    # Independent readback of the semantic fields.
    after = read_sheet(f"A1:I{len(rows)+1}", TOKEN, SHEET_ID)
    data_rows = [row for row in after[1:] if any(row)]
    by = Counter(row[1] for row in data_rows if len(row) > 1)
    animal = [row for row in data_rows if "养殖" in (row[1] if len(row) > 1 else "")]
    print(json.dumps({"readback_rows": len(data_rows), "readback_category_cells": dict(by), "animal_rows": len(animal), "header": after[0] if after else []}, ensure_ascii=False))
    if len(data_rows) != len(rows) or animal:
        raise SystemExit("readback validation failed")


if __name__ == "__main__":
    main()
