"""
FunASR 语音转文字模块 — 长驻进程版
模型只加载一次（首次约 10-15 秒），之后每次转写仅需几秒。
通过 _funasr_daemon.py 长驻进程通信，避免每次 subprocess 都重新加载 2GB 模型。

阶段拆分（供 pipeline 流水线并行）:
  prepare_audio(url)  — 下载 + ffmpeg 抽音频（I/O，可与转写并行）
  transcribe(path)    — FunASR 转写（daemon，串行加锁）
  process_video(url)  — 兼容旧接口：prepare + transcribe + 清理
"""

import os
import subprocess
import sys
import time
import json
import atexit
import hashlib
import threading
from pathlib import Path

from config import VENV_PYTHON


# ===== FunASR 长驻进程管理 =====

_daemon = None  # subprocess.Popen 实例
_daemon_lock = threading.Lock()  # 保护 stdin/stdout 通信（协议是串行的）
_stderr_thread = None
_atexit_registered = False


def _drain_stderr(proc):
    """后台排空 daemon stderr，防止管道缓冲区写满导致 daemon 死锁。"""
    try:
        for line in iter(proc.stderr.readline, b""):
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                # 仅打印有信息量的行，避免刷屏
                if any(k in text.lower() for k in ("error", "warn", "exception", "fail")):
                    print(f"[FunASR:stderr] {text[:300]}")
    except Exception:
        pass


def ensure_daemon():
    """预热：确保 FunASR daemon 已启动且模型就绪。可在流水线开始时调用。"""
    _get_daemon()


def _get_daemon():
    """获取或启动 FunASR 长驻进程。模型只加载一次。"""
    global _daemon, _stderr_thread, _atexit_registered
    with _daemon_lock:
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

        # 排空 stderr，避免管道堵塞
        _stderr_thread = threading.Thread(
            target=_drain_stderr, args=(_daemon,), daemon=True, name="funasr-stderr"
        )
        _stderr_thread.start()

        # 等待就绪信号（模型加载约 10-15 秒）
        ready = False
        deadline = time.time() + 120
        while time.time() < deadline:
            if _daemon.poll() is not None:
                stderr = b""
                try:
                    stderr = _daemon.stderr.read() or b""
                except Exception:
                    pass
                raise RuntimeError(
                    f"FunASR daemon 启动失败 (exit {_daemon.returncode}):\n"
                    f"{stderr.decode('utf-8', errors='replace')[:500]}"
                )
            # 启动阶段的 readline 也要有超时，用 select
            import select
            readable, _, _ = select.select([_daemon.stdout], [], [], 1.0)
            if not readable:
                continue
            raw = _daemon.stdout.readline()
            if not raw:
                time.sleep(0.1)
                continue
            resp = json.loads(raw.decode().strip())
            if resp.get("status") == "ready":
                ready = True
                break

        if not ready:
            try:
                _daemon.kill()
            except Exception:
                pass
            _daemon = None
            raise RuntimeError("FunASR daemon 启动超时 (120s)")

        elapsed = time.time() - t0
        print(f"[FunASR] 模型就绪 ({elapsed:.1f}s)")

        # 轻量 warmup：确认 ping 通路正常（模型已加载，几乎瞬时）
        try:
            _send_cmd_unlocked({"action": "ping"}, timeout=30)
            print("[FunASR] warmup ping ok")
        except Exception as e:
            print(f"[FunASR] warmup ping 跳过: {e}")

        if not _atexit_registered:
            atexit.register(stop_daemon)
            _atexit_registered = True
        return _daemon


def _send_cmd_unlocked(cmd: dict, timeout: int = 300) -> dict:
    """向 daemon 发送命令（调用方须已持有 _daemon_lock，或在 _get_daemon 内部）。"""
    global _daemon
    daemon = _daemon
    if daemon is None or daemon.poll() is not None:
        raise RuntimeError("FunASR daemon 未运行")

    daemon.stdin.write((json.dumps(cmd, ensure_ascii=False) + "\n").encode("utf-8"))
    daemon.stdin.flush()

    import select
    ready, _, _ = select.select([daemon.stdout], [], [], timeout)
    if not ready:
        print(f"[FunASR] daemon 响应超时 ({timeout}s)，重启...")
        try:
            daemon.kill()
        except Exception:
            pass
        _daemon = None
        raise RuntimeError(f"FunASR daemon 超时 ({timeout}s)")

    raw = daemon.stdout.readline()
    if not raw:
        if daemon.poll() is not None:
            _daemon = None
            raise RuntimeError(
                f"FunASR daemon 已退出 (exit {daemon.returncode})"
            )
        raise RuntimeError("FunASR daemon 无响应")

    return json.loads(raw.decode().strip())


