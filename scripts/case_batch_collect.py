"""从维基文库批量采集继承类裁判文书并写入指定飞书子表。

只接受含裁判文书头部、正文、判决部分的页面；搜索摘要和空页面不入库。
"""
from __future__ import annotations

import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from typing import Iterable

sys.path.insert(0, str(__file__).rsplit("/scripts/", 1)[0] + "/scripts")
from feishu_utils import append_rows, get_column_map, read_sheet

TOKEN = "GyIOwKpuXisDVUkmIWocT9udnIb"
SHEET = "KCqWhI"
API = "https://zh.wikisource.org/w/api.php"
UA = "CaseResearchBot/1.0"
SEARCH_TERMS = [
    "遗嘱继承纠纷", "法定继承纠纷", "房产继承纠纷", "房屋继承纠纷",
    "代书遗嘱", "公证遗嘱", "自书遗嘱", "遗嘱无效", "遗产分割纠纷",
    "遗赠扶养协议纠纷", "银行储蓄存款合同纠纷", "存款继承纠纷",
]
EXISTING = {2326162, 3521625, 4069251, 2777322}


def api(params: dict) -> dict:
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=40) as response:
                return json.loads(response.read())
        except Exception as exc:
            if attempt == 4:
                raise
            # 维基文库对连续请求会返回四二九，批量抓取时主动退避。
            time.sleep(8 + attempt * 5 if "429" in str(exc) else 2 + attempt * 2)
    raise RuntimeError("unreachable")


def search_ids(limit: int = 500) -> list[tuple[int, str]]:
    found: dict[int, str] = {}
    for term in SEARCH_TERMS:
        cont: dict = {}
        while len(found) < limit:
            params = {
                "action": "query", "list": "search", "srsearch": term,
                "srnamespace": 0, "srlimit": 100, "format": "json",
            }
            params.update(cont)
            data = api(params)
            for item in data.get("query", {}).get("search", []):
                title = item.get("title", "")
                if "判决书" in title or "裁定书" in title:
                    found[int(item["pageid"])] = title
            if "continue" not in data:
                break
            cont = data["continue"]
        if len(found) >= limit:
            break
        time.sleep(1.2)
    return [(pid, title) for pid, title in found.items() if pid not in EXISTING]


def fetch_pages(pageids: list[int]) -> dict[int, str]:
    data = api({
        "action": "query", "pageids": "|".join(map(str, pageids)),
        "prop": "revisions", "rvprop": "content", "rvslots": "main",
        "format": "json",
    })
    result = {}
    for pid, page in data.get("query", {}).get("pages", {}).items():
        revisions = page.get("revisions", [])
        if revisions:
            result[int(pid)] = revisions[0].get("slots", {}).get("main", {}).get("*", "")
    return result


def header(text: str, key: str) -> str:
    match = re.search(r"\|\s*" + re.escape(key) + r"\s*=\s*([^\n|}]+)", text)
    return match.group(1).strip() if match else ""


def strip_markup(text: str) -> str:
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\[\[([^]|]+)\|([^]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^]]+)\]\]", r"\1", text)
    text = re.sub(r"\[https?://[^ ]+ ([^]]+)\]", r"\1", text)
    text = text.replace("&nbsp;", " ")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def cut(text: str, start: str, end: str, max_len: int = 520) -> str:
    a = text.find(start)
    if a < 0:
        return ""
    a += len(start)
    b = text.find(end, a) if end else len(text)
    if b < 0:
        b = len(text)
    return strip_markup(text[a:b])[:max_len]


CN_DIGITS = "零一二三四五六七八九"


def cn_int(n: int, year: bool = False) -> str:
    if year:
        return "".join("〇" if c == "0" else CN_DIGITS[int(c)] for c in str(n))
    if n == 0:
        return "零"
    units = ["", "十", "百", "千", "万", "十万", "百万", "千万", "亿"]
    if n < 10000:
        digits = list(map(int, str(n)))
        out = ""
        zero = False
        for i, d in enumerate(digits):
            pos = len(digits) - i - 1
            if d == 0:
                if out:
                    zero = True
                continue
            if zero:
                out += "零"
                zero = False
            if not (d == 1 and pos == 1 and not out):
                out += CN_DIGITS[d]
            out += ["", "十", "百", "千"][pos]
        return out
    if n < 100000000:
        high, low = divmod(n, 10000)
        return cn_int(high) + "万" + (cn_int(low) if low else "")
    high, low = divmod(n, 100000000)
    return cn_int(high) + "亿" + (cn_int(low) if low else "")


def amount_repl(match: re.Match) -> str:
    raw = match.group(1).replace(",", "")
    try:
        val = float(raw)
    except ValueError:
        return match.group(0)
    if val >= 100000:
        return cn_int(int(round(val / 10000))) + "万元"
    rounded = int(round(val / 1000) * 1000)
    if rounded == 0:
        rounded = 1000
    return cn_int(rounded) + "元"


def cn_numbers(text: str) -> str:
    # 年份按逐字读法写“二〇二四年”，不能写成“二千零二十四年”。
    text = re.sub(r"(\d{4})\s*年", lambda m: cn_int(int(m.group(1)), year=True) + "年", text)
    text = re.sub(r"(?<!\d)(\d{2})\s*年", lambda m: "二〇" + CN_DIGITS[int(m.group(1)[0])] + CN_DIGITS[int(m.group(1)[1])] + "年", text)
    text = re.sub(r"(\d[\d,]*(?:\.\d+)?)\s*元", amount_repl, text)
    text = re.sub(r"(\d[\d,]*)\s*万元", lambda m: cn_int(int(m.group(1).replace(",", ""))) + "万元", text)
    text = re.sub(r"(\d[\d,]*)\s*%", lambda m: "百分之" + cn_int(int(m.group(1).replace(",", ""))), text)
    text = re.sub(r"\d[\d,]*", lambda m: cn_int(int(m.group(0).replace(",", ""))), text)
    return text


