#!/usr/bin/env bash
# ============================================================
# 内容库一键部署脚本（macOS）
# 用法：bash scripts/setup/setup_mac.sh
# 可重复运行（已完成的步骤会自动跳过）。
# ============================================================
set -e

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PIP_MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"
MC_DIR="$ROOT/tools/MediaCrawler"
VENV_PY="$MC_DIR/.venv/bin/python"

echo "=========================================="
echo " 内容库一键部署 (macOS)"
echo " 项目目录：$ROOT"
echo "=========================================="

# ---------- 1. Go ----------
echo ""
echo "[1/7] 检查 Go 编译环境..."
if command -v go >/dev/null 2>&1; then
  echo "  ✓ 已安装：$(go version)"
else
  echo "  未检测到 Go，尝试用 Homebrew 安装..."
  if command -v brew >/dev/null 2>&1; then
    brew install go
  else
    echo "  ✗ 请先安装 Homebrew（https://brew.sh），或从 https://go.dev/dl/ 下载 Go。"
    exit 1
  fi
fi

# ---------- 2. 编译视频号下载工具 ----------
echo ""
echo "[2/7] 编译视频号下载工具..."
bash "$ROOT/scripts/setup/build_tool.sh"
echo "  检测系统代理并写入工具配置..."
python3 "$ROOT/scripts/detect_proxy.py"

# ---------- 3. 克隆 MediaCrawler（抖音采集）----------
echo ""
echo "[3/7] 检查抖音采集工具 MediaCrawler..."
if [ -d "$MC_DIR/.git" ]; then
  echo "  ✓ 已存在，跳过克隆"
else
  echo "  克隆 MediaCrawler（ NanmiCoder）..."
  git clone https://github.com/NanmiCoder/MediaCrawler.git "$MC_DIR"
fi

# ---------- 4. 创建虚拟环境 ----------
echo ""
echo "[4/7] 配置 Python 虚拟环境..."
if [ -x "$VENV_PY" ]; then
  echo "  ✓ 虚拟环境已存在：$VENV_PY"
else
  echo "  创建虚拟环境..."
  python3 -m venv "$MC_DIR/.venv"
fi
"$VENV_PY" -m pip install --upgrade pip -i "$PIP_MIRROR"

# ---------- 5. 安装依赖 ----------
echo ""
echo "[5/7] 安装 Python 依赖（MediaCrawler + FunASR，首次约 2GB 较慢）..."
if [ -f "$MC_DIR/requirements.txt" ]; then
  "$VENV_PY" -m pip install -r "$MC_DIR/requirements.txt" -i "$PIP_MIRROR"
fi
"$VENV_PY" -m pip install -r "$ROOT/scripts/setup/requirements.txt" -i "$PIP_MIRROR"

# Playwright 浏览器（抖音采集需要）
echo "  安装 Playwright Chromium（抖音采集用）..."
"$VENV_PY" -m playwright install chromium || echo "  ⚠ Playwright 安装失败，可稍后手动：$VENV_PY -m playwright install chromium"

# ---------- 6. 预下载 FunASR 模型 ----------
echo ""
echo "[6/7] 预下载语音识别模型..."
"$VENV_PY" "$ROOT/scripts/setup/warmup_models.py"

# ---------- 7. .env 配置 ----------
echo ""
echo "[7/7] 检查 .env 配置..."
if [ -f "$ROOT/.env" ]; then
  echo "  ✓ .env 已存在，跳过"
else
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo "  已创建 .env，请稍后填入凭证（见下方说明）"
fi

# ---------- 完成 ----------
echo ""
echo "=========================================="
echo " ✓ 部署完成！"
echo "=========================================="
echo ""
echo "接下来手动完成 3 件事："
echo "  1. 编辑 $ROOT/.env，填入："
echo "     - FEISHU_APP_ID / FEISHU_APP_SECRET（飞书开放平台应用凭证）"
echo "     - LLM_API_KEY（DeepSeek API key）"
echo "  2. 把飞书应用机器人加为表格协作者（编辑权限）。"
echo "  3. 采集视频号前，启动下载工具并在微信电脑版打开视频号页面："
echo "     cd tools/wx_channels_download && ./wx_video_download"
echo "     （首次运行会自动安装证书）"
echo ""
echo "详细使用说明见 docs/部署文档.md 和 docs/业务逻辑.md"
