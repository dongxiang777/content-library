#!/usr/bin/env python3
"""
内容分类脚本 - 将原始文案按14字段结构分类整理

用法:
    python3 classify_content.py --input source/新文案.md --output data/new-batch.json
    python3 classify_content.py --input source/新文案.md --append data/content-library.json

输入: Markdown文件，每条文案用数字编号分隔 (1. / 2. / 3. ...)
输出: JSON二维数组，每行14个字段，可直接用 write_to_feishu.py 写入飞书

分类规则基于关键词匹配，复杂情况需要人工复核。
"""

import json
import re
import argparse
import sys
from datetime import date
from pathlib import Path


# ===== 分类规则 =====

# 选题方向关键词
TOPIC_KEYWORDS = {
    "自书遗嘱手把手教程": ["怎么写", "手把手", "教你写", "照着写", "标准写法", "万能格式", "格式", "步骤", "操作"],
    "自书遗嘱效力科普": ["无效", "有效", "效力", "法律", "民法典", "1134", "法定要件", "符合条件"],
    "遗嘱必写关键条款": ["三句话", "八句话", "关键句", "一定要写", "必写", "不能少", "加上这句", "这句话"],
    "独生子女继承保护": ["独生子女", "独生女", "独生子", "七大姑", "八大姨", "转继承", "代位继承"],
    "遗嘱避坑与无效写法": ["错误", "避坑", "踩坑", "作废", "翻车", "不能", "千万不要", "常见错误"],
    "继承流程与法律科普": ["继承流程", "公证处", "法院", "打官司", "过户", "办理"],
    "遗嘱与公证对比": ["公证", "不用公证", "不需要公证", "公证费", "性价比"],
    "房产继承与税费": ["房产", "房子", "过户费", "税费", "契税", "个税", "省钱"],
    "年轻人与遗嘱": ["年轻人", "90后", "00后", "婚前", "结婚前"],
    "情感与家庭故事": ["故事", "真实案例", "新闻", "事件"],
}

# 内容切入角度
ANGLE_KEYWORDS = {
    "教程引导": ["怎么", "如何", "教你", "步骤", "操作", "照着写", "手把手"],
    "认知恐惧": ["后果", "严重", "危险", "千万别", "后悔", "害怕", "恐惧", "外人分走"],
    "实用安心": ["省钱", "方便", "简单", "零成本", "省心", "实用"],
    "正能量激励": ["智慧", "通透", "想明白", "幸福", "正能量"],
    "警示科普": ["注意", "警惕", "安全", "风险", "防范"],
    "共情引导": ["理解", "不容易", "辛苦", "委屈", "心疼"],
}

# 目标人群
AUDIENCE_KEYWORDS = {
    "50-65岁中老年人": ["老人", "父母", "爸妈", "老年", "晚年"],
    "独生子女父母": ["独生子女", "独生女", "独生子", "一个孩子"],
    "有车一族": ["车", "驾驶", "事故", "保险"],
    "农户种植户": ["种地", "农民", "农田", "种植", "农业"],
}

# 情绪钩子
EMOTION_KEYWORDS = {
    "安全感": ["安心", "踏实", "放心", "保障", "安全"],
    "恐惧紧迫": ["后果", "严重", "危险", "赶紧", "趁早", "别等"],
    "实用安心": ["省钱", "简单", "方便", "实用", "划算"],
    "温暖治愈": ["温暖", "疼爱", "守护", "幸福", "正能量"],
    "委屈共鸣": ["委屈", "心酸", "不容易", "苦", "难"],
}


def parse_scripts_from_markdown(filepath: str) -> list:
    """从Markdown文件解析文案列表。支持 '1.' / '2.' 编号分隔。"""
    with open(filepath, 'r') as f:
        text = f.read()
    
    # 按 "数字." 分隔
    parts = re.split(r'\n\d+[\.\、]\s*\n?', text)
    scripts = [p.strip() for p in parts if p.strip() and len(p.strip()) > 20]
    return scripts


def classify_keyword(text: str, rules: dict) -> str:
    """基于关键词匹配返回最佳分类。"""
    scores = {}
    for category, keywords in rules.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > 0:
            scores[category] = score
    
    if scores:
        return max(scores, key=scores.get)
    return ""


def extract_tags(text: str) -> str:
    """从文案中提取特征标签。"""
    tag_pool = [
        "自书遗嘱", "公证", "亲笔书写", "签名落款", "年月日",
        "夫妻共同财产", "独生子女", "法定继承", "代位继承", "转继承",
        "录像留证", "见证人", "遗嘱执行人", "民法典1134",
        "房产过户", "税费", "保险理赔", "事故处理",
        "婆媳矛盾", "家庭关系", "独居安全", "居家适老化",
        "退休生活", "老年心态", "农业保险", "自然灾害",
    ]
    matched = [tag for tag in tag_pool if tag in text]
    return ",".join(matched[:6]) if matched else ""


def classify_script(script: str, default_platform: str = "手动录入") -> list:
    """将一条文案分类为14字段数组。"""
    topic = classify_keyword(script, TOPIC_KEYWORDS)
    angle = classify_keyword(script, ANGLE_KEYWORDS)
    audience = classify_keyword(script, AUDIENCE_KEYWORDS)
    emotion = classify_keyword(script, EMOTION_KEYWORDS)
    tags = extract_tags(script)
    
    # 默认值
    if not audience:
        audience = "50-65岁中老年人"
    if not emotion:
        emotion = "安全感"
    
    # 根据选题推断业务方向和细分领域
    biz = "传承"
    sub = "遗嘱"
    
    return [
        biz, sub, "口播", topic or "待分类",
        angle or "待分类", audience, emotion,
        tags, default_platform, "", "", "",
        str(date.today()), script
    ]


def main():
    parser = argparse.ArgumentParser(description="内容分类脚本")
    parser.add_argument("--input", required=True, help="输入 Markdown 文件路径")
    parser.add_argument("--output", help="输出 JSON 文件路径")
    parser.add_argument("--append", metavar="EXISTING", help="追加到已有 JSON 文件")
    parser.add_argument("--platform", default="手动录入", help="平台来源")
    parser.add_argument("--review", action="store_true", help="输出后标记需要人工复核的条目")
    
    args = parser.parse_args()
    
    scripts = parse_scripts_from_markdown(args.input)
    print(f"解析到 {len(scripts)} 条文案")
    
    classified = [classify_script(s, args.platform) for s in scripts]
    
    # 统计
    topics = {}
    uncategorized = 0
    for row in classified:
        t = row[3]
        topics[t] = topics.get(t, 0) + 1
        if t == "待分类":
            uncategorized += 1
    
    print("\n分类统计:")
    for t, c in sorted(topics.items(), key=lambda x: -x[1]):
        print(f"  {t}: {c}")
    
    if uncategorized > 0 and args.review:
        print(f"\n⚠️ {uncategorized} 条未自动分类，需要人工复核")
    
    if args.append:
        with open(args.append) as f:
            existing = json.load(f)
        classified = existing + classified
        print(f"\n追加后总计: {len(classified)} 条")
        output_path = args.append
    elif args.output:
        output_path = args.output
    else:
        output_path = None
    
    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w') as f:
            json.dump(classified, f, ensure_ascii=False, indent=2)
        print(f"已保存到 {output_path}")
    else:
        print(json.dumps(classified, ensure_ascii=False, indent=2)[:2000])


if __name__ == "__main__":
    main()
