"""
爆款内容库 Pipeline 配置
列位置不再硬编码 — 运行时通过 feishu_utils.get_column_map() 读飞书表头动态获取。
"""

# ===== 飞书表格 =====
SPREADSHEET_TOKEN = "RwZFsg8klhpzWVtHrpUcnguAn4g"
SHEET_ID = "3a6f67"

# 字段名清单（顺序无关紧要，运行时按飞书表头匹配列位置）
FIELD_NAMES = [
    "业务方向", "细分领域", "内容形式", "选题方向", "内容切入角度",
    "目标人群", "情绪钩子", "特征标签", "文案全文", "标题",
    "平台", "原视频标签", "原始链接", "创作者",
    "点赞", "收藏", "转发", "评论", "入库日期",
]

# ===== MediaCrawler =====
MEDIACRAWLER_DIR = "/Users/shaoxinjiang/CodexWorkspace/projects/content library/tools/MediaCrawler"
VENV_PYTHON = f"{MEDIACRAWLER_DIR}/.venv/bin/python"

# ===== 项目路径 =====
PROJECT_DIR = "/Users/shaoxinjiang/CodexWorkspace/projects/content library"
DATA_DIR = f"{PROJECT_DIR}/data"
SCRIPTS_DIR = f"{PROJECT_DIR}/scripts"

# ===== 加载项目 .env =====
import os as _os

def _load_env():
    """从项目 .env 文件加载环境变量（不覆盖已有）。"""
    _env_path = _os.path.join(_os.path.dirname(__file__), "..", ".env")
    _env_path = _os.path.normpath(_env_path)
    if not _os.path.isfile(_env_path):
        return
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            _k, _v = _k.strip(), _v.strip().strip("'\"")
            if _k and _k not in _os.environ:
                _os.environ[_k] = _v

_load_env()

# ===== AI 分类 LLM 配置 (OpenAI 兼容端点) =====
LLM_BASE_URL = _os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_API_KEY = _os.environ.get("LLM_API_KEY", "")
LLM_MODEL = _os.environ.get("LLM_MODEL", "deepseek-chat")

# ===== Pipeline 并行度 =====
# prepare（下载+ffmpeg）预取条数 / 线程数；FunASR 仍串行（daemon 单通道）
PREPARE_WORKERS = int(_os.environ.get("PREPARE_WORKERS", "2"))
# AI 分类等 I/O 线程数（飞书 append 在 pipeline 侧串行，保证行序）
IO_WORKERS = int(_os.environ.get("IO_WORKERS", "2"))

# ===== 搜索关键词 (按业务线) =====
SEARCH_KEYWORDS = {
    "传承": [
        "遗嘱怎么写", "自书遗嘱", "立遗嘱", "遗嘱模板",
        "遗嘱公证", "遗产继承", "独生子女继承",
    ],
    "情感老年IP": [
        "老年生活", "退休生活", "婆媳关系", "家庭矛盾",
        "养老", "独居老人",
    ],
    "保险理赔": [
        "车险理赔", "保险拒赔", "农业保险", "交通事故理赔",
    ],
}

# ===== 标签提取工具 =====
import re

def extract_hashtags(text: str) -> tuple:
    """从标题/描述中提取 #标签，返回 (清理后的文本, 标签列表)。
    支持抖音和视频号格式：#标签 #标签 #标签#标签
    """
    if not text:
        return "", []
    tags = re.findall(r'#([^\s#]+)', text)
    cleaned = re.sub(r'\s*#[^\s#]+', '', text).strip()
    return cleaned, tags
