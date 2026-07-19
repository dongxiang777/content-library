"""
FunASR 语音转文字模块 — 长驻进程版
模型只加载一次（首次约 10-15 秒），之后每次转写仅需几秒。
通过 _funasr_daemon.py 长驻进程通信，避免每次 subprocess 都重新加载 2GB 模型。
"""

import os
import subprocess
import sys
import time
import json
import atexit
from pathlib import Path

from config import VENV_PYTHON


# ===== FunASR 长驻进程管理 =====

_daemon = None  # subprocess.Popen 实例


def _get_daemon():
    """获取或启动 FunASR 长驻进程。模型只加载一次。"""
    global _daemon
    if _daemon is not None and _daemon.poll() is None:
        return _daemon

    daemon_script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "_funasr_daemon.py")

    env = os.environ.copy()
    for var in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                "ALL_PROXY", "all_proxy"]:
        env.pop(var, None)

    print("[FunASR] 启动长驻进程，加载模型中...")
    t0 = time.time()

    _daemon = subprocess.Popen(
        [VENV_PYTHON, "-u", daemon_script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        bufsize=0,
    )

    # 等待就绪信号（模型加载约 10-15 秒）
    ready = False
    deadline = time.time() + 120
    while time.time() < deadline:
        if _daemon.poll() is not None:
            stderr = _daemon.stderr.read().decode()
            raise RuntimeError(
                f"FunASR daemon 启动失败 (exit {_daemon.returncode}):\n{stderr[:500]}"
            )
        raw = _daemon.stdout.readline()
        if not raw:
            time.sleep(0.5)
            continue
        resp = json.loads(raw.decode().strip())
        if resp.get("status") == "ready":
            ready = True
            break

    if not ready:
        _daemon.kill()
        raise RuntimeError("FunASR daemon 启动超时 (120s)")

    elapsed = time.time() - t0
    print(f"[FunASR] 模型就绪 ({elapsed:.1f}s)")

    atexit.register(stop_daemon)
    return _daemon


def _send_cmd(cmd: dict, timeout: int = 300) -> dict:
    """向 daemon 发送命令并读取响应。用 select 强制超时，防止无限阻塞。"""
    import select
    daemon = _get_daemon()
    daemon.stdin.write((json.dumps(cmd, ensure_ascii=False) + "\n").encode("utf-8"))
    daemon.stdin.flush()

    # 用 select 实现真正的超时（readline 本身不支持超时）
    ready, _, _ = select.select([daemon.stdout], [], [], timeout)
    if not ready:
        # 超时 — 杀掉 daemon，下次调用会重新启动
        print(f"[FunASR] daemon 响应超时 ({timeout}s)，重启...")
        try:
            daemon.kill()
        except Exception:
            pass
        global _daemon
        _daemon = None
        raise RuntimeError(f"FunASR daemon 超时 ({timeout}s)")

    raw = daemon.stdout.readline()
    if not raw:
        if daemon.poll() is not None:
            stderr = daemon.stderr.read().decode()
            _daemon = None
            raise RuntimeError(
                f"FunASR daemon 已退出 (exit {daemon.returncode}):\n{stderr[:500]}"
            )
        raise RuntimeError("FunASR daemon 无响应")

    return json.loads(raw.decode().strip())


def transcribe(audio_path: str) -> str:
    """转写音频文件，返回文本。模型已常驻内存，通常几秒完成。"""
    print(f"[FunASR] 转写: {audio_path}")
    t0 = time.time()

    resp = _send_cmd({"action": "transcribe", "audio_path": audio_path})

    elapsed = time.time() - t0
    if not resp.get("ok"):
        raise RuntimeError(f"FunASR 转写失败: {resp.get('error', 'unknown')}")

    text = resp.get("text", "")
    print(f"[FunASR] 转写完成 ({elapsed:.1f}s, {len(text)}字)")
    return text


def stop_daemon():
    """关闭 FunASR 长驻进程。"""
    global _daemon
    if _daemon is None:
        return
    try:
        _daemon.stdin.write(b'{"action":"quit"}\n')
        _daemon.stdin.flush()
        _daemon.wait(timeout=5)
    except Exception:
        try:
            _daemon.kill()
        except Exception:
            pass
    _daemon = None


# ===== 视频下载 & 音频提取 =====


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
    return True


def process_video(url: str, work_dir: str = "/tmp") -> dict:
    """
    完整的视频→文案流程: 下载 → 提取音频 → 转写。
    返回 {"transcript": str, "video_path": str, "audio_path": str, "success": bool}
    处理完成后自动清理临时文件。
    """
    import hashlib
    video_id = hashlib.md5(url.encode()).hexdigest()  # 用完整 hash 防碰撞
    video_path = f"{work_dir}/dy_{video_id}.mp4"
    audio_path = f"{work_dir}/dy_{video_id}.wav"

    result = {"transcript": "", "video_path": video_path,
              "audio_path": audio_path, "success": False}

    try:
        if not download_video(url, video_path):
            return result

        if not extract_audio(video_path, audio_path):
            return result

        # 验证音频文件确实存在（无音轨视频 ffmpeg 可能 exit 0 但无输出）
        if not Path(audio_path).exists():
            print("[FunASR] 音频文件不存在，可能视频无音轨")
            return result

        try:
            result["transcript"] = transcribe(audio_path)
            result["success"] = True
        except Exception as e:
            print(f"[FunASR] 转写异常: {e}")
    finally:
        # 清理临时文件
        for f in [video_path, audio_path]:
            try:
                if Path(f).exists():
                    os.remove(f)
            except OSError:
                pass

    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python transcribe.py <video_url_or_audio_path>")
        sys.exit(1)

    target = sys.argv[1]
    if target.endswith(('.wav', '.mp3', '.m4a', '.flac')):
        text = transcribe(target)
        print(f"\n--- 转写结果 ---\n{text}")
    else:
        r = process_video(target)
        print(f"\n--- 转写结果 ---\n{r['transcript']}")
    stop_daemon()
