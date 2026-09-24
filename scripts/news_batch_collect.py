"""从主流新闻媒体公开页面采集老人遗嘱、房产、存款和失能取款困难案例。"""
from __future__ import annotations

import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from urllib.parse import urlparse
from xml.etree import ElementTree

from lxml import html as lxml_html

sys.path.insert(0, str(__file__).rsplit("/scripts/", 1)[0] + "/scripts")
from case_batch_collect import cn_int, cn_numbers
from feishu_utils import write_cells

TOKEN = "GyIOwKpuXisDVUkmIWocT9udnIb"
SHEET = "KCqWhI"
START_ROW = 207
SEED_URLS = [
    "https://m.thepaper.cn/newsDetail_forward_31305564", "https://m.thepaper.cn/newsDetail_forward_30798454",
    "https://m.thepaper.cn/newsDetail_forward_32883485", "https://m.thepaper.cn/detail/33204655",
    "https://m.thepaper.cn/detail/28613849", "https://www.thepaper.cn/newsdetail_forward_27892947",
    "https://www.thepaper.cn/newsDetail_forward_32806359", "https://m.thepaper.cn/newsDetail_forward_32529092",
    "https://m.thepaper.cn/newsDetail_forward_28608596", "https://m.thepaper.cn/newsDetail_forward_29969830",
    "https://m.thepaper.cn/newsDetail_forward_23019122", "https://m.thepaper.cn/newsDetail_forward_32814968",
    "https://m.thepaper.cn/newsDetail_forward_30641614", "https://m.thepaper.cn/newsDetail_forward_23274777",
    "https://m.thepaper.cn/newsDetail_forward_32883861", "https://m.thepaper.cn/newsDetail_forward_31152668",
    "https://m.thepaper.cn/newsDetail_forward_30798454", "https://m.thepaper.cn/newsDetail_forward_31066226",
    "https://m.thepaper.cn/newsDetail_forward_31387758", "https://m.thepaper.cn/newsDetail_forward_32345560",
    "https://m.thepaper.cn/newsDetail_forward_26937986", "https://m.thepaper.cn/newsDetail_forward_32867763",
    "https://m.thepaper.cn/newsDetail_forward_31851111", "https://m.thepaper.cn/newsDetail_forward_24432000",
    "https://m.thepaper.cn/newsDetail_forward_30965867", "https://www.thepaper.cn/newsDetail_forward_26496907",
    "https://m.thepaper.cn/newsDetail_forward_32814968", "https://m.thepaper.cn/newsDetail_forward_32666865",
    "https://m.thepaper.cn/newsDetail_forward_32496291", "https://m.thepaper.cn/newsDetail_forward_2067929",
    "https://m.thepaper.cn/newsDetail_forward_31006362", "https://m.thepaper.cn/newsDetail_forward_1367107",
    "https://m.thepaper.cn/newsDetail_forward_11567027", "https://www.thepaper.cn/newsDetail_forward_25488110",
    "https://m.thepaper.cn/newsDetail_forward_33501719", "https://m.thepaper.cn/newsDetail_forward_5267601",
    "https://m.thepaper.cn/newsDetail_forward_20659933", "https://m.thepaper.cn/newsDetail_forward_29466789",
    "https://m.thepaper.cn/newsDetail_forward_24754743", "https://legal.people.com.cn/n1/2018/0410/c42510-29916417.html",
    "https://news.enorth.com.cn/system/2026/03/31/059276159.shtml", "https://www.chinanews.com.cn/sh/2026/01-06/10546870.shtml",
    "https://news.sina.com.cn/o/2025-03-22/doc-ineqpvce1614254.shtml", "https://news.cctv.com/2024/05/17/ARTIeVbLsZkhhifh9L3WTlbQ240517.shtml",
    "https://www.sohu.com/a/975616087_253235", "https://www.thepaper.cn/newsDetail_forward_2803902",
    "https://m.thepaper.cn/newsDetail_forward_32318154", "https://www.sznews.com/news/content/2019-05/07/content_21719026_0.htm",
    "https://news.ifeng.com/c/8g9scflFh4u", "https://m.thepaper.cn/newsDetail_forward_15321254",
    "https://m.thepaper.cn/newsDetail_forward_11622833",
]
QUERIES = [
    'site:thepaper.cn 遗嘱 继承 房产 老人',
    'site:thepaper.cn 银行 存款 老人 昏迷 取款',
    'site:news.qq.com 遗嘱 继承 房产 老人',
    'site:news.qq.com 银行 存款 老人 ICU 取款',
    'site:163.com 遗嘱 继承 房产 老人',
    'site:163.com 银行 存款 老人 昏迷 取钱',
    'site:sohu.com 遗嘱 继承 房产 老人',
    'site:sohu.com 老人 昏迷 银行 存款 医药费',
    'site:xinhuanet.com 遗嘱 继承 老人 房产',
    'site:people.com.cn 遗嘱 继承 老人 房产',
    '老人 立遗嘱 房产 继承 新闻',
    '老人 去世 存款 继承 银行 新闻',
    '老人 昏迷 取不出 银行 存款 新闻',
    '老人 ICU 医药费 银行 存款 新闻',
]
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"


def fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def search(query: str) -> list[tuple[str, str, str]]:
    url = "https://www.sogou.com/web?" + urllib.parse.urlencode({"query": query})
    root = lxml_html.fromstring(fetch(url))
    out = []
    for box in root.xpath('//div[contains(concat(" ", normalize-space(@class), " "), " vrwrap ")]'):
        links = box.xpath('.//h3[contains(@class,"vr-title")]//a/@href')
        if not links:
            continue
        href = links[0]
        if href.startswith('/'):
            href = 'https://www.sogou.com' + href
        try:
            req = urllib.request.Request(href, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as response:
                href = response.geturl()
                if "sogou.com/link" in href:
                    redirect_html = response.read().decode("utf-8", "ignore")
                    m = re.search(r"(?:window\.location\.replace|URL=)\\?['\"]?(https?://[^'\" )]+)", redirect_html)
                    if m:
                        href = html.unescape(m.group(1))
        except Exception:
            continue
        if not href.startswith("http"):
            continue
        title = " ".join(box.xpath('.//h3//text()'))
        snippet = " ".join(box.xpath('.//*[contains(@class,"fz-mid") or contains(@class,"str_info")]//text()'))
        out.append((href, re.sub(r"\s+", " ", title).strip(), re.sub(r"\s+", " ", snippet).strip()))
    return out


def source_name(url: str) -> str:
    host = urlparse(url).netloc.lower()
    names = {
        "thepaper.cn": "澎湃新闻", "news.qq.com": "腾讯新闻", "163.com": "网易新闻",
        "sohu.com": "搜狐新闻", "xinhuanet.com": "新华网", "people.com.cn": "人民网",
    }
    for domain, name in names.items():
        if host.endswith(domain):
            return name
    return host.replace("www.", "") or "媒体"


def parse_article(url: str, title: str, snippet: str) -> tuple[str, str, str] | None:
    try:
        raw = fetch(url)
    except Exception:
        return None
    root = lxml_html.fromstring(raw)
    for node in root.xpath("//script|//style|//noscript|//svg|//nav|//footer"):
        node.getparent().remove(node)
    ogs = root.xpath('//meta[@property="og:title"]/@content')
    real_title = ogs[0].strip() if ogs else title
    date = ""
    for sel in ['meta[property="article:published_time"]', 'meta[name="date"]', 'meta[name="publishdate"]', 'meta[name="PubDate"]']:
        if 'property=' in sel:
            attr, val = 'property', sel.split('property="')[1].split('"')[0]
        else:
            attr, val = 'name', sel.split('name="')[1].split('"')[0]
        vals = root.xpath(f'//meta[@{attr}="{val}"]/@content')
        if vals and vals[0]:
            date = vals[0][:10]
            break
    if not date:
        m = re.search(r"(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})", raw)
        if m:
            date = "-".join(m.groups())
    blocks = []
    for node in root.xpath("//article//p | //p"):
        text = re.sub(r"\s+", "", html.unescape(node.text_content()))
        if len(text) >= 18:
            blocks.append(text)
    body = "".join(dict.fromkeys(blocks))
    if len(body) < 220:
        body = re.sub(r"\s+", "", snippet)
    if len(body) < 120:
        return None
    return real_title, date, body[:900]


def make_row(seq: int, url: str, title: str, date: str, body: str) -> list:
    year_match = re.search(r"20\d{2}", date) or re.search(r"20\d{2}", body) or re.search(r"20\d{2}", title)
    year = int(year_match.group(0)) if year_match else 2020
    date_match = re.match(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", date or "")
    if date_match:
        y, m, d = map(int, date_match.groups())
        date_cn = f"{cn_int(y, year=True)}年{cn_int(m)}月{cn_int(d)}日"
    else:
        date_cn = cn_numbers(date) if date else cn_int(year, year=True) + "年（报道日期未披露）"
    source = source_name(url)
    if any(x in (title + body) for x in ["昏迷", "ICU", "失能", "取不出", "密码", "医药费"]):
        category = "ICU失能"
        keywords = "ICU失能；存款继承与取款困难；银行授权；意定监护；监护证明"
    elif any(x in (title + body) for x in ["房产", "房屋", "住房"]):
        category = "房产继承"
        keywords = "房产继承；遗嘱；多子女家庭；再婚家庭；遗产分割"
    else:
        category = "遗嘱"
        keywords = "遗嘱；遗嘱效力；房产继承；存款继承"
    detail = f"{cn_int(year, year=True)}年：{source}报道。{body}。这是一则媒体报道，不是法院判决；报道中的争议集中在{keywords.split('；')[0]}以及老人财产能否被及时、清楚地安排。"
    detail = cn_numbers(detail)
    clean_title = cn_numbers(title)
    summary = f"{cn_int(year, year=True)}年{source}报道的一起真实生活事件，核心问题是{keywords.split('；')[0]}和老人财产安排。"
    return [
        f"news-case-20260731-{seq:03d}", category, "新闻案例", clean_title,
        keywords, "重病老人；老年家庭；子女；继承人",
        f"{source}报道的真实事件，老人财产安排在关键时刻出现难题", keywords,
        detail, summary, "未披露（新闻报道，无法院判决）", "新闻", url, date_cn,
    ]


def main(target: int = 50) -> None:
    links = {url: ("", "") for url in SEED_URLS}
    for query in QUERIES:
        try:
            for url, title, snippet in search(query):
                if url not in links:
                    links[url] = (title, snippet)
        except Exception:
            continue
        time.sleep(1.2)
    rows = []
    for url, (title, snippet) in links.items():
        if "xinhuanet.com/politics/2015-05/27/c_127847347.htm" in url:
            continue
        parsed = parse_article(url, title, snippet)
        if not parsed:
            continue
        real_title, date, body = parsed
        rows.append(make_row(len(rows) + 1, url, real_title, date, body))
        if len(rows) >= target:
            break
        time.sleep(0.4)
    if len(rows) < target:
        raise RuntimeError(f"only {len(rows)} verifiable news pages found")
    end = START_ROW + len(rows) - 1
    result = write_cells(TOKEN, SHEET, f"A{START_ROW}:N{end}", rows)
    print(json.dumps({"count": len(rows), "range": f"A{START_ROW}:N{end}", "write": result}, ensure_ascii=False))


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 50)