def anonymize(text: str, title: str) -> str:
    # 判决页普遍使用“某”加编号，口播只保留首位主角，其余改成关系中性的继承人称呼。
    tokens = []
    for token in re.findall(r"[A-Z]?[一-鿿]{1,4}某[0-9甲乙丙丁]?...", text):
        if token not in tokens:
            tokens.append(token)
    # 更稳妥地处理常见人名形态，避免把“某某银行”等机构当成人物。
    tokens = []
    for token in re.findall(r"[A-Z]?[一-鿿]{1,4}某(?:[0-9甲乙丙丁])?", text):
        if token.endswith("银行") or token in {"某某银行", "某某律师事务所"}:
            continue
        if token not in tokens:
            tokens.append(token)
    if not tokens:
        return text
    main = re.search(r"([A-Z]?[一-鿿]{1,3})某", title)
    main_token = main.group(0) if main else tokens[0]
    mapping = {main_token: main_token.rstrip("0123456789甲乙丙丁")}
    for token in tokens:
        mapping.setdefault(token, "另一名继承人")
    for token in sorted(mapping, key=len, reverse=True):
        text = text.replace(token, mapping[token])
    return text


def classify(title: str) -> tuple[str, str]:
    if "存款" in title or "储蓄" in title or "银行" in title:
        return "存款继承与取款困难", "存款继承与取款困难；银行存款；继承资格"
    if "房" in title:
        return "房产继承", "房产继承；继承纠纷；房屋分割"
    if "遗赠扶养" in title:
        return "遗嘱", "遗赠扶养协议；遗嘱；继承纠纷"
    return "遗嘱", "遗嘱；遗嘱效力；法定继承"


def make_row(pageid: int, title: str, text: str, seq: int) -> list:
    court, kind, case_no, year, month, day = (header(text, k) for k in ("court", "type", "案号", "year", "month", "day"))
    if not court or not case_no or "判决如下" not in text or "本院认为" not in text:
        raise ValueError("missing_verifiable_fields")
    category, keywords = classify(title)
    clean_title = cn_numbers(re.sub(r"某[0-9甲乙丙丁]", "某", title))
    facts = cut(text, "经审理查明", "本院认为", 620)
    if not facts:
        # 部分判决书直接从原告诉称跳到“本院认为”，不能因为缺少固定小标题而丢掉案情。
        before_reason = text[:text.find("本院认为")] if "本院认为" in text else text
        for marker in ("事实及理由：", "事实和理由：", "原告向本院提出诉讼请求："):
            pos = before_reason.find(marker)
            if pos >= 0:
                before_reason = before_reason[pos + len(marker):]
                break
        facts = strip_markup(before_reason[-900:])
    reason = cut(text, "本院认为", "判决如下", 520)
    judgment = cut(text, "判决如下：", "如果未按", 360) or cut(text, "判决如下", "如果未按", 360)
    facts = cn_numbers(anonymize(facts, title))
    reason = cn_numbers(anonymize(reason, title))
    judgment = cn_numbers(anonymize(judgment, title))
    detail = f"{cn_int(int(year), year=True)}年：{court}。这是一起{category}案件。判决书查明，{facts}。争议的关键在于，{reason}。最终判决部分载明：{judgment}。这个案件提醒人们，涉及老人房产、遗嘱或存款时，人物关系、证据形成过程和继承手续，都会直接影响最后能不能把财产真正拿到手。"
    detail = re.sub(r"\s+", "", detail)
    summary = f"{cn_int(int(year), year=True)}年，{court}审理了一起{clean_title}，争议焦点集中在{keywords.split('；')[0]}和继承资格。"
    date = f"{cn_int(int(year), year=True)}年{cn_int(int(month))}月{cn_int(int(day))}日"
    return [
        f"wiki-case-20260731-{seq:03d}", category, "裁判案例", clean_title,
        keywords, "有老人财产传承需求的家庭；多子女家庭；继承人",
        f"明明有{keywords.split('；')[0]}，家人却因为手续和证据问题闹上法庭", keywords,
        detail, summary, case_no, "裁判文书（维基文库）",
        f"https://zh.wikisource.org/w/index.php?curid={pageid}", date,
    ]


def main(target: int = 300) -> None:
    pairs = search_ids(max(target * 2 + 20, 100))
    rows = []
    used = set()
    # 单页正文很长，十页一批更稳定，也避免接口因响应过大超时。
    for batch_start in range(0, len(pairs), 10):
        batch = pairs[batch_start:batch_start + 10]
        pages = fetch_pages([p[0] for p in batch])
        for pageid, title in batch:
            if len(rows) >= target:
                break
            text = pages.get(pageid, "")
            try:
                row = make_row(pageid, title, text, len(rows) + 1)
            except Exception:
                continue
            if row[10] in used:
                continue
            used.add(row[10])
            rows.append(row)
            if len(rows) % 25 == 0:
                print("verified", len(rows), flush=True)
        time.sleep(1.5)
        if len(rows) >= target:
            break
    if len(rows) < target:
        raise RuntimeError(f"only {len(rows)} verifiable cases found")
    result = append_rows(TOKEN, SHEET, rows)
    print(json.dumps({"count": len(rows), "append": result}, ensure_ascii=False))


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 300)
