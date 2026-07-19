"""
AI 内容分类模块
使用 LLM (OpenAI 兼容端点) 对文案内容进行多维度分类。

分类字段:
- 业务方向: 传承 / 情感老年IP / 保险理赔
- 细分领域: 遗嘱 / 意定监护 / 继承纠纷 / 情感 / 养老 / 车险 / 农险 等
- 内容形式: 口播 / 案例讲解 / 情景剧 / 图文
- 选题方向: 自书遗嘱教程 / 遗嘱效力科普 / 独生子女继承 等
- 内容切入角度: 教程引导 / 认知恐惧 / 实用安心 / 共情引导 等
- 目标人群: 50-65岁中老年人 / 独生子女父母 / 有车一族 等
- 情绪钩子: 安全感 / 恐惧紧迫 / 实用安心 / 温暖治愈 / 委屈共鸣
- 特征标签: 逗号分隔的细粒度关键词

配置方式 (环境变量):
  LLM_BASE_URL  - API 端点 (默认 http://localhost:11434/v1)
  LLM_API_KEY   - API Key  (默认 ollama)
  LLM_MODEL     - 模型名称 (默认 qwen2.5:7b)
"""

import json
import os
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(__file__))
from config import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL


CLASSIFY_PROMPT = """你是爆款内容库的分类标注员。你的任务是把新采集的视频文案按已有数据风格分类，保持与库中现有 80 条数据的一致性。

## 待分类文案
标题: {title}
描述: {desc}
{transcript_section}

## 8 个分类字段及值域约束

### 1. 业务方向（三选一）
传承 / 情感老年IP / 保险理赔

### 2. 细分领域（优先用已有值，确实不同才创新）
已有值: 遗嘱、婆媳家庭矛盾、人生感悟正能量、养老现实独居安全、车险理赔、农险理赔
规则: 如果内容明显属于以上某个值就用它；如果确实不属于任何已有值，可以新建（如"房产继承""意定监护"），但要尽量收敛。

### 3. 内容形式
口播（绝大多数短视频都是口播形式，只有明确非口播才用其他值）
其他可选: 案例讲解、情景剧、图文

### 4. 选题方向（优先用已有值，确实不同才创新）
已有值:
- 自书遗嘱手把手教程（教怎么写自书遗嘱的步骤教程）
- 自书遗嘱效力科普（讲自书遗嘱的法律效力、注意事项）
- 遗嘱必写关键条款（讲遗嘱里必须写的某句话/某个条款）
- 独生子女继承保护（讲独生子女家庭的继承风险和保护方法）
- 遗嘱避坑与无效写法（讲常见错误写法导致遗嘱无效）
- 排除配偶离婚保护（讲如何防止遗产被子女的配偶分走）
- 特殊家庭遗嘱安排（再婚家庭、多子女家庭等特殊情况的遗嘱安排）
- 遗产纠纷真实案例（用真实案例/新闻讲继承纠纷）
- 继承过户与省税（讲房产继承过户流程、赠与vs买卖、税费）
- 婆媳关系调解 / 晚年生活态度 / 独居老人安全
- 车险理赔避坑 / 农业保险理赔指南

规则: 如果内容明显对应以上某个选题就用它；如果确实不对应，可以用简短的主题概括新建（如"打印遗嘱注意事项"），但风格要保持一致——用"XX+XX"的组合短语，不要太长。

### 5. 内容切入角度（优先用已有值）
已有值及典型场景:
- 教程科普: 手把手教步骤、讲解操作方法
- 认知恐惧: 强调"你不做XX就会XX"的恐惧后果
- 避坑警示: 警告常见错误、指出坑
- 案例故事: 用真实案例/新闻事件讲述
- 实用科普: 平实的知识科普
- 共情引导: 从情感共鸣切入
- 正能量激励: 正面鼓励
- 警示科普: 带警示意味的知识科普

### 6. 目标人群（优先用已有值）
已有值: 中老年通用、独生子女父母、已婚子女父母、50-65岁中老年人、九零后、有车一族、农户种植户

### 7. 情绪钩子（优先用已有值）
已有值: 实用安心、恐惧紧迫、愤怒不公、温暖正能量、委屈共鸣、温暖治愈
规则: 教程类多为"实用安心"，恐吓类多为"恐惧紧迫"。

### 8. 特征标签（逗号分隔，3-6个）
风格参考:
- "自书遗嘱,亲笔书写,签名落款,年月日"
- "独生子女,法定继承,自书遗嘱,代位继承,夫妻共同财产,录像留证"
- "自书遗嘱,民法典,夫妻共同财产,遗嘱执行人"
- "自书遗嘱,公证遗嘱,打印遗嘱,遗嘱无效,每一页签名,年月日"
规则: 用具体法律术语+场景关键词，不要用泛泛的大词。

## 参考示例（库中真实数据）

示例1:
标题: "自书遗嘱怎么写？只需5步，不用花一分钱！"
分类: 业务方向=传承, 细分领域=遗嘱, 内容形式=口播, 选题方向=自书遗嘱手把手教程, 内容切入角度=教程科普, 目标人群=中老年通用, 情绪钩子=实用安心, 特征标签=自书遗嘱,零成本,A4纸,五步,亲笔书写,签名落款

示例2:
标题: "独生子女父母一定要尽早立遗嘱！"
分类: 业务方向=传承, 细分领域=遗嘱, 内容形式=口播, 选题方向=独生子女继承保护, 内容切入角度=认知恐惧, 目标人群=独生子女父母, 情绪钩子=恐惧紧迫, 特征标签=独生子女,法定继承,七大姑八大姨,代位继承,自书遗嘱

示例3:
标题: "遗嘱里这三句话一定要写死"
分类: 业务方向=传承, 细分领域=遗嘱, 内容形式=口播, 选题方向=遗嘱必写关键条款, 内容切入角度=避坑警示, 目标人群=中老年通用, 情绪钩子=实用安心, 特征标签=自书遗嘱,民法典,夫妻共同财产,遗嘱执行人

## 输出格式
严格按以下 JSON 格式输出，不要加任何其他内容：
```json
{{
  "业务方向": "",
  "细分领域": "",
  "内容形式": "",
  "选题方向": "",
  "内容切入角度": "",
  "目标人群": "",
  "情绪钩子": "",
  "特征标签": ""
}}
```"""


