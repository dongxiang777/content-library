#!/bin/bash
# 白大姐第二批 + Homan校长 自动接续脚本
# 等待 PID 11460 (第一批) 结束后自动运行

cd "/Users/shaoxinjiang/CodexWorkspace/projects/content library"

# 等第一批结束
while kill -0 11460 2>/dev/null; do
    sleep 30
done

echo "=== 第一批结束，启动白大姐第二批 (9个断连账号) ===" >> /tmp/chain.log
python3 -u scripts/channels/run_baidajie.py >> /tmp/baidajie_run2.log 2>&1

echo "=== 白大姐第二批结束，启动 Homan校长 ===" >> /tmp/chain.log
python3 -u scripts/channels/run_homan.py >> /tmp/homan_run.log 2>&1

echo "=== 全部完成 ===" >> /tmp/chain.log
