# 爆款内容库

短视频自动化内容生产系统的文案池。从抖音搜索/创作者主页采集视频数据，AI 自动分类后写入飞书电子表格，支持视频转文案。

## 飞书表格

- **URL**: https://mcn0a4ritrc4.feishu.cn/sheets/RwZFsg8klhpzWVtHrpUcnguAn4g
- **Token**: `RwZFsg8klhpzWVtHrpUcnguAn4g`
- **Sheet ID**: `3a6f67`

## 字段结构 (18列 A-R)

| 列 | 字段 | 说明 |
|----|------|------|
| A | 业务方向 | 传承 / 情感老年IP / 保险理赔 |
| B | 细分领域 | 遗嘱 / 意定监护 / 继承纠纷 / 车险 / 农险 等 |
| C | 内容形式 | 口播 / 案例讲解 / 情景剧 / 图文 |
| D | 选题方向 | 自书遗嘱教程 / 独生子女继承保护 等 |
| E | 内容切入角度 | 教程引导 / 认知恐惧 / 实用安心 等 |
| F | 目标人群 | 50-65岁中老年人 / 独生子女父母 / 有车一族 等 |
| G | 情绪钩子 | 安全感 / 恐惧紧迫 / 实用安心 / 温暖治愈 等 |
| H | 特征标签 | 逗号分隔的细粒度关键词 |
| I | 文案全文 | 完整口播文案 (FunASR 转录) |
| J | 标题 | 视频标题 |
| K | 平台 | 来源平台 |
| L | 原始链接 | 原始视频 URL |
| M | 创作者 | 原始视频作者 |
| N | 点赞 | 点赞数 |
| O | 收藏 | 收藏数 |
| P | 转发 | 转发数 |
| Q | 评论 | 评论数 |
| R | 入库日期 | YYYY-MM-DD |

## 目录结构

```
content library/
├── README.md                    ← 本文件
├── .env                         ← 飞书凭证 + DeepSeek API key (不入 git)
├── .env.example                 ← 环境变量模板
├── scripts/
│   ├── config.py                ← 集中配置 (飞书/LLM/MediaCrawler路径)
│   ├── feishu_utils.py          ← 飞书表格读写 (HTTP 直连, 无 lark-cli)
│   ├── pipeline.py              ← 主 Pipeline CLI (crawl/import/process/full)
│   ├── ai_classify.py           ← DeepSeek API 内容分类 (8维度)
│   ├── transcribe.py            ← FunASR 视频转文案
│   ├── classify_content.py      ← 关键词分类 (旧版, 保留)
│   └── write_to_feishu.py       ← 飞书写入 (旧版, 保留)
├── data/
│   └── pipeline_state.json      ← Pipeline 状态 (不入 git)
├── source/
│   ├── 能爆文案.md
│   └── 近期爆款文案.md
└── tools/
    └── MediaCrawler/            ← 第三方工具 (不入 git, 需单独克隆)
```

## 环境配置

1. 克隆本项目
2. 克隆 MediaCrawler 到 `tools/` 目录:
   ```bash
   cd tools
   git clone https://github.com/NanmiCoder/MediaCrawler.git
   cd MediaCrawler && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   ```
3. 创建 `.env` 文件 (参考 `.env.example`):
   ```
   FEISHU_APP_ID=你的飞书应用ID
   FEISHU_APP_SECRET=你的飞书应用密钥
   LLM_BASE_URL=https://api.deepseek.com/v1
   LLM_API_KEY=你的DeepSeek API key
   LLM_MODEL=deepseek-chat
   ```
4. 将飞书应用机器人加为表格协作者 (编辑权限)

## 使用方式

### Pipeline CLI

```bash
cd scripts

# 搜索模式: 按关键词采集抖音视频
python3 pipeline.py crawl --type search --keywords "遗嘱怎么写,自书遗嘱" --count 20

# 主页模式: 采集指定创作者的全部视频
python3 pipeline.py crawl --type creator --creator-id "创作者主页URL或sec_uid"

# 导入飞书 + AI分类 + 转文案
python3 pipeline.py process

# 一键全流程: 采集 → 导入 → 分类 → 转文案
python3 pipeline.py full --type search --keywords "遗嘱怎么写" --count 20
```

### 在 QoderWork 中使用

通过 `douyin-content-pipeline` 技能，直接用自然语言驱动:

> "搜'遗嘱怎么写'，要点赞 1 万以上的，最近一周的，来 20 条"

## 技术栈

- **采集**: MediaCrawler (抖音搜索/创作者主页)
- **存储**: 飞书电子表格 (HTTP API 直连, bot token 认证)
- **转写**: FunASR paraformer-zh (本地推理)
- **分类**: DeepSeek API (8维度自动分类)
- **编排**: Python Pipeline CLI
