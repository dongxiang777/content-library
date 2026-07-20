@echo off
rem 从源码编译视频号下载工具（Windows）。
rem 用法：在 PowerShell 或 cmd 中运行 scripts\setup\build_tool.bat
setlocal

cd /d "%~dp0..\..\tools\wx_channels_download"

where go >nul 2>nul
if errorlevel 1 (
    echo [X] 未检测到 Go。
    echo     Windows 安装：https://go.dev/dl/ 下载 .msi 安装包，安装后重开终端。
    exit /b 1
)

go version
echo 编译中（首次需下载依赖，约 1-3 分钟）...
rem embed_inject 标签必需：把注入脚本打包进二进制，否则视频号功能不可用
go build -tags embed_inject -o wx_video_download.exe .
if errorlevel 1 (
    echo [X] 编译失败，请检查上方报错。
    exit /b 1
)

echo [OK] 编译完成：%cd%\wx_video_download.exe
endlocal
