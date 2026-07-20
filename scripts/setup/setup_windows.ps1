# ============================================================
# 内容库一键部署脚本（Windows）
# 用法：在项目根目录，右键"使用 PowerShell 运行"，或执行：
#   powershell -ExecutionPolicy Bypass -File scripts\setup\setup_windows.ps1
# 可重复运行（已完成的步骤会自动跳过）。
# 注意：首次安装证书时可能弹出管理员权限确认（UAC），请点"是"。
# ============================================================
$ErrorActionPreference = "Stop"

$ROOT = (Resolve-Path "$PSScriptRoot\..\..").Path
$PIP_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
$MC_DIR = Join-Path $ROOT "tools\MediaCrawler"
$VENV_PY = Join-Path $MC_DIR ".venv\Scripts\python.exe"

Write-Host "=========================================="
Write-Host " 内容库一键部署 (Windows)"
Write-Host " 项目目录：$ROOT"
Write-Host "=========================================="

# ---------- 1. Go ----------
Write-Host "`n[1/7] 检查 Go 编译环境..."
if (Get-Command go -ErrorAction SilentlyContinue) {
    Write-Host "  [OK] 已安装：$(go version)"
} else {
    Write-Host "  未检测到 Go，尝试用 winget 安装..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id GoLang.Go -e
        Write-Host "  安装后请关闭并重新打开本脚本（让 PATH 生效）。"
        exit 0
    } else {
        Write-Host "  [X] 请从 https://go.dev/dl/ 下载 .msi 安装包，安装后重开本脚本。"
        exit 1
    }
}

# ---------- 2. 编译视频号下载工具 ----------
Write-Host "`n[2/7] 编译视频号下载工具..."
& "$ROOT\scripts\setup\build_tool.bat"

# ---------- 3. 克隆 MediaCrawler（抖音采集）----------
Write-Host "`n[3/7] 检查抖音采集工具 MediaCrawler..."
if (Test-Path "$MC_DIR\.git") {
    Write-Host "  [OK] 已存在，跳过克隆"
} else {
    Write-Host "  克隆 MediaCrawler..."
    git clone https://github.com/NanmiCoder/MediaCrawler.git $MC_DIR
}

# ---------- 4. 创建虚拟环境 ----------
Write-Host "`n[4/7] 配置 Python 虚拟环境..."
if (Test-Path $VENV_PY) {
    Write-Host "  [OK] 虚拟环境已存在：$VENV_PY"
} else {
    Write-Host "  创建虚拟环境（需要已安装 Python 3.10+）..."
    python -m venv "$MC_DIR\.venv"
}
& $VENV_PY -m pip install --upgrade pip -i $PIP_MIRROR

# ---------- 5. 安装依赖 ----------
Write-Host "`n[5/7] 安装 Python 依赖（MediaCrawler + FunASR，首次约 2GB 较慢）..."
if (Test-Path "$MC_DIR\requirements.txt") {
    & $VENV_PY -m pip install -r "$MC_DIR\requirements.txt" -i $PIP_MIRROR
}
& $VENV_PY -m pip install -r "$ROOT\scripts\setup\requirements.txt" -i $PIP_MIRROR

Write-Host "  安装 Playwright Chromium（抖音采集用）..."
& $VENV_PY -m playwright install chromium

# ---------- 6. 预下载 FunASR 模型 ----------
Write-Host "`n[6/7] 预下载语音识别模型..."
& $VENV_PY "$ROOT\scripts\setup\warmup_models.py"

# ---------- 7. .env 配置 ----------
Write-Host "`n[7/7] 检查 .env 配置..."
if (Test-Path "$ROOT\.env") {
    Write-Host "  [OK] .env 已存在，跳过"
} else {
    Copy-Item "$ROOT\.env.example" "$ROOT\.env"
    Write-Host "  已创建 .env，请稍后填入凭证（见下方说明）"
}

# ---------- 完成 ----------
Write-Host "`n=========================================="
Write-Host " [OK] 部署完成！"
Write-Host "=========================================="
Write-Host @"

接下来手动完成 3 件事：
  1. 编辑 $ROOT\.env，填入：
     - FEISHU_APP_ID / FEISHU_APP_SECRET（飞书开放平台应用凭证）
     - LLM_API_KEY（DeepSeek API key）
  2. 把飞书应用机器人加为表格协作者（编辑权限）。
  3. 采集视频号前，以管理员身份启动下载工具并在微信电脑版打开视频号页面：
     cd tools\wx_channels_download ; .\wx_video_download.exe
     （首次运行会自动安装证书，需点 UAC 确认）

详细使用说明见 docs\部署文档.md 和 docs\业务逻辑.md
"@
