"""
爆款内容库 Pipeline 配置
列位置不再硬编码 — 运行时通过 feishu_utils.get_column_map() 读飞书表头动态获取。
"""

# ===== 飞书表格 =====
SPREADSHEET_TOKEN = "RwZFsg8klhpzWVtHrpUcnguAn4g"
SHEET_ID = "3a6f67"  # 默认子表（传承 IP）

# 业务方向 → 子表 sheet_id 映射
SHEET_MAP = {
    "传承": "3a6f67",       # 传承 IP
    "情感": "sy4S58",       # 情感 IP
    "理赔": "SEtYpj",       # 理赔
}

def resolve_sheet_id(business: str) -> str:
    """根据业务方向字符串匹配对应子表 sheet_id。
    匹配规则：业务方向包含关键词即命中（如 "情感老年IP" → 情感表）。
    未匹配时回退到默认 SHEET_ID（传承）。
    """
    if not business:
        return SHEET_ID
    for keyword, sid in SHEET_MAP.items():
        if keyword in business:
            return sid
    return SHEET_ID

# 字段名清单（顺序无关紧要，运行时按飞书表头匹配列位置）
FIELD_NAMES = [
    "业务方向", "细分领域", "内容形式", "选题方向", "内容切入角度",
    "目标人群", "情绪钩子", "特征标签", "文案全文", "标题",
    "平台", "原视频标签", "原始链接", "创作者",
    "点赞", "收藏", "转发", "评论", "入库日期",
]

# ===== 项目路径（动态计算，跨电脑/跨平台可用，不写死绝对路径）=====
import os as _os

# scripts/config.py 所在目录即 scripts/，上一级即项目根目录
SCRIPTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
PROJECT_DIR = _os.path.dirname(SCRIPTS_DIR)
DATA_DIR = _os.path.join(PROJECT_DIR, "data")

# ===== MediaCrawler（抖音采集，需单独克隆到 tools/ 下）=====
MEDIACRAWLER_DIR = _os.path.join(PROJECT_DIR, "tools", "MediaCrawler")
# 整个项目复用 MediaCrawler 的虚拟环境（FunASR 也装在这里）。
# Windows 的 venv 解释器在 Scripts\python.exe，macOS/Linux 在 bin/python。
_VENV_BIN = "Scripts" if _os.name == "nt" else "bin"
_PY_EXE = "python.exe" if _os.name == "nt" else "python"
VENV_PYTHON = _os.path.join(MEDIACRAWLER_DIR, ".venv", _VENV_BIN, _PY_EXE)

# ===== 加载项目 .env =====

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

# ===== 视频号 (wx_video_download 本地服务) =====
# 服务需已启动（默认 http://127.0.0.1:2022），微信侧 socket 需已初始化。
CHANNELS_API_BASE = _os.environ.get("CHANNELS_API_BASE", "http://127.0.0.1:2022")
# MITM 代理：finder.video.qq.com 解密下载（与 config.yaml proxy.port 一致）
CHANNELS_PROXY = _os.environ.get("CHANNELS_PROXY", "http://127.0.0.1:2023")
CHANNELS_DOWNLOAD_DIR = f"{DATA_DIR}/sph_videos"
CHANNELS_STATE = f"{DATA_DIR}/channels_state.json"

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

    视频号搜索结果常带 HTML 高亮（<em class="highlight">…</em>），
    必须先剥标签再提取，否则 hashtag 会被截断、标题残留碎片。
    """
    if not text:
        return "", []
    # 清除 HTML 标签（搜索高亮等），避免污染标签与标题
    text = re.sub(r'<[^>]+>', '', text)
    tags = re.findall(r'#([^\s#]+)', text)
    cleaned = re.sub(r'\s*#[^\s#]+', '', text).strip()
    return cleaned, tags
