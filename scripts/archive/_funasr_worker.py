#!/usr/bin/env python3
"""FunASR 转写 worker — 在 MediaCrawler venv 中运行。"""
import sys
import os
import io

# 关闭代理
for k in list(os.environ):
    if "proxy" in k.lower():
        del os.environ[k]

# 抑制模型加载时的版本输出
_real_stdout = sys.stdout
sys.stdout = io.StringIO()
from funasr import AutoModel
model = AutoModel(
    model="paraformer-zh",
    vad_model="fsmn-vad",
    punc_model="ct-punc",
    disable_update=True,
)
sys.stdout = _real_stdout

audio_path = sys.argv[1]
result = model.generate(input=audio_path, batch_size_s=300)
text = result[0]["text"] if result else ""
# 清理可能残留的版本号前缀
for prefix in ["funasr version:", "FunASR version:"]:
    if text.startswith(prefix):
        text = text[text.index("\n") + 1:] if "\n" in text else text.split(". ", 1)[-1]
        break
print(text, end="")
