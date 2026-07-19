"""
FunASR 语音转文字模块
使用 paraformer-zh 模型进行中文语音识别。
模型首次加载会从 ModelScope 下载 (~2.1GB)，之后缓存在本地。

注意: 下载模型时必须关闭代理 (unset http_proxy https_proxy 等)，否则 SSL 报错。
"""

import os
import subprocess
import sys
import time
from pathlib import Path

# 确保能导入同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import VENV_PYTHON


# 模型加载是重量级操作，用全局变量做单例
_model = None


def _unset_proxy():
    """临时清除代理环境变量（ModelScope 下载需要直连）。"""
    proxy_vars = ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                  "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"]
    saved = {}
    for var in proxy_vars:
        if var in os.environ:
            saved[var] = os.environ.pop(var)
    return saved


def _restore_proxy(saved: dict):
    """恢复之前的代理设置。"""
    os.environ.update(saved)


def get_model():
    """获取 FunASR 模型单例。首次调用会下载/加载模型。"""
    global _model
    if _model is not None:
        return _model

    saved_proxy = _unset_proxy()
    try:
        from funasr import AutoModel
        print("[FunASR] 加载模型 paraformer-zh + fsmn-vad + ct-punc ...")
        t0 = time.time()
        _model = AutoModel(
            model="paraformer-zh",
            vad_model="fsmn-vad",
            punc_model="ct-punc",
            disable_update=True,
        )
        print(f"[FunASR] 模型加载完成 ({time.time() - t0:.1f}s)")
    finally:
        _restore_proxy(saved_proxy)
    return _model


def download_video(url: str, output_path: str, timeout: int = 120) -> bool:
    """从抖音下载视频文件。"""
    print(f"[下载] {url[:80]}...")
    result = subprocess.run(
        ["curl", "-sL", "-o", output_path,
         "--connect-timeout", "10", "--max-time", str(timeout),
         "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
         "-H", "Referer: https://www.douyin.com/",
         url],
        capture_output=True, text=True
    )
    if result.returncode != 0 or not Path(output_path).exists():
        print(f"[下载] 失败: {result.stderr[:200]}")
        return False

    size_mb = Path(output_path).stat().st_size / (1024 * 1024)
    if size_mb < 0.1:
        print(f"[下载] 文件太小 ({size_mb:.2f}MB)，可能无效")
        return False

    print(f"[下载] 完成 ({size_mb:.1f}MB)")
    return True


def extract_audio(video_path: str, audio_path: str) -> bool:
    """用 ffmpeg 从视频提取 16kHz 单声道 WAV。"""
    result = subprocess.run(
        ["ffmpeg", "-i", video_path, "-vn",
         "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
         audio_path, "-y", "-loglevel", "error"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"[ffmpeg] 失败: {result.stderr[:200]}")
        return False
    print(f"[ffmpeg] 音频提取完成: {audio_path}")
    return True


def transcribe(audio_path: str) -> str:
    """对音频文件执行语音识别，返回带标点的文本。
    通过 subprocess 调用 venv Python 中的 _funasr_worker.py，避免系统 Python 缺少 funasr。
    """
    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_funasr_worker.py")
    print(f"[FunASR] 转写: {audio_path}")
    t0 = time.time()

    # 用 venv Python 跑 worker
    env = os.environ.copy()
    for var in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"]:
        env.pop(var, None)

    result = subprocess.run(
        [VENV_PYTHON, worker, audio_path],
        capture_output=True, text=True, env=env,
        timeout=300,
    )

    if result.returncode != 0:
        raise RuntimeError(f"FunASR worker 失败: {result.stderr[:500]}")

    elapsed = time.time() - t0
    text = result.stdout.strip()
    print(f"[FunASR] 转写完成 ({elapsed:.1f}s, {len(text)}字)")
    return text


def process_video(url: str, work_dir: str = "/tmp") -> dict:
    """
    完整的视频→文案流程: 下载 → 提取音频 → 转写。
    返回 {"transcript": str, "video_path": str, "audio_path": str, "success": bool}
    """
    import hashlib
    video_id = hashlib.md5(url.encode()).hexdigest()[:12]
    video_path = f"{work_dir}/dy_{video_id}.mp4"
    audio_path = f"{work_dir}/dy_{video_id}.wav"

    result = {"transcript": "", "video_path": video_path,
              "audio_path": audio_path, "success": False}

    # 1. 下载视频
    if not download_video(url, video_path):
        return result

    # 2. 提取音频
    if not extract_audio(video_path, audio_path):
        return result

    # 3. 语音识别
    try:
        result["transcript"] = transcribe(audio_path)
        result["success"] = True
    except Exception as e:
        print(f"[FunASR] 转写异常: {e}")

    return result


if __name__ == "__main__":
    # 单独测试
    if len(sys.argv) < 2:
        print("用法: python transcribe.py <video_url_or_audio_path>")
        sys.exit(1)

    target = sys.argv[1]
    if target.endswith(('.wav', '.mp3', '.m4a', '.flac')):
        # 直接转写音频文件
        text = transcribe(target)
        print(f"\n--- 转写结果 ---\n{text}")
    else:
        # 当作视频 URL 处理
        r = process_video(target)
        print(f"\n--- 转写结果 ---\n{r['transcript']}")
