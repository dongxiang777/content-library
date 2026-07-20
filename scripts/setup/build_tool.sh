#!/usr/bin/env bash
# 从源码编译视频号下载工具（macOS / Linux）。
# 用法：bash scripts/setup/build_tool.sh
set -e

TOOL_DIR="$(cd "$(dirname "$0")/../../tools/wx_channels_download" && pwd)"
cd "$TOOL_DIR"

if ! command -v go >/dev/null 2>&1; then
  echo "✗ 未检测到 Go。"
  echo "  macOS 安装：brew install go"
  echo "  或官网下载：https://go.dev/dl/"
  exit 1
fi

echo "Go 版本：$(go version)"
echo "编译中（首次需下载依赖，约 1-3 分钟）..."
# embed_inject 标签必需：把注入脚本打包进二进制，否则视频号功能不可用
go build -tags embed_inject -o wx_video_download .

echo "✓ 编译完成：$TOOL_DIR/wx_video_download"
