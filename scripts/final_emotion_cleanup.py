import json
import re
from pathlib import Path

from feishu_utils import get_column_map, read_sheet, write_row_fields

TARGET = "RwZFsg8klhpzWVtHrpUcnguAn4g"
COLLECTION = "HxHmsSRjrhqsPkt8gDEcUIL8nMc"
SHEET = "sy4S58"
DIGITS = "零一二三四五六七八九"


def int_zh(value: str) -> str:
    n = int(value)
    if n == 0:
        return "零"
    units = ["", "十", "百", "千", "万", "亿"]
    if n < 10000:
        out, zero = "", False
        ds = str(n)
        for pos, ch in enumerate(ds[::-1]):
            d = int(ch)
            if d:
                piece = DIGITS[d] + units[pos]
                out = piece + ("零" if zero and out else "") + out
                zero = False
            else:
                zero = True
        return out
    if n < 100000000:
        a, b = divmod(n, 10000)
        return int_zh(str(a)) + "万" + (("零" + int_zh(str(b))) if b and b < 1000 else (int_zh(str(b)) if b else ""))
    a, b = divmod(n, 100000000)
    return int_zh(str(a)) + "亿" + (("零" + int_zh(str(b))) if b and b < 10000000 else (int_zh(str(b)) if b else ""))


def number_zh(text: str) -> str:
    def decimal(m):
        a, b = m.group(1).split(".")
        return int_zh(a) + "点" + "".join(DIGITS[int(x)] for x in b)
    text = re.sub(r"(\d+(?:\.\d+)?)\s*%", lambda m: "百分之" + decimal(re.match(r"(.*)", m.group(1))), text)
    text = re.sub(r"(?<!\d)(\d{4})(?=年)", lambda m: "".join(DIGITS[int(x)] for x in m.group(1)), text)
    text = re.sub(r"(?<![\w])(\d+\.\d+)", decimal, text)
    return re.sub(r"\d+", lambda m: int_zh(m.group(0)), text)


def clean(text: str) -> str:
    # 保留角色标签和段落换行；之前把所有空白压成一个空格，导致
    # “用户：……主播：……”黏成同一段，表面上像角色混杂。
    text = text or ""
    text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    # 只删独立的填充词和连续重复，不删有语义的感叹。
    text = re.sub(r"(?:^|[，。！？])(?:嗯+|呃+|那个|就是那个)(?=[，。！？])", "", text)
    text = re.sub(r"(对|嗯|啊|哎)(?:[，、。]?\1){1,}", r"\1", text)
    text = re.sub(r"(我|你|他|她|它|的|不|了|就是|没有|什么|怎么)(?:[，、。]?\1)+", r"\1", text)
    text = re.sub(r"(然后){2,}", "然后", text)
    text = text.replace("性梗", "心梗").replace("新儿子", "亲儿子")
    text = text.replace("搓了一杯", "搓了一把").replace("打把手", "搭把手")
    text = text.replace("思顿人格", "独立人格").replace("制冻", "制动")
    text = text.replace("还想纹吗", "还想闻吗").replace("这个个", "这个")
    text = text.replace("完备", "陪伴")
    for alias in ("校长哥哥", "校长姐姐", "校长", "白大姐", "李姐", "马姐", "娜姐"):
        text = text.replace(alias, "老师")
    return number_zh(text)


HOST_CUES = ("直播间", "公屏", "点个", "关注", "大家", "我告诉你", "我认为", "你要", "你们", "对吧", "为什么", "怎么样", "认同")
USER_CUES = ("我家", "我老公", "我丈夫", "我儿子", "我女儿", "我婆婆", "我对象", "我今年", "我以前", "我现在", "我想", "我觉得")


def parse_source(source: str):
    parts = re.split(r"【说话人(\d+)】：", source or "")
    segments = [(parts[i], parts[i + 1].strip()) for i in range(1, len(parts) - 1, 2) if parts[i + 1].strip()]
    if not segments:
        return None
    first = segments[0][0]
    hosts, users = [], []
    # 开场说话人通常是主播；其他声道按每段语义判断，保留多声道主播的销售/互动段。
    for speaker, body in segments:
        h = sum(body.count(x) for x in HOST_CUES) + 2 * (body.count("？") + body.count("?"))
        u = sum(body.count(x) for x in USER_CUES)
        if speaker == first or (h >= u + 2 and (h >= 2 or "大家" in body)):
            hosts.append(body)
        else:
            users.append(body)
    if not users:
        return clean("\n".join(hosts))
    return "用户：" + clean("\n".join(users)) + "\n主播：" + clean("\n".join(hosts))


def main():
    preview = json.loads(Path("data/emotion_dialogue_preview.json").read_text())
    source_by_row = {x.get("row"): x.get("source_text", "") for x in preview.get("records", []) if x.get("row")}
    rows = read_sheet("A1:Z292", TARGET, SHEET)
    header = [str(x).strip() if x else "" for x in rows[0]]
    text_col = header.index("文案全文")
    target_map = get_column_map(TARGET, SHEET)
    rewritten = 0
    for row_num, row in enumerate(rows[1:], 2):
        current = str(row[text_col] if text_col < len(row) else "")
        source = source_by_row.get(row_num)
        candidate = parse_source(source) if source and "【说话人" in source else None
        if candidate and (candidate.startswith("用户：") or not current.startswith("用户：")):
            if candidate != current:
                write_row_fields(TARGET, SHEET, row_num, {"文案全文": candidate}, target_map)
                rewritten += 1
        elif current:
            if current.startswith("用户：") and "主播：" in current and "\n主播：" not in current:
                current = current.replace("主播：", "\n主播：", 1)
            cleaned = clean(current)
            if cleaned != current:
                write_row_fields(TARGET, SHEET, row_num, {"文案全文": cleaned}, target_map)
                rewritten += 1
    # 完成爆款表后，按行同步采集表。
    target_rows = read_sheet("A1:Z292", TARGET, SHEET)
    collection_rows = read_sheet("A1:Z292", COLLECTION, SHEET)
    th = [str(x).strip() if x else "" for x in target_rows[0]]
    ch = [str(x).strip() if x else "" for x in collection_rows[0]]
    ti, ci = th.index("文案全文"), ch.index("文案全文")
    collection_map = get_column_map(COLLECTION, SHEET)
    synced = 0
    for row_num in range(2, min(len(target_rows), len(collection_rows)) + 1):
        value = str(target_rows[row_num - 1][ti] if ti < len(target_rows[row_num - 1]) else "")
        old = str(collection_rows[row_num - 1][ci] if ci < len(collection_rows[row_num - 1]) else "")
        if value and value != old:
            write_row_fields(COLLECTION, SHEET, row_num, {"文案全文": value}, collection_map)
            synced += 1
    print({"rewritten": rewritten, "synced": synced})


if __name__ == "__main__":
    main()
