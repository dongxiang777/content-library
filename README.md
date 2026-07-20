# 爆款内容库

短视频自动化内容生产系统的文案池。从**抖音**和**视频号**采集点赞高的同类爆款视频，FunASR 转成文字文案，DeepSeek 自动分类后写入飞书电子表格，作为视频号矩阵账号的内容弹药库。

## 两份核心文档

- **[部署文档](docs/部署文档.md)** — 如何在新电脑（macOS / Windows）一键部署，以及多机迭代同步。
- **[业务逻辑](docs/业务逻辑.md)** — 项目如何运转：两条采集链路、完整流程、做了哪些优化、数据字段。

## 快速开始

```bash
# 1. 克隆
git clone https://github.com/dongxiang777/content-library.git "content library"
cd "content library"

# 2. 一键部署（装依赖 + 编译工具 + 下模型）
bash scripts/setup/setup_mac.sh          # macOS
# 或 Windows：
# powershell -ExecutionPolicy Bypass -File scripts\setup\setup_windows.ps1

# 3. 填 .env 凭证（飞书 + DeepSeek），把飞书机器人加为表格协作者
```

## 三条业务线 / 三个子表

| 业务线 | 方向 | 飞书子表 |
|--------|------|----------|
| 传承 | 遗嘱 / 意定监护 / 继承 | 传承 IP |
| 情感 | 老年情感 / 养老 / 家庭矛盾 | 情感 IP |
| 理赔 | 车险 / 农险 / 保险理赔 | 理赔 |

- **飞书表格**: https://mcn0a4ritrc4.feishu.cn/sheets/RwZFsg8klhpzWVtHrpUcnguAn4g
- **数据字段**: 19 列（A–S），详见[业务逻辑文档](docs/业务逻辑.md)。

## 技术栈

- **采集**: MediaCrawler（抖音）+ wx_video_download MITM 代理（视频号，源码已并入 `tools/`）
- **转写**: FunASR paraformer-zh + cam++ 说话人分离（本地推理，零 API 成本）
- **分类**: DeepSeek API（8 维度自动分类）
- **存储**: 飞书电子表格（HTTP API 直连，按业务方向路由子表）
- **编排**: Python 并行流水线（下载 / 转写 / 分类 / 写入重叠执行）

## 常用命令

```bash
# 抖音全流程
cd scripts && python3 pipeline.py full --type search --keywords "遗嘱怎么写" --count 20

# 视频号矩阵采集（点赞≥2000 → 情感表）
python3 scripts/channels/run_baidajie.py
```
