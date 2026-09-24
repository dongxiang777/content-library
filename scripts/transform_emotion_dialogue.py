"""将 FunASR 的说话人转写整理成用户/主播两段式文案。

这里只做角色归并和必要的空白清理，不改写原文。说话人编号不能直接
映射为角色：同一条视频里主播可能使用多个麦克风/声道编号。
"""

from __future__ import annotations

from dataclasses import dataclass
import argparse
import json
import re
from pathlib import Path
from typing import Iterable


_SPEAKER_RE = re.compile(r"【说话人(\d+)】：")

# 这些词只用于判断角色，不会从文案中删除。权重刻意偏向“对观众/对来电者说话”
# 的证据，避免把用户提到“校长”误判成主播。
_HOST_CUES = {
    "直播间": 4,
    "请大家": 4,
    "大家": 2,
    "欢迎": 2,
    "点个": 3,
    "关注": 2,
    "小爱心": 3,
    "分享给": 2,
    "我再讲": 3,
    "我认为": 2,
    "我们今天": 2,
    "结束": 3,
    "你知道": 2,
    "以前你": 3,
    "你和": 2,
    "你讲": 2,
    "你觉得": 2,
    "你要": 1,
    "你们": 2,
    "怎么样": 2,
    "对吧": 2,
    "好不好": 2,
    "哪个庙": 2,
    "真的吗": 2,
    "非常感谢": 2,
    "推荐": 1,
}

_USER_CUES = {
    "我家": 3,
    "我们家": 3,
    "我的孩子": 3,
    "我孩子": 3,
    "我儿子": 3,
    "我女儿": 3,
    "我老公": 3,
    "我丈夫": 3,
    "我以前": 2,
    "我带": 2,
    "我去了": 2,
    "我真的": 2,
    "我和": 2,
    "我也": 2,
    "遇到": 1,
    "问题": 1,
    "我看完": 2,
    "我学会": 2,
    "我想": 1,
    "我觉得": 1,
    "去年": 1,
    "今年": 1,
    "孩子": 1,
    "老公": 1,
    "丈夫": 1,
}


@dataclass(frozen=True)
class _Segment:
    speaker: str
    text: str
    order: int


@dataclass(frozen=True)
class TransformResult:
    text: str
    status: str  # auto | review | unchanged
    reason: str = ""


def parse_segments(text: str) -> list[_Segment]:
    """按原始顺序解析所有说话人段落。"""
    matches = list(_SPEAKER_RE.finditer(text or ""))
    if not matches:
        return []
    segments: list[_Segment] = []
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = _clean_piece(text[start:end])
        if body:
            segments.append(_Segment(match.group(1), body, i))
    return segments


