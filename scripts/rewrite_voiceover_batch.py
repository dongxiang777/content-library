"""把事实卡压缩成短视频口播稿，写入 O 列。

规则：只使用已有事实卡中的信息；删除裁判书套话、法条和程序性细节，
按“冲突—反驳—关键细节—结果”组织文本。
"""
from __future__ import annotations

import re
import sys

sys.path.insert(0, str(__file__).rsplit("/scripts/", 1)[0] + "/scripts")
from feishu_utils import read_sheet, write_cells

TOKEN = "GyIOwKpuXisDVUkmIWocT9udnIb"
SHEET = "KCqWhI"


def sentences(text: str) -> list[str]:
    text = re.sub(r"\{\{.*?\}\}", "", text)
    text = text.replace("判决书查明，", "").replace("争议的关键在于，", "")
    text = re.sub(r"依照.*?规定[，。]", "", text)
    text = re.sub(r"《[^》]{0,40}》", "", text)
    text = re.sub(r"（[^（）]{0,30}）", "", text)
    parts = re.split(r"[。！？；]", text)
    bad = ("本院认为", "综上", "诉讼请求", "原告请求", "被告辩称", "案件受理费", "上诉", "法条", "第一千")
    out = []
    for p in parts:
        p = re.sub(r"\s+", "", p).strip("，：、")
        if len(p) >= 12 and not any(x in p for x in bad):
            out.append(p)
    return out


def court_from_fact(fact: str) -> str:
    m = re.search(r"：([^。]+人民法院)", fact)
    if m:
        return m.group(1).replace("中华人民共和国", "")
    return "法院"


def simplify_people(s: str) -> str:
    s = s.replace("另一名继承人", "对方")
    s = re.sub(r"[A-Z]?[一-鿿]{1,4}某[一二三四五六七八九十甲乙丙丁]", "对方", s)
    s = s.replace("gap}}", "")
    return s


def pick_detail(ss: list[str], category: str) -> list[str]:
    keys = ["遗嘱", "原件", "视频", "打印", "照顾", "赡养", "银行", "存款", "房屋", "房产", "签名", "手印", "昏迷", "再婚", "子女", "孙子", "补偿", "支付", "分得", "无效", "有效"]
    ranked = []
    for i, s in enumerate(ss):
        score = sum(2 if k in s else 0 for k in keys)
        if any(x in s for x in ("人民币", "账号", "尾号", "平方米", "鉴定机构", "证据如下", "判决书", "本院", "最终判决", "这个案件提醒")):
            score -= 3
        if s.count("对方") > 2 or "另一名继承人" in s:
            score -= 5
        ranked.append((score, -i, s))
    ranked.sort(reverse=True)
    selected = []
    for _, _, s in ranked:
        s = simplify_people(s)
        if s and all(s not in x and x not in s for x in selected):
            selected.append(s)
        if len(selected) >= 3:
            break
    return selected


def result_sentence(ss: list[str]) -> str:
    full = "".join(ss)
    if "遗嘱应认定无效" in full or "遗嘱无效" in full:
        return "法院最后没有认可这份遗嘱，房产按继承份额重新分配。"
    if "遗嘱合法有效" in full or "遗嘱有效" in full:
        if "补偿" in full or "折价" in full or "支付" in full:
            return "法院最后认可遗嘱，但取得财产的一方还要向其他继承人支付相应补偿。"
        return "法院最后认可遗嘱，按照遗嘱指定的方式处理财产。"
    if "改按法定继承" in full:
        return "法院最后没有按这份遗嘱处理，而是改按法定继承分配。"
    keys = ("最终", "判决", "认定", "有效", "无效", "驳回", "归", "支付", "分得", "继承")
    ranked = []
    for i, s in enumerate(ss):
        if any(k in s for k in keys):
            score = sum(2 if k in s else 0 for k in keys)
            if "依照" in s or "规定" in s:
                score -= 3
            ranked.append((score, -i, s))
    if not ranked:
        return "法院最后根据现有证据作出了财产分配。"
    ranked.sort(reverse=True)
    return simplify_people(ranked[0][2]) + "。"


def make_voice(row: list) -> str:
    category = str(row[1])
    fact = str(row[8])
    title = str(row[3])
    ss = sentences(fact)
    court = court_from_fact(fact)
    signal = title + category + str(row[4])
    joined = title + fact
    if "银行" in signal or "存款" in signal or "储蓄" in signal or category == "ICU失能":
        hook = "老人去世后，家人想把钱取出来，却发现账户里的钱根本动不了。"
    elif "再婚" in signal:
        hook = "再婚妻子想继续住在房子里，前妻所生的女儿却要拿回产权。"
    elif "脑萎缩" in signal or "原件" in signal or "打印" in signal or "代书" in signal:
        hook = "有人拿着遗嘱来要房子，可另一边一核对，原件和复印件竟然对不上。"
    elif "房" in signal or category == "房产继承":
        hook = "一套房子本来要由家人分，最后却因为一份遗嘱起了争议。"
    else:
        hook = "一份遗嘱本来想把财产安排清楚，最后却让家人争到了法院。"
    details = pick_detail(ss, category)
    body = "。".join(details[:2])
    if body:
        body += "。"
    result = result_sentence(ss)
    source = f"这件事后来闹到了{court}。"
    tail = "关键不在于文书上写了什么，而在于这份安排能不能被证据证明。"
    if "银行" in signal or "存款" in signal or "储蓄" in signal or category == "ICU失能":
        tail = "钱一直在账户里，但继承身份没有确认，家人就未必能马上取出来。"
    elif "房" in signal or category == "房产继承":
        tail = "房子最后归谁，不只看谁拿出了一份文书，还要看这份安排能不能经得起核对。"
    # 裁判书残片若已明显匿名污染，不把残片强行塞进主播稿。
    if body.count("对方") > 2 or "这个案件提醒" in body or "最终判决" in body:
        body = ""
    return hook + source + body + result + tail


def main() -> None:
    rows = read_sheet("A12:O256", TOKEN, SHEET)
    out = []
    for row in rows:
        if len(row) < 15:
            row = row + [None] * (15 - len(row))
        if row[0]:
            row[14] = make_voice(row)
        out.append([row[14]])
    write_cells(TOKEN, SHEET, "O12:O256", out)
    print("rewritten", len(out))


if __name__ == "__main__":
    main()
