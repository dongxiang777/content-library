#!/usr/bin/env python3
"""
FunASR 长驻 daemon — 模型只加载一次，通过 stdin/stdout JSON 接收转写请求。
由 transcribe.py 通过 subprocess.Popen 启动，避免每次转写都重新加载 2GB 模型。

支持说话人分离 (speaker diarization)：加载 cam++ 说话人模型，
单人视频返回纯文本，多人（连麦）视频返回带说话人标签的分段文案。

协议 (每行一个 JSON):
  stdin:  {"action": "transcribe", "audio_path": "/path/to.wav"}
  stdout: {"ok": true, "text": "转写文本...", "speakers": 1}
  stdout: {"ok": true, "text": "【说话人1】：...\n【说话人2】：...", "speakers": 2}
  stdin:  {"action": "quit"}
"""
import sys
import os
import io
import json

# 关闭代理（ModelScope 需要直连）
for k in list(os.environ):
    if "proxy" in k.lower():
        del os.environ[k]

# 抑制模型加载时的 stdout 输出（funasr 会打印版本号）
_real_stdout = sys.stdout
_real_stderr = sys.stderr
sys.stdout = io.StringIO()
sys.stderr = io.StringIO()
try:
    from funasr import AutoModel
    model = AutoModel(
        model="paraformer-zh",
        vad_model="fsmn-vad",
        punc_model="ct-punc",
        spk_model="cam++",
        disable_update=True,
    )
finally:
    _load_stderr = sys.stderr.getvalue()
    sys.stdout = _real_stdout
    sys.stderr = _real_stderr
if _load_stderr:
    print(f"[daemon:load] {_load_stderr[:200]}", file=sys.stderr)


def _clean_text(text: str) -> str:
    """清理 FunASR 输出中可能残留的版本号前缀。"""
    for prefix in ["funasr version:", "FunASR version:"]:
        if text.startswith(prefix):
            text = text[text.index("\n") + 1:] if "\n" in text else text.split(". ", 1)[-1]
            break
    return text.strip()


def _transcribe(audio_path: str) -> dict:
    """转写并做说话人分离，返回 {"text": ..., "speakers": N}。"""
    result = model.generate(input=audio_path, batch_size_s=300)
    if not result or not isinstance(result, list) or len(result) == 0:
        return {"text": "", "speakers": 0}

    item = result[0] if isinstance(result[0], dict) else {}
    plain_text = _clean_text(item.get("text", ""))

    # 提取 sentence_info（含说话人标签）
    sentence_info = item.get("sentence_info") or []
    if not sentence_info:
        return {"text": plain_text, "speakers": 1}

    # 统计说话人数量
    spk_ids = sorted(set(s.get("spk", 0) for s in sentence_info))
    num_speakers = len(spk_ids)

    if num_speakers <= 1:
        # 单人：返回纯文本（兼容现有流程）
        return {"text": plain_text, "speakers": 1}

    # 多人（连麦）：按说话人分段，合并相邻同说话人的句子
    segments = []
    current_spk = None
    current_texts = []

    for sent in sentence_info:
        spk = sent.get("spk", 0)
        text = _clean_text(sent.get("text", ""))
        if not text:
            continue
        if spk != current_spk:
            if current_texts:
                segments.append((current_spk, "".join(current_texts)))
            current_spk = spk
            current_texts = [text]
        else:
            current_texts.append(text)
    if current_texts:
        segments.append((current_spk, "".join(current_texts)))

    # 格式化输出：【说话人1】：xxx
    lines = []
    for spk, text in segments:
        label = f"说话人{spk_ids.index(spk) + 1}"
        lines.append(f"【{label}】：{text}")

    formatted = "\n".join(lines)
    return {"text": formatted, "speakers": num_speakers}


def main():
    # 发就绪信号
    print(json.dumps({"status": "ready"}), flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
            action = cmd.get("action", "")

            if action == "quit":
                print(json.dumps({"status": "bye"}), flush=True)
                break
            elif action == "ping":
                print(json.dumps({"status": "ok"}), flush=True)
            elif action == "transcribe":
                audio_path = cmd["audio_path"]
                result = _transcribe(audio_path)
                print(json.dumps({
                    "ok": True,
                    "text": result["text"],
                    "speakers": result["speakers"],
                }, ensure_ascii=False), flush=True)
            else:
                print(json.dumps({"ok": False, "error": f"unknown: {action}"}), flush=True)
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