# SSL + 无代理 opener（线程安全，不修改 os.environ）
def _build_llm_opener():
    import ssl
    import urllib.request
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except ImportError:
        ctx.load_verify_locations("/etc/ssl/cert.pem")
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ctx),
        urllib.request.HTTPHandler(),
    )


_LLM_OPENER = None


def _get_llm_opener():
    global _LLM_OPENER
    if _LLM_OPENER is None:
        _LLM_OPENER = _build_llm_opener()
    return _LLM_OPENER


def _call_llm(prompt: str, base_url: str = LLM_BASE_URL,
              api_key: str = LLM_API_KEY, model: str = LLM_MODEL,
              timeout: int = 60) -> str:
    """调用 OpenAI 兼容的 LLM API。线程安全。"""
    try:
        import urllib.request
        import urllib.error

        url = f"{base_url.rstrip('/')}/chat/completions"
        payload = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": "你是内容分类专家，只输出 JSON。"},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.1,
            "max_tokens": 500,
        }).encode('utf-8')

        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST"
        )

        with _get_llm_opener().open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return data["choices"][0]["message"]["content"]

    except urllib.error.URLError as e:
        print(f"[AI分类] LLM 端点不可达 ({base_url}): {e}")
        return ""
    except Exception as e:
        print(f"[AI分类] LLM 调用异常: {e}")
        return ""


def _parse_classify_response(raw: str) -> dict:
    """从 LLM 响应中提取 JSON 分类结果。"""
    if not raw:
        return {}

    # 尝试直接解析
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 尝试从 markdown code block 中提取
    import re
    match = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', raw)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # 尝试找到第一个 { 和最后一个 }
    start = raw.find('{')
    end = raw.rfind('}')
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass

    print(f"[AI分类] 无法解析 LLM 响应: {raw[:200]}")
    return {}


def classify_item(title: str = "", desc: str = "",
                  transcript: str = "", **kwargs) -> dict:
    """
    对单条内容进行 AI 分类。
    返回 dict: {业务方向, 细分领域, 内容形式, 选题方向, 内容切入角度, 目标人群, 情绪钩子, 特征标签}
    """
    transcript_section = f"文案全文:\n{transcript}" if transcript else "(无文案全文，仅根据标题和描述分类)"
    prompt = CLASSIFY_PROMPT.format(
        title=title or "(无标题)",
        desc=desc or "(无描述)",
        transcript_section=transcript_section,
    )

    raw = _call_llm(prompt)
    result = _parse_classify_response(raw)

    # 确保所有字段都有值（缺失的填空字符串）
    expected_keys = ["业务方向", "细分领域", "内容形式", "选题方向",
                     "内容切入角度", "目标人群", "情绪钩子", "特征标签"]
    return {k: result.get(k, "") for k in expected_keys}


def classify_batch(items: list, progress: bool = True) -> list:
    """
    批量分类。items: [{"title": str, "desc": str, "transcript": str}, ...]
    返回: [dict, ...] 每个 dict 包含8个分类字段。
    """
    results = []
    total = len(items)
    for i, item in enumerate(items):
        if progress:
            print(f"[AI分类] {i + 1}/{total}: {item.get('title', '')[:40]}...")
        r = classify_item(
            title=item.get("title", ""),
            desc=item.get("desc", ""),
            transcript=item.get("transcript", ""),
        )
        results.append(r)
    return results


if __name__ == "__main__":
    # 测试单条分类
    test = classify_item(
        title="立遗嘱时，一定要加上这句话，否则会给子女留下隐患和麻烦！",
        desc="立遗嘱时，一定要加上这句话，否则会给子女留下隐患和麻烦！#遗嘱 #麻烦 #民法典",
        transcript="大家好，今天跟大家聊一个特别重要的话题，就是立遗嘱的时候，有一句话你一定要写上...",
    )
    print("\n分类结果:")
    for k, v in test.items():
        print(f"  {k}: {v}")
