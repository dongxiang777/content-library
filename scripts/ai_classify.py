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


CLASSIFY_PROMPT = """你是一个短视频内容分类专家。请根据以下文案内容，对视频进行多维度分类。

## 文案信息
标题: {title}
描述: {desc}
{transcript_section}

## 分类要求
请为以下8个维度各给出一个分类值：

1. **业务方向** (只能选一个): 传承 / 情感老年IP / 保险理赔
2. **细分领域**: 如 遗嘱、意定监护、继承纠纷、房产继承、情感、养老、家庭矛盾、车险、农险、人身保险 等
3. **内容形式**: 口播 / 案例讲解 / 情景剧 / 图文 / 街采 / 访谈
4. **选题方向**: 用简短的主题概括，如"自书遗嘱手把手教程"、"独生子女继承保护"、"遗嘱必写关键条款"
5. **内容切入角度**: 教程引导 / 认知恐惧 / 实用安心 / 正能量激励 / 警示科普 / 共情引导
6. **目标人群**: 如"50-65岁中老年人"、"独生子女父母"、"有车一族"、"农户种植户"
7. **情绪钩子**: 安全感 / 恐惧紧迫 / 实用安心 / 温暖治愈 / 委屈共鸣
8. **特征标签**: 3-6个逗号分隔的细粒度关键词，如"自书遗嘱,签名落款,民法典1134"

## 输出格式
严格按以下 JSON 格式输出，不要加其他内容：
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


def _call_llm(prompt: str, base_url: str = LLM_BASE_URL,
              api_key: str = LLM_API_KEY, model: str = LLM_MODEL,
              timeout: int = 60) -> str:
    """调用 OpenAI 兼容的 LLM API。"""
    try:
        import ssl
        import urllib.request
        import urllib.error

        # SSL: 加载系统证书（Homebrew Python 缺根证书）
        _ctx = ssl.create_default_context()
        try:
            import certifi
            _ctx.load_verify_locations(certifi.where())
        except ImportError:
            _ctx.load_verify_locations("/etc/ssl/cert.pem")

        # 代理绕过：国内 API 直连
        _PROXY_KEYS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")
        _saved = {k: os.environ.pop(k) for k in _PROXY_KEYS if k in os.environ}

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

        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                return data["choices"][0]["message"]["content"]
        finally:
            os.environ.update(_saved)

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
