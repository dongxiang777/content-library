import concurrent.futures
import json
import os
import re
import subprocess

from feishu_utils import get_column_map, read_sheet, write_row_fields

TARGET = "RwZFsg8klhpzWVtHrpUcnguAn4g"
COLLECTION = "HxHmsSRjrhqsPkt8gDEcUIL8nMc"
SHEET = "sy4S58"
ROOT = "/Users/shaoxinjiang/CodexWorkspace/projects/content library"
DIGITS = "零一二三四五六七八九"


def int_zh(n):
    n = int(n)
    if n == 0:
        return "零"
    units = ["", "十", "百", "千", "万", "亿"]
    if n < 10000:
        out, zero = "", False
        for pos, ch in enumerate(str(n)[::-1]):
            d = int(ch)
            if d:
                out = DIGITS[d] + units[pos] + (("零" + out) if zero and out else out)
                zero = False
            else:
                zero = True
        return out
    if n < 100000000:
        a, b = divmod(n, 10000)
        return int_zh(a) + "万" + (("零" + int_zh(b)) if b and b < 1000 else (int_zh(b) if b else ""))
    a, b = divmod(n, 100000000)
    return int_zh(a) + "亿" + (("零" + int_zh(b)) if b and b < 10000000 else (int_zh(b) if b else ""))


def numbers_zh(text):
    text = re.sub(r"(?<!\d)(\d{4})(?=年)", lambda m: "".join(DIGITS[int(x)] for x in m.group(1)), text)
    text = re.sub(r"(\d+(?:\.\d+)?)\s*%", lambda m: "百分之" + m.group(1).replace(".", "点"), text)
    text = re.sub(r"\d+", lambda m: int_zh(m.group(0)), text)
    return text


def run_one(item):
    row, source = item
    dialogue = source.startswith("用户：") and "主播：" in source
    mode = "这是双段连麦文案，必须保留且只输出一段‘用户：’和一段‘主播：’。" if dialogue else "这是单段口播，必须保持单段，不要添加用户或主播标签。"
    prompt = f"""你是中文短视频文案的资深校对编辑。请校对下面第{row}行文案。

{mode}
要求：
一、保留原文百分之九十以上的信息、故事、冲突、反转、观点、互动和结尾，不要总结，不要改成提纲。
二、删掉没有信息的口头填充词、连续重复和明显的语音转写噪音，但保留自然的情绪语气和短视频节奏。
三、根据上下文修正明显 ASR 错词、错字、残句和不通顺表达；不确定的词不要擅自改成新事实。
四、如果是双段文案，用户的经历、问题和回答只能放在用户段；主播的开场、提问、分析、建议、互动和结尾只能放在主播段。
五、所有阿拉伯数字都必须改成中文数字；所有用户对主播的称谓、主播自称和主播旧称谓统一为“老师”。
六、只输出最终文案，不要解释、不要 Markdown 代码块、不要评价。

原文：
{source}
"""
    p = subprocess.run(
        ["codex", "exec", "--ephemeral", "-s", "read-only", "-C", ROOT, prompt],
        text=True, capture_output=True, timeout=240,
    )
    out = p.stdout.strip()
    out = re.sub(r"^```[^\n]*\n?|\n?```$", "", out).strip()
    out = numbers_zh(out)
    if dialogue:
        valid = out.startswith("用户：") and "\n主播：" in out and out.count("用户：") == 1 and out.count("主播：") == 1
    else:
        valid = not out.startswith("用户：") and "主播：" not in out
    valid = valid and "说话人" not in out and len(out) >= max(80, int(len(source) * 0.38)) and len(out) <= int(len(source) * 1.8)
    return row, out, valid, p.stderr[-300:]


def main():
    rows = read_sheet("A1:Z292", TARGET, SHEET)
    h = [str(x).strip() if x else "" for x in rows[0]]
    i = h.index("文案全文")
    items = [(n, str(row[i] if i < len(row) else "")) for n, row in enumerate(rows[1:], 2) if str(row[i] if i < len(row) else "")]
    cm = get_column_map(TARGET, SHEET)
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(run_one, item) for item in items]
        for k, future in enumerate(concurrent.futures.as_completed(futures), 1):
            row, out, valid, err = future.result()
            results.append((row, out, valid))
            print(f"{k}/{len(items)} row={row} {'WRITE' if valid else 'SKIP'}", flush=True)
    written = 0
    for row, out, valid in sorted(results):
        if valid:
            write_row_fields(TARGET, SHEET, row, {"文案全文": out}, cm)
            written += 1
    # 只在爆款表全部写回后，按行同步采集表。
    target_rows = read_sheet("A1:Z292", TARGET, SHEET)
    collection_rows = read_sheet("A1:Z292", COLLECTION, SHEET)
    th = [str(x).strip() if x else "" for x in target_rows[0]]
    ch = [str(x).strip() if x else "" for x in collection_rows[0]]
    ti, ci = th.index("文案全文"), ch.index("文案全文")
    c_map = get_column_map(COLLECTION, SHEET)
    synced = 0
    for row in range(2, min(len(target_rows), len(collection_rows)) + 1):
        v = str(target_rows[row - 1][ti] if ti < len(target_rows[row - 1]) else "")
        old = str(collection_rows[row - 1][ci] if ci < len(collection_rows[row - 1]) else "")
        if v and v != old:
            write_row_fields(COLLECTION, SHEET, row, {"文案全文": v}, c_map)
            synced += 1
    print(json.dumps({"total": len(items), "written": written, "synced": synced}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
