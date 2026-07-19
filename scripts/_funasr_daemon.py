#!/usr/bin/env python3
"""
FunASR 长驻 daemon — 模型只加载一次，通过 stdin/stdout JSON 接收转写请求。
由 transcribe.py 通过 subprocess.Popen 启动，避免每次转写都重新加载 2GB 模型。

协议 (每行一个 JSON):
  stdin:  {"action": "transcribe", "audio_path": "/path/to.wav"}
  stdout: {"ok": true, "text": "转写文本..."}
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


def _transcribe(audio_path: str) -> str:
    result = model.generate(input=audio_path, batch_size_s=300)
    if not result or not isinstance(result, list) or len(result) == 0:
        return ""
    text = result[0].get("text", "") if isinstance(result[0], dict) else ""
    return _clean_text(text)


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
                text = _transcribe(audio_path)
                print(json.dumps({"ok": True, "text": text}, ensure_ascii=False), flush=True)
            else:
                print(json.dumps({"ok": False, "error": f"unknown: {action}"}), flush=True)
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
