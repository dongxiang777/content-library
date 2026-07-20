#!/usr/bin/env python3
r"""
预下载 FunASR 语音识别模型（约 2GB），避免首次转写时长时间等待。
用法：用 MediaCrawler 的 venv 运行
  macOS/Linux: tools/MediaCrawler/.venv/bin/python scripts/setup/warmup_models.py
  Windows:     tools\MediaCrawler\.venv\Scripts\python.exe scripts\setup\warmup_models.py

模型从 ModelScope 下载，需要直连（脚本会自动关闭代理）。
"""
import os
import sys

# ModelScope 需直连，关闭代理
for k in list(os.environ):
    if "proxy" in k.lower():
        del os.environ[k]

MODELS = dict(
    model="paraformer-zh",   # 中文语音识别主模型
    vad_model="fsmn-vad",    # 语音活动检测（断句）
    punc_model="ct-punc",    # 标点恢复
    spk_model="cam++",       # 说话人分离（多人连麦时分段）
)

print("开始下载/校验 FunASR 模型（首次约 2GB，请耐心等待）...")
print(f"  模型组合：{', '.join(MODELS.values())}")
try:
    from funasr import AutoModel
    model = AutoModel(**MODELS, disable_update=True)
    print("✓ 模型已就绪，后续转写无需再下载。")
except ImportError:
    print("✗ 未安装 funasr。请先运行安装脚本（setup_mac.sh / setup_windows.ps1）。")
    sys.exit(1)
except Exception as e:
    print(f"✗ 模型下载失败：{e}")
    print("  请检查网络（需能访问 modelscope.cn），然后重试。")
    sys.exit(1)