def _clean_piece(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text


def _clean_merged_piece(text: str) -> str:
    """去掉因删除主播追问而残留在段首的应答口头禅。"""
    text = re.sub(r"^(?:也是|是的，|是的|对，好，|对，好|对，)", "", text)
    return text.strip()


def _score(text: str, cues: dict[str, int]) -> int:
    return sum(text.count(cue) * weight for cue, weight in cues.items())


def _speaker_scores(segments: Iterable[_Segment]) -> dict[str, tuple[int, int, int]]:
    grouped: dict[str, list[str]] = {}
    for segment in segments:
        grouped.setdefault(segment.speaker, []).append(segment.text)
    scores: dict[str, tuple[int, int, int]] = {}
    for speaker, pieces in grouped.items():
        text = " ".join(pieces)
        host = _score(text, _HOST_CUES) + text.count("？") * 2 + text.count("?") * 2
        user = _score(text, _USER_CUES)
        scores[speaker] = (host, user, len(text))
    return scores


def _infer_roles(segments: list[_Segment]) -> tuple[set[str], set[str], str]:
    scores = _speaker_scores(segments)
    if len(scores) < 2:
        return set(), set(), "说话人不足两个，无法区分用户和主播"

    # 先找主播：多个说话人可能都属于主播（开场、提问、总结分别由不同
    # 声道识别出来），但主播证据必须明显强于其个人经历证据。
    hosts = {
        speaker
        for speaker, (host, user, _length) in scores.items()
        if host >= 4 and host >= user
    }

    # 两人样本的补充规则：一个说话人明显像主播时，另一个就是用户。
    if not hosts and len(scores) == 2:
        ranked = sorted(scores, key=lambda s: (scores[s][0], scores[s][1]), reverse=True)
        top = ranked[0]
        if scores[top][0] >= 2 and scores[top][0] > scores[ranked[1]][0]:
            hosts = {top}

    users = set(scores) - hosts
    substantial_users = {
        speaker for speaker in users if scores[speaker][1] >= 3
    }
    if not hosts:
        return set(), set(), "没有足够的主播话术证据"
    if not substantial_users:
        return set(), set(), "没有足够的用户叙事证据"
    if len(substantial_users) > 1:
        return set(), set(), "存在多个可能的用户，不能安全合并"
    return hosts, substantial_users, ""


def transform_dialogue(text: str) -> TransformResult:
    """返回用户在前、主播在后的两段式文案。"""
    segments = parse_segments(text)
    if not segments:
        return TransformResult(text or "", "unchanged")

    hosts, users, reason = _infer_roles(segments)
    if reason:
        return TransformResult(text or "", "review", reason)

    user_text = "\n".join(
        _clean_merged_piece(s.text) for s in segments if s.speaker in users
    )
    host_text = "\n".join(
        _clean_merged_piece(s.text) for s in segments if s.speaker in hosts
    )
    result = f"用户：{user_text}\n主播：{host_text}"
    return TransformResult(result, "auto")


__all__ = ["TransformResult", "parse_segments", "transform_dialogue"]


def _cell_text(value: object) -> str:
    """提取飞书超链接单元格中的可比对文本。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        for item in value:
            result = _cell_text(item)
            if result:
                return result
    if isinstance(value, dict):
        for key in ("link", "text", "url"):
            result = _cell_text(value.get(key))
            if result:
                return result
    return ""


def build_preview(state_path: str, sheet_id: str) -> dict:
    """读取最新飞书快照，生成只包含拟更新字段的本地预览。"""
    # 延迟导入，保证纯文本单元测试不需要飞书凭证。
    from feishu_utils import read_sheet
    from config import SPREADSHEET_TOKEN

    state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    rows = read_sheet("A1:Z2000", SPREADSHEET_TOKEN, sheet_id)
    if not rows:
        raise RuntimeError("目标飞书子表为空")
    header = [str(x).strip() if x else "" for x in rows[0]]
    text_col = header.index("文案全文")
    link_col = header.index("原始链接")

    by_link: dict[str, tuple[int, str]] = {}
    for row_num, row in enumerate(rows[1:], 2):
        if link_col >= len(row):
            continue
        link = _cell_text(row[link_col])
        if link:
            by_link[link] = (row_num, str(row[text_col] if text_col < len(row) else ""))

    records = []
    stats = {"auto": 0, "review": 0, "unchanged": 0, "missing_row": 0}
    for oid, item in state.get("items", {}).items():
        if item.get("sheet_id") != sheet_id:
            continue
        raw = item.get("transcript") or ""
        if "【说话人" not in raw:
            continue
        result = transform_dialogue(raw)
        link = item.get("share_url") or ""
        row_info = by_link.get(link)
        row_num, current = row_info if row_info else (None, "")
        status = result.status
        reason = result.reason
        if row_num is None:
            status = "review"
            reason = "飞书中找不到对应原始链接"
            stats["missing_row"] += 1
        stats[status] = stats.get(status, 0) + 1
        records.append({
            "object_id": oid,
            "row": row_num,
            "share_url": link,
            "creator": item.get("nickname") or "",
            "status": status,
            "reason": reason,
            "current_text": current,
            "source_text": raw,
            "new_text": result.text if status == "auto" else current,
        })
    return {"sheet_id": sheet_id, "stats": stats, "records": records}


def write_preview(preview: dict, output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(preview, ensure_ascii=False, indent=2), encoding="utf-8")


def write_back(preview: dict) -> dict:
    """只更新文案全文，并跳过预览后发生变化的行。"""
    from feishu_utils import get_column_map, read_sheet, write_row_fields
    from config import SPREADSHEET_TOKEN

    sheet_id = preview["sheet_id"]
    col_map = get_column_map(SPREADSHEET_TOKEN, sheet_id)
    rows = read_sheet("A1:Z2000", SPREADSHEET_TOKEN, sheet_id)
    header = [str(x).strip() if x else "" for x in rows[0]]
    text_col = header.index("文案全文")
    results = {"written": 0, "skipped_conflict": 0, "skipped_review": 0, "failed": []}
    for record in preview["records"]:
        if record["status"] != "auto" or not record.get("row"):
            results["skipped_review"] += 1
            continue
        row_num = int(record["row"])
        row = rows[row_num - 1] if row_num - 1 < len(rows) else []
        current = str(row[text_col] if text_col < len(row) else "")
        if current != record["current_text"]:
            results["skipped_conflict"] += 1
            continue
        if current == record["new_text"]:
            continue
        try:
            write_row_fields(
                SPREADSHEET_TOKEN,
                sheet_id,
                row_num,
                {"文案全文": record["new_text"]},
                col_map,
            )
            results["written"] += 1
        except Exception as exc:  # pragma: no cover - live API failure path
            results["failed"].append({"row": row_num, "error": str(exc)})
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="整理情感 IP 多轮对话文案")
    parser.add_argument("--state", default="data/channels_state.json")
    parser.add_argument("--sheet-id", default="sy4S58")
    parser.add_argument("--preview", default="data/emotion_dialogue_preview.json")
    parser.add_argument("--write", action="store_true", help="写回通过检查的文案全文")
    args = parser.parse_args()

    preview = build_preview(args.state, args.sheet_id)
    write_preview(preview, args.preview)
    print(json.dumps(preview["stats"], ensure_ascii=False))
    if args.write:
        print(json.dumps(write_back(preview), ensure_ascii=False))


if __name__ == "__main__":
    main()
