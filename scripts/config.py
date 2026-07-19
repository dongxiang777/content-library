"""
爆款内容库 Pipeline 配置
"""

# ===== 飞书表格 =====
SPREADSHEET_TOKEN = "RwZFsg8klhpzWVtHrpUcnguAn4g"
SHEET_ID = "3a6f67"

# 19列字段定义 (A-S)
FIELDS = [
    "业务方向",     # A (0)
    "细分领域",     # B (1)
    "内容形式",     # C (2)
    "选题方向",     # D (3)
    "内容切入角度", # E (4)
    "目标人群",     # F (5)
    "情绪钩子",     # G (6)
    "特征标签",     # H (7)
    "文案全文",     # I (8)
    "标题",         # J (9)
    "平台",         # K (10)
    "原始链接",     # L (11)
    "创作者",       # M (12)
    "点赞",         # N (13)
    "收藏",         # O (14)
    "转发",         # P (15)
    "评论",         # Q (16)
    "入库日期",     # R (17)
    "原视频标签",   # S (18)
]

# 字段索引快捷引用
IDX_BIZ = 0       # 业务方向
IDX_SUB = 1       # 细分领域
IDX_FORM = 2      # 内容形式
IDX_TOPIC = 3     # 选题方向
IDX_ANGLE = 4     # 内容切入角度
IDX_AUDIENCE = 5  # 目标人群
IDX_EMOTION = 6   # 情绪钩子
IDX_TAGS = 7      # 特征标签
IDX_TRANSCRIPT = 8  # 文案全文
IDX_TITLE = 9     # 标题
IDX_PLATFORM = 10 # 平台
IDX_URL = 11      # 原始链接
IDX_CREATOR = 12  # 创作者
IDX_LIKES = 13    # 点赞
IDX_COLLECTS = 14 # 收藏
IDX_SHARES = 15   # 转发
IDX_COMMENTS = 16 # 评论
IDX_DATE = 17     # 入库日期
IDX_HASHTAGS = 18 # 原视频标签

# ===== 标签提取工具 =====
import re

def extract_hashtags(text: str) -> tuple:
    """从标题/描述中提取 #标签，返回 (清理后的文本, 标签列表)。
    支持抖音和视频号格式：#标签 #标签 #标签#标签
    """
    if not text:
        return "", []
    # 匹配 # 后跟非空白字符（到下一个 # 或空格或字符串结尾）
    tags = re.findall(r'#([^\s#]+)', text)
    # 从文本中移除标签部分
    cleaned = re.sub(r'\s*#[^\s#]+', '', text).strip()
    return cleaned, tags

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
# 可通过环境变量覆盖: LLM_BASE_URL, LLM_API_KEY, LLM_MODEL
LLM_BASE_URL = _os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_API_KEY = _os.environ.get("LLM_API_KEY", "")
LLM_MODEL = _os.environ.get("LLM_MODEL", "deepseek-chat")

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
