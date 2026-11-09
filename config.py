"""config.py — 配置与参数输入模块。

优先级（后者覆盖前者）：
    1. 代码内默认值
    2. 项目根目录下的 .env 文件
    3. 进程环境变量
    4. 命令行参数

安全约定：所有密钥只从环境变量 / .env 读取，不写入任何代码或产物文件。
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "reports"

SCHEMA_VERSION = 1

# --------------------------------------------------------------------------- #
# 预置列表：供用户直接复制或通过 --list-presets 查看
# --------------------------------------------------------------------------- #

PRESET_SUBREDDITS: dict[str, str] = {
    "SaaS": "SaaS 创业者与从业者，工具选型与订阅吐槽集中地",
    "Notion": "Notion 用户社区，模板、数据库、协作痛点密集",
    "productivity": "效率工具与方法论，替代品讨论多",
    "SideProject": "独立开发者展示与互评，需求验证样本好",
    "indiehackers": "独立开发者商业与产品讨论",
    "startups": "创业公司工具栈与流程痛点",
    "Entrepreneur": "中小企业主，付费意愿表达较多",
    "smallbusiness": "小微企业主，运营工具与成本敏感",
    "selfhosted": "自托管爱好者，反感订阅制，替代方案讨论多",
    "webdev": "Web 开发工具链与工作流吐槽",
    "androidapps": "安卓应用体验与功能请求",
    "iosapps": "iOS 应用体验与功能请求",
    "freelance": "自由职业者接单、开票、时间管理痛点",
    "ADHD": "注意力管理类工具的高频真实痛点（样本情绪强）",
}

PRESET_KEYWORDS: dict[str, str] = {
    "wish there was": "最强的缺失功能信号，几乎等同于需求陈述",
    "i wish": "愿望表达，覆盖面广",
    "is there a tool": "工具寻找意图，购买/试用意愿高",
    "looking for a tool": "同上，主动寻找解决方案",
    "alternative to": "替代品诉求，直接指向竞品机会",
    "feature request": "显式功能请求",
    "would pay for": "付费意愿，商业价值最高的信号",
    "shut up and take my money": "强付费意愿的口语表达",
    "so annoying": "情绪化吐槽，痛点真实度高",
    "frustrating": "挫败感表达",
    "hate that": "强烈负面情绪",
    "deal breaker": "决策否决点，功能刚需",
    "no way to": "能力缺失描述",
    "why is there no": "缺失功能反问句式",
    "missing feature": "功能缺失",
    "manual workaround": "手工绕行方案，自动化机会",
    "switched from": "流失原因，常伴随竞品对比",
    "biggest problem": "显式痛点总结",
    "求推荐": "中文社区工具寻找意图",
    "有没有": "中文社区缺失功能询问",
    "太麻烦": "中文社区低效吐槽",
}

CATEGORY_LABELS: dict[str, str] = {
    "data_import_export": "数据导入 / 导出",
    "integration": "第三方集成",
    "performance": "性能与稳定性",
    "ux_usability": "易用性 / 交互体验",
    "mobile": "移动端体验",
    "collaboration": "协作与权限",
    "automation_ai": "自动化 / AI 能力",
    "pricing_billing": "定价与计费",
    "search_organization": "搜索与信息组织",
    "reporting_analytics": "报表与分析",
    "onboarding_support": "上手引导与客服",
    "reliability_security": "可靠性与安全",
    "other": "其他",
}

VALID_CATEGORIES = tuple(CATEGORY_LABELS.keys())
VALID_COLLECT_MODES = ("top", "search", "both")


class ConfigError(RuntimeError):
    """配置缺失或非法时抛出，由 main.py 统一捕获并友好提示。"""


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #


def load_dotenv(path: Path, override: bool = False) -> None:
    """极简 .env 解析器（避免引入 python-dotenv 依赖）。

    支持 `KEY=VALUE`、`export KEY=VALUE`、引号包裹、`#` 注释。
    默认不覆盖已存在的环境变量，符合 12-factor 约定。
    """
    if not path.is_file():
        return
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = value


def _env(*names: str, default: str = "") -> str:
    """按顺序读取第一个非空环境变量，便于兼容多种命名习惯。"""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def parse_list(value: Any) -> list[str]:
    """把 CLI 字符串 / 环境变量解析成去重列表。

    支持英文逗号、中文逗号、分号、换行分隔，自动去掉 r/ 前缀与首尾空白。
    """
    if value is None:
        return []
    items: Iterable[str]
    if isinstance(value, (list, tuple, set)):
        items = [str(v) for v in value]
    else:
        text = str(value)
        for sep in ("，", ";", "；", "\n", "|"):
            text = text.replace(sep, ",")
        items = text.split(",")

    result: list[str] = []
    for item in items:
        cleaned = item.strip()
        if not cleaned:
            continue
        if cleaned.lower().startswith("r/"):
            cleaned = cleaned[2:]
        cleaned = cleaned.strip("/ ")
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def _as_bool(value: str | bool | None, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


# --------------------------------------------------------------------------- #
# 配置对象
# --------------------------------------------------------------------------- #


@dataclass
class Settings:
    """全流程运行参数。字段名与 README 中的环境变量一一对应。"""

    # --- Reddit 凭证 ---
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = ""
    reddit_username: str = ""
    reddit_password: str = ""
    reddit_refresh_token: str = ""

    # --- 大模型 ---
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.2
    llm_timeout: float = 120.0
    llm_max_retries: int = 4
    use_llm: bool = True

    # --- 检索参数 ---
    subreddits: list[str] = field(default_factory=lambda: ["SaaS", "SideProject"])
    keywords: list[str] = field(default_factory=lambda: list(PRESET_KEYWORDS)[:6])
    time_range_days: int = 365
    post_limit: int = 500
    comment_limit: int = 8
    comment_min_score: int = 2
    post_min_score: int = 1
    collect_mode: str = "top"
    per_keyword_limit: int = 50
    request_delay: float = 0.0
    force_refresh: bool = False

    # --- 分析参数 ---
    batch_size: int = 8
    max_posts_to_analyze: int = 300
    max_chars_per_post: int = 2600
    max_chars_per_comment: int = 700
    merge_with_llm: bool | None = None  # None = 自动（items 多时启用）
    merge_min_items: int = 25
    min_evidence_posts: int = 1

    # --- 路径与运行模式 ---
    data_dir: Path = DEFAULT_DATA_DIR
    output_dir: Path = DEFAULT_OUTPUT_DIR
    demo: bool = False
    from_cache: bool = False
    quiet: bool = False

    # ---------------------------------------------------------------- #
    # 派生属性
    # ---------------------------------------------------------------- #
    @property
    def time_filter(self) -> str:
        """把天数映射为 PRAW 的 time_filter 枚举值（文档限定 hour/day/week/month/year/all）。"""
        days = self.time_range_days
        if days <= 1:
            return "day"
        if days <= 7:
            return "week"
        if days <= 31:
            return "month"
        if days <= 365:
            return "year"
        return "all"

    @property
    def has_reddit_credentials(self) -> bool:
        return bool(
            self.reddit_client_id and self.reddit_client_secret and self.reddit_user_agent
        )

    @property
    def has_llm_credentials(self) -> bool:
        if self.llm_api_key:
            return True
        # 本地 Ollama / LM Studio 等通常不校验 API Key
        local_markers = ("localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal")
        return any(marker in self.llm_base_url for marker in local_markers)

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def raw_data_path(self) -> Path:
        return self.data_dir / "raw_data.json"

    # ---------------------------------------------------------------- #
    # 校验
    # ---------------------------------------------------------------- #
    def validate(self) -> None:
        """快速失败：把配置问题在花钱调用 API 之前暴露出来。"""
        if not self.subreddits:
            raise ConfigError(
                "未指定目标 Subreddit。请使用 --subreddits SaaS,Notion 或设置 REDDIT_SUBREDDITS。"
            )
        if not (1 <= self.time_range_days <= 3650):
            raise ConfigError("time_range_days 必须在 1-3650 之间。")
        if not (1 <= self.post_limit <= 1000):
            raise ConfigError(
                "post_limit 必须在 1-1000 之间（Reddit listing 单次最多返回 1000 条，"
                "更大范围请拆分赛道或缩小 time range）。"
            )
        if not (0 <= self.comment_limit <= 50):
            raise ConfigError("comment_limit 必须在 0-50 之间。")
        if not (1 <= self.batch_size <= 40):
            raise ConfigError("batch_size 必须在 1-40 之间。")
        if not (1 <= self.max_posts_to_analyze <= 1000):
            raise ConfigError("max_posts_to_analyze 必须在 1-1000 之间。")
        if self.collect_mode not in VALID_COLLECT_MODES:
            raise ConfigError(f"collect_mode 必须是 {VALID_COLLECT_MODES} 之一。")
        if self.collect_mode in {"search", "both"} and not self.keywords:
            raise ConfigError(
                "collect_mode 为 search/both 时必须提供至少一个检索关键词。"
            )
        if not self.demo and not self.from_cache and not self.has_reddit_credentials:
            raise ConfigError(
                "缺少 Reddit API 凭证。请在 .env 或环境变量中设置 "
                "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET / REDDIT_USER_AGENT。\n"
                "申请方式见 README.md「第一步：申请 Reddit API 凭证」。"
            )
        if not self.demo and not self.from_cache and not self.reddit_user_agent:
            raise ConfigError("缺少 REDDIT_USER_AGENT（Reddit 强制要求，且必须是描述性字符串）。")
        if self.use_llm and not self.has_llm_credentials:
            raise ConfigError(
                "已启用 AI 分析但缺少大模型凭证。请设置 LLM_API_KEY（以及可选的 LLM_BASE_URL），"
                "或加 --no-llm 使用离线启发式分析。"
            )

    def public_meta(self) -> dict[str, Any]:
        """用于写入报告元信息，自动剔除任何凭证。"""
        return {
            "subreddits": list(self.subreddits),
            "keywords": list(self.keywords),
            "time_range_days": self.time_range_days,
            "time_filter": self.time_filter,
            "post_limit": self.post_limit,
            "comment_limit": self.comment_limit,
            "max_posts_to_analyze": self.max_posts_to_analyze,
            "collect_mode": self.collect_mode,
            "llm_model": self.llm_model if self.use_llm else "heuristic (offline)",
            "demo": self.demo,
            "schema_version": SCHEMA_VERSION,
        }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reddit-backlog-agent",
        description="抓取 Reddit 讨论，用 AI 聚合出功能愿望清单与痛点优先级报告。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python main.py --subreddits SaaS,Notion --days 365 --limit 300\n"
            "  python main.py --keywords \"wish there was,alternative to\" --mode search\n"
            "  python main.py --demo            # 无需任何密钥，离线跑通全流程\n"
            "  python main.py --from-cache      # 复用已抓取数据，只重跑分析\n"
            "  python main.py --list-presets    # 查看预置板块与关键词\n"
        ),
    )
    parser.add_argument(
        "--subreddits",
        action="append",
        metavar="LIST",
        help="目标板块，逗号分隔，可重复传入。例如 SaaS,Notion",
    )
    parser.add_argument(
        "--keywords",
        action="append",
        metavar="LIST",
        help="检索 / 信号词，逗号分隔，可重复传入。",
    )
    parser.add_argument("--days", type=int, metavar="N", help="时间范围（天），默认 365。")
    parser.add_argument("--limit", type=int, metavar="N", help="每个板块最大帖子数（<=1000）。")
    parser.add_argument("--comments", type=int, metavar="N", help="每帖保留的高赞评论条数。")
    parser.add_argument("--comment-min-score", type=int, metavar="N", help="评论最低赞数。")
    parser.add_argument(
        "--mode",
        choices=VALID_COLLECT_MODES,
        help="采集模式：top（板块热帖）/ search（关键词搜索）/ both。",
    )
    parser.add_argument("--batch-size", type=int, metavar="N", help="单次送模型的帖子数。")
    parser.add_argument(
        "--analyze-limit", type=int, metavar="N", help="最多送多少篇帖子给模型分析（控制成本）。"
    )
    parser.add_argument("--model", metavar="NAME", help="大模型名称，覆盖 LLM_MODEL。")
    parser.add_argument("--base-url", metavar="URL", help="OpenAI 兼容 Base URL。")
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="不调用大模型，使用离线启发式规则分析（结果较粗糙，可用于试跑）。",
    )
    parser.add_argument("--demo", action="store_true", help="使用内置样例数据离线演示。")
    parser.add_argument(
        "--from-cache", action="store_true", help="跳过采集，直接分析 data/raw_data.json。"
    )
    parser.add_argument(
        "--refresh", action="store_true", help="忽略本地缓存，强制重新请求 Reddit。"
    )
    parser.add_argument("--data-dir", metavar="PATH", help="中间数据目录，默认 ./data。")
    parser.add_argument("--output-dir", metavar="PATH", help="报告输出目录，默认 ./reports。")
    parser.add_argument("--quiet", action="store_true", help="只输出警告与最终结果路径。")
    parser.add_argument(
        "--list-presets", action="store_true", help="打印预置板块与关键词后退出。"
    )
    return parser


def print_presets() -> None:
    print("预置 Subreddit 列表（可直接用于 --subreddits）：\n")
    width = max(len(name) for name in PRESET_SUBREDDITS)
    for name, desc in PRESET_SUBREDDITS.items():
        print(f"  {name.ljust(width)}  {desc}")
    print("\n预置关键词列表（可直接用于 --keywords）：\n")
    width = max(len(word) for word in PRESET_KEYWORDS)
    for word, desc in PRESET_KEYWORDS.items():
        print(f'  "{word}"'.ljust(width + 4) + f" {desc}")
    print("\n提示：关键词越贴近真实抱怨句式，信号质量越高。")


def settings_from_env() -> Settings:
    """从环境变量构造基础配置（.env 已在 build_settings 中加载）。"""
    return Settings(
        reddit_client_id=_env("REDDIT_CLIENT_ID", "REDDIT_CLIENTID", "PRAW_CLIENT_ID"),
        reddit_client_secret=_env(
            "REDDIT_CLIENT_SECRET", "REDDIT_SECRET", "PRAW_CLIENT_SECRET"
        ),
        reddit_user_agent=_env("REDDIT_USER_AGENT", "PRAW_USER_AGENT"),
        reddit_username=_env("REDDIT_USERNAME"),
        reddit_password=_env("REDDIT_PASSWORD"),
        reddit_refresh_token=_env("REDDIT_REFRESH_TOKEN"),
        llm_api_key=_env("LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"),
        llm_base_url=_env("LLM_BASE_URL", "OPENAI_BASE_URL"),
        llm_model=_env("LLM_MODEL", "OPENAI_MODEL", default="gpt-4o-mini"),
        use_llm=_as_bool(os.environ.get("USE_LLM"), default=True),
        subreddits=parse_list(os.environ.get("REDDIT_SUBREDDITS", ""))
        or Settings().subreddits,
        keywords=parse_list(os.environ.get("REDDIT_KEYWORDS", "")) or Settings().keywords,
        time_range_days=int(os.environ.get("TIME_RANGE_DAYS", "365") or 365),
        post_limit=int(os.environ.get("POST_LIMIT", "500") or 500),
    )


def build_settings(argv: list[str] | None = None) -> Settings:
    """解析 CLI 并合成最终配置。"""
    load_dotenv(PROJECT_ROOT / ".env")
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_presets:
        print_presets()
        sys.exit(0)

    settings = settings_from_env()

    if args.subreddits:
        settings.subreddits = parse_list(args.subreddits)
    if args.keywords:
        settings.keywords = parse_list(args.keywords)
    if args.days is not None:
        settings.time_range_days = args.days
    if args.limit is not None:
        settings.post_limit = args.limit
    if args.comments is not None:
        settings.comment_limit = args.comments
    if args.comment_min_score is not None:
        settings.comment_min_score = args.comment_min_score
    if args.mode is not None:
        settings.collect_mode = args.mode
    if args.batch_size is not None:
        settings.batch_size = args.batch_size
    if args.analyze_limit is not None:
        settings.max_posts_to_analyze = args.analyze_limit
    if args.model:
        settings.llm_model = args.model
    if args.base_url:
        settings.llm_base_url = args.base_url
    if args.no_llm:
        settings.use_llm = False
    if args.demo:
        settings.demo = True
        settings.from_cache = True
        settings.use_llm = False
    if args.from_cache:
        settings.from_cache = True
    if args.refresh:
        settings.force_refresh = True
    if args.data_dir:
        settings.data_dir = Path(args.data_dir).expanduser().resolve()
    if args.output_dir:
        settings.output_dir = Path(args.output_dir).expanduser().resolve()
    settings.quiet = bool(args.quiet)

    settings.validate()
    return settings


def iter_field_names() -> list[str]:
    """调试辅助：列出所有配置字段名。"""
    return [f.name for f in fields(Settings)]


if __name__ == "__main__":  # pragma: no cover - 手工调试入口
    cfg = build_settings()
    for key, value in cfg.public_meta().items():
        print(f"{key}: {value}")