def _send_cmd(cmd: dict, timeout: int = 300) -> dict:
    """向 daemon 发送命令并读取响应。线程安全（串行化通信）。"""
    # 确保 daemon 存活（_get_daemon 内部也持锁，注意顺序：先 get 再 send 都在同一把锁里）
    with _daemon_lock:
        if _daemon is None or _daemon.poll() is not None:
            # 释放锁后启动，避免 _get_daemon 重入死锁
            pass
        else:
            return _send_cmd_unlocked(cmd, timeout)

    # daemon 未运行：先启动
    _get_daemon()
    with _daemon_lock:
        return _send_cmd_unlocked(cmd, timeout)


def transcribe(audio_path: str) -> str:
    """转写音频文件，返回文本。模型已常驻内存，通常几秒完成。线程安全。"""
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
    with _daemon_lock:
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
    t0 = time.time()
    result = subprocess.run(
        ["curl", "-sL", "-o", output_path,
         "--connect-timeout", "10", "--max-time", str(timeout),
         "--retry", "2", "--retry-delay", "1",
         "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
         "-H", "Referer: https://www.douyin.com/",
         url],
        capture_output=True, text=True
    )
    if result.returncode != 0 or not Path(output_path).exists():
        print(f"[下载] 失败: {(result.stderr or '')[:200]}")
        return False

    size_mb = Path(output_path).stat().st_size / (1024 * 1024)
    if size_mb < 0.1:
        print(f"[下载] 文件太小 ({size_mb:.2f}MB)，可能无效")
        return False

    print(f"[下载] 完成 ({size_mb:.1f}MB, {time.time()-t0:.1f}s)")
    return True


def extract_audio(video_path: str, audio_path: str) -> bool:
    """用 ffmpeg 从视频提取 16kHz 单声道 WAV。"""
    t0 = time.time()
    result = subprocess.run(
        ["ffmpeg", "-i", video_path, "-vn",
         "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
         "-threads", "2",
         audio_path, "-y", "-loglevel", "error"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"[ffmpeg] 失败: {(result.stderr or '')[:200]}")
        return False
    print(f"[ffmpeg] 抽音频完成 ({time.time()-t0:.1f}s)")
    return True


def cleanup_paths(paths):
    """删除临时文件，忽略错误。可公开调用。"""
    for f in paths:
        if not f:
            continue
        try:
            if Path(f).exists():
                os.remove(f)
        except OSError:
            pass


# 兼容内部旧名
_cleanup_paths = cleanup_paths


def prepare_audio(url: str, work_dir: str = "/tmp") -> dict:
    """
    下载视频并提取音频（I/O 阶段，可与 FunASR 转写并行）。
    返回:
      {
        "ok": bool,
        "audio_path": str|None,
        "cleanup": [paths...],   # 调用方转写后负责清理
        "error": str|None,
      }
    视频文件在抽完音频后立即删除，只保留 wav，减少磁盘占用。
    """
    video_id = hashlib.md5(url.encode()).hexdigest()
    # 用线程 id 防并发准备同一 URL 时路径冲突
    tid = threading.get_ident() % 100000
    video_path = f"{work_dir}/dy_{video_id}_{tid}.mp4"
    audio_path = f"{work_dir}/dy_{video_id}_{tid}.wav"
    cleanup = [video_path, audio_path]

    if not url:
        return {"ok": False, "audio_path": None, "cleanup": [], "error": "empty url"}

    try:
        if not download_video(url, video_path):
            _cleanup_paths(cleanup)
            return {"ok": False, "audio_path": None, "cleanup": [], "error": "download failed"}

        if not extract_audio(video_path, audio_path):
            _cleanup_paths(cleanup)
            return {"ok": False, "audio_path": None, "cleanup": [], "error": "ffmpeg failed"}

        # 抽完即删视频，只留音频给转写
        _cleanup_paths([video_path])

        if not Path(audio_path).exists():
            print("[准备] 音频文件不存在，可能视频无音轨")
            _cleanup_paths(cleanup)
            return {"ok": False, "audio_path": None, "cleanup": [], "error": "no audio track"}

        return {
            "ok": True,
            "audio_path": audio_path,
            "cleanup": [audio_path],
            "error": None,
        }
    except Exception as e:
        _cleanup_paths(cleanup)
        return {"ok": False, "audio_path": None, "cleanup": [], "error": str(e)}


def process_video(url: str, work_dir: str = "/tmp") -> dict:
    """
    完整的视频→文案流程: 下载 → 提取音频 → 转写。
    返回 {"transcript": str, "video_path": str, "audio_path": str, "success": bool}
    处理完成后自动清理临时文件。
    """
    result = {"transcript": "", "video_path": "",
              "audio_path": "", "success": False}

    prep = prepare_audio(url, work_dir)
    result["audio_path"] = prep.get("audio_path") or ""
    cleanup = prep.get("cleanup") or []

    try:
        if not prep.get("ok"):
            return result

        try:
            result["transcript"] = transcribe(prep["audio_path"])
            result["success"] = True
        except Exception as e:
            print(f"[FunASR] 转写异常: {e}")
    finally:
        _cleanup_paths(cleanup)

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
