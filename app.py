"""app.py — Reddit 赛道需求挖掘 Agent 的可视化界面（Streamlit）。

启动方式
--------
    python -m streamlit run app.py
    # 或双击 start_ui.bat

说明
----
- 界面只负责「收集参数 → 调用既有模块 → 展示结果」，采集 / 分析 / 报告的
  全部业务逻辑仍由 config.py / scraper.py / analyzer.py / reporter.py 承担，
  与命令行版本共用同一套代码，不存在两套实现。
- 凭证优先从 .env 读取；界面上可以直接覆盖，也可以一键写回 .env。
- 每次运行的中间数据仍在 data/，报告仍在 reports/，命令行与界面可以互相接续。
"""

from __future__ import annotations

import html
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import scraper
from analyzer import AnalyzerError, analyze
from config import (
    CATEGORY_LABELS,
    PRESET_KEYWORDS,
    PRESET_SUBREDDITS,
    PROJECT_ROOT,
    ConfigError,
    Settings,
    load_dotenv,
    parse_list,
)
from reporter import build_dataframe, priority_of, write_reports

DEMO_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "sample_raw.json"
ENV_FILE = PROJECT_ROOT / ".env"

PRIORITY_COLORS = {"P0": "#e5484d", "P1": "#f08c00", "P2": "#3b82f6", "P3": "#8b95a5"}
PRIORITY_HINT = {
    "P0": "强度 ≥ 8，MVP 必须覆盖",
    "P1": "强度 6.5-8，有明确场景",
    "P2": "强度 4.5-6.5，信号分散",
    "P3": "强度 < 4.5，暂不投入",
}

LLM_PRESETS: dict[str, dict[str, str]] = {
    "DeepSeek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "OpenAI": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "通义千问（兼容模式）": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
    },
    "Kimi（Moonshot）": {"base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    "本地 Ollama": {"base_url": "http://localhost:11434/v1", "model": "qwen2.5:7b"},
    "自定义兼容端点": {"base_url": "", "model": ""},
}

COLLECT_LABELS = {
    "板块热帖（top）": "top",
    "关键词搜索（search）": "search",
    "两者都抓（both）": "both",
}

NAV_ITEMS = ["① 配置任务", "② 分析看板", "③ 报告与导出", "④ 使用说明"]

CSS = """
<style>
.block-container {padding-top: 2.0rem; padding-bottom: 3rem; max-width: 1220px;}
.rb-hero {
  border-radius: 18px; padding: 22px 26px 18px 26px; margin-bottom: 14px;
  border: 1px solid rgba(128,128,128,.28);
  background: linear-gradient(135deg, rgba(59,130,246,.14) 0%, rgba(59,130,246,.02) 60%, transparent 100%);
}
.rb-hero h1 {font-size: 1.55rem; margin: 0 0 6px 0; letter-spacing: .3px;}
.rb-hero p {margin: 0; opacity: .8; font-size: .92rem;}
.rb-badge {
  display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: .74rem;
  border: 1px solid rgba(128,128,128,.4); margin: 6px 6px 0 0; line-height: 1.6;
}
.rb-badge.ok {color: #16a34a; border-color: rgba(22,163,74,.45); background: rgba(22,163,74,.08);}
.rb-badge.bad {color: #dc2626; border-color: rgba(220,38,38,.45); background: rgba(220,38,38,.08);}
.rb-badge.info {color: #2563eb; border-color: rgba(37,99,235,.40); background: rgba(37,99,235,.08);}
.rb-quote {
  border-left: 3px solid #3b82f6; padding: 4px 0 4px 14px;
  margin: 10px 0 2px 0; font-size: .95rem; line-height: 1.55;
}
.rb-quote small {opacity: .65;}
.rb-note {font-size: .82rem; opacity: .72; line-height: 1.6;}
[data-testid="stMetricValue"] {font-size: 1.45rem;}
[data-testid="stMetricLabel"] {opacity: .75;}
div[data-testid="stSidebarNav"] {display: none;}
</style>
"""


# --------------------------------------------------------------------------- #
# 基础设施
# --------------------------------------------------------------------------- #


def _inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def _init_state() -> None:
    defaults = {
        "result": None,
        "payload": None,
        "paths": {},
        "run_log": [],
        "nav_index": NAV_ITEMS[0],
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def _goto(page: str) -> None:
    """跳转到指定页：必须在 radio 实例化之前改值，所以先存一个待处理标记。"""
    st.session_state["_nav_target"] = page
    st.rerun()


def _has(value: str | None) -> bool:
    return bool(value and str(value).strip())


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or default


def _cache_stats() -> tuple[int, str]:
    """返回本地缓存帖子数与生成时间，用于界面提示。"""
    path = PROJECT_ROOT / "data" / "raw_data.json"
    if not path.is_file():
        return 0, ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0, ""
    meta = payload.get("meta") or {}
    generated = str(meta.get("generated_at", ""))[:16].replace("T", " ")
    return len(payload.get("posts") or []), generated


def _write_env(values: dict[str, str]) -> None:
    """把非空值合并写回 .env，保留文件中其他行与注释。"""
    lines: list[str] = []
    if ENV_FILE.is_file():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()

    pending = {k: v for k, v in values.items() if v}
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in pending:
            lines[index] = f"{key}={pending.pop(key)}"

    if pending:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# 由可视化界面写入")
        lines.extend(f"{key}={value}" for key, value in pending.items())

    ENV_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


class _LogCapture(logging.Handler):
    """把运行期日志收进列表，界面里可以直接看。"""

    def __init__(self, sink: list[str]) -> None:
        super().__init__()
        self.sink = sink
        self.setFormatter(
            logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S")
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink.append(self.format(record))
            del self.sink[:-400]
        except Exception:  # noqa: BLE001 - 日志本身不能抛错
            pass


# --------------------------------------------------------------------------- #
# 侧边栏：连接与凭证
# --------------------------------------------------------------------------- #


def _render_sidebar() -> dict[str, Any]:
    with st.sidebar:
        st.markdown("### 连接设置")
        st.caption("凭证只写入本机 .env 或当前会话，不会出现在报告里。")

        has_creds = all(
            _has(_env(name))
            for name in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT")
        )
        with st.expander("Reddit API 凭证", expanded=not has_creds):
            client_id = st.text_input(
                "Client ID",
                value=_env("REDDIT_CLIENT_ID"),
                help="reddit.com/prefs/apps 创建 app 后，名称下方那串短字符。",
            )
            client_secret = st.text_input(
                "Client Secret",
                value=_env("REDDIT_CLIENT_SECRET"),
                type="password",
                help="页面上标着 secret 的长串。",
            )
            user_agent = st.text_input(
                "User-Agent",
                value=_env("REDDIT_USER_AGENT") or "script:reddit-backlog-agent:v1.0 (by u/your_name)",
                help="Reddit 要求的描述性字符串，建议带上你的 Reddit 用户名。",
            )
            st.markdown(
                '<div class="rb-note">只读采集不需要账号密码，也不需要浏览器授权跳转。'
                "详见「使用说明」页。</div>",
                unsafe_allow_html=True,
            )
            if st.button("保存到 .env", width="stretch"):
                try:
                    _write_env(
                        {
                            "REDDIT_CLIENT_ID": client_id.strip(),
                            "REDDIT_CLIENT_SECRET": client_secret.strip(),
                            "REDDIT_USER_AGENT": user_agent.strip(),
                        }
                    )
                    st.success("已写入 .env（本机明文保存，请勿提交到 Git）。")
                except OSError as exc:
                    st.error(f"写入失败：{exc}")

        st.divider()
        st.markdown("### 大模型")
        provider = st.selectbox("服务商", list(LLM_PRESETS), index=0)
        preset = LLM_PRESETS[provider]
        base_url = st.text_input(
            "Base URL",
            value=_env("LLM_BASE_URL") or preset["base_url"],
            placeholder="https://api.deepseek.com/v1",
        )
        model = st.text_input(
            "模型名", value=_env("LLM_MODEL") or preset["model"] or "gpt-4o-mini"
        )
        api_key = st.text_input(
            "API Key",
            value=_env("LLM_API_KEY") or _env("OPENAI_API_KEY"),
            type="password",
            help="本地 Ollama 可留空。",
        )
        use_llm = st.checkbox(
            "启用 AI 语义分析",
            value=True,
            help="关闭后走离线规则聚类，只能用来试跑流程，结论质量明显下降。",
        )

        st.divider()
        with st.expander("高级参数"):
            batch_size = st.number_input(
                "每批帖子数", min_value=1, max_value=40, value=8, step=1,
                help="每批越大越省调用次数，但单次输出更容易不完整。",
            )
            analyze_limit = st.number_input(
                "最多分析帖数", min_value=1, max_value=1000, value=300, step=10,
                help="按「关键词命中 + 热度」排序后截断，直接决定 token 成本。",
            )
            comment_min_score = st.number_input(
                "评论最低赞数", min_value=0, max_value=100, value=2, step=1,
                help="过滤低赞噪声评论。",
            )
            request_delay = st.number_input(
                "请求间隔（秒）", min_value=0.0, max_value=5.0, value=0.0, step=0.1,
                help="遇到 429 时可调大到 0.5-1.0。",
            )

    posts_cached, cached_at = _cache_stats()
    return {
        "client_id": client_id.strip(),
        "client_secret": client_secret.strip(),
        "user_agent": user_agent.strip(),
        "provider": provider,
        "base_url": base_url.strip(),
        "model": model.strip(),
        "api_key": api_key.strip(),
        "use_llm": use_llm,
        "batch_size": int(batch_size),
        "analyze_limit": int(analyze_limit),
        "comment_min_score": int(comment_min_score),
        "request_delay": float(request_delay),
        "cached_posts": posts_cached,
        "cached_at": cached_at,
    }


def _render_header(conn: dict[str, Any]) -> None:
    creds_ok = bool(conn["client_id"] and conn["client_secret"] and conn["user_agent"])
    cred_badge = (
        '<span class="rb-badge ok">Reddit 凭证已配置</span>'
        if creds_ok
        else '<span class="rb-badge bad">Reddit 凭证未配置</span>'
    )
    if conn["use_llm"] and (conn["api_key"] or "localhost" in conn["base_url"]):
        llm_badge = f'<span class="rb-badge info">模型 {html.escape(conn["model"] or "-")}</span>'
    else:
        llm_badge = '<span class="rb-badge">规则模式（未启用 AI）</span>'

    if conn["cached_posts"]:
        cache_badge = (
            f'<span class="rb-badge">本地缓存 {conn["cached_posts"]} 帖'
            f'{" · " + conn["cached_at"] if conn["cached_at"] else ""}</span>'
        )
    else:
        cache_badge = '<span class="rb-badge">本地暂无缓存</span>'

    st.markdown(
        f"""
        <div class="rb-hero">
          <h1>Reddit 赛道需求挖掘 Agent</h1>
          <p>抓取子版块讨论 → AI 聚类痛点与打分 → 输出可执行的功能愿望清单</p>
          <div>{cred_badge}{llm_badge}{cache_badge}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------- #
# 页面一：任务配置
# --------------------------------------------------------------------------- #


def _build_settings(conn: dict[str, Any], params: dict[str, Any], mode: str) -> Settings:
    settings = Settings()
    settings.reddit_client_id = conn["client_id"]
    settings.reddit_client_secret = conn["client_secret"]
    settings.reddit_user_agent = conn["user_agent"]

    settings.llm_api_key = conn["api_key"]
    settings.llm_base_url = conn["base_url"]
    settings.llm_model = conn["model"]
    settings.use_llm = bool(conn["use_llm"])

    settings.subreddits = params["subreddits"]
    settings.keywords = params["keywords"]
    settings.time_range_days = params["days"]
    settings.post_limit = params["limit"]
    settings.comment_limit = params["comments"]
    settings.collect_mode = params["collect_mode"]
    settings.batch_size = conn["batch_size"]
    settings.max_posts_to_analyze = conn["analyze_limit"]
    settings.comment_min_score = conn["comment_min_score"]
    settings.request_delay = conn["request_delay"]
    settings.quiet = True  # 关掉 tqdm，进度交给界面
    settings.data_dir = PROJECT_ROOT / "data"
    settings.output_dir = PROJECT_ROOT / "reports"

    if mode == "demo":
        settings.demo = True
        settings.from_cache = True
        settings.use_llm = False
    elif mode == "cache":
        settings.from_cache = True
    return settings


def _page_config(conn: dict[str, Any]) -> None:
    st.markdown("#### 1. 数据来源")
    mode_label = st.radio(
        "运行方式",
        ["在线采集", "复用本地缓存", "内置示例数据（无需凭证）"],
        horizontal=True,
        label_visibility="collapsed",
    )
    mode = {"在线采集": "live", "复用本地缓存": "cache", "内置示例数据（无需凭证）": "demo"}[mode_label]
    if mode == "demo":
        st.caption("使用 tests/fixtures 里的示例数据跑通全流程，不访问 Reddit 与大模型，用来先看报告长什么样。")
    elif mode == "cache":
        cached, cached_at = _cache_stats()
        if cached:
            st.caption(f"已检测到本地缓存 {cached} 篇帖子（{cached_at}），将跳过采集直接分析。")
        else:
            st.warning("本地还没有缓存数据，请先用「在线采集」跑一次。")

    st.markdown("#### 2. 抓取范围")
    sub_col, sub_extra_col = st.columns([1, 1])
    with sub_col:
        picked = st.multiselect(
            "目标板块",
            options=list(PRESET_SUBREDDITS),
            default=["SaaS", "SideProject"],
            format_func=lambda name: f"r/{name}",
            help="预置的都是工具选型 / 独立开发者 / 效率工具类社区，赛道画像见 README 的预置列表。",
        )
    with sub_extra_col:
        extra_subs = st.text_input(
            "自定义板块（逗号分隔）",
            placeholder="例如：obsidianmd, BearApp",
            help="支持中文逗号；带不带 r/ 前缀都可以。",
        )

    picked_kw = st.multiselect(
        "信号词",
        options=list(PRESET_KEYWORDS),
        default=list(PRESET_KEYWORDS)[:4],
        format_func=lambda word: f'"{word}"',
        help="按信号强度排序：wish there was / would pay for / alternative to 命中率最高。"
        "选 4-8 个即可，太多会稀释样本。",
    )
    kw_col, tip_col = st.columns([1, 1.4])
    with kw_col:
        extra_kw = st.text_input(
            "自定义信号词（逗号分隔）",
            placeholder="例如：i would pay, no way to",
        )
    with tip_col:
        st.caption(
            "信号词决定抓到的是「有情绪的需求」还是「泛泛讨论」。"
            "抱怨句式越具体越好，例如 no way to / deal breaker。"
        )

    subreddits = list(dict.fromkeys(picked + parse_list(extra_subs)))
    keywords = list(dict.fromkeys(picked_kw + parse_list(extra_kw)))

    st.markdown("#### 3. 采样参数")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        days = st.slider("时间范围（天）", 7, 730, 365, step=1)
    with c2:
        limit = st.slider("每板块帖子数", 20, 1000, 200, step=20)
    with c3:
        comments = st.slider("每帖高赞评论", 0, 20, 8, step=1)
    with c4:
        collect_label = st.selectbox("采集模式", list(COLLECT_LABELS), index=0)

    collect_mode = COLLECT_LABELS[collect_label]
    estimated = len(subreddits) * limit
    st.caption(
        f"预计扫描约 {estimated} 帖 ≈ {estimated * 1.2:.0f} 次接口请求；"
        f"若模型分析 {min(conn['analyze_limit'], estimated)} 帖，"
        f"约 {estimated / max(conn['batch_size'], 1):.0f} 次模型调用。"
        "第一次跑建议先用 50-100 帖验证链路。"
    )

    st.divider()
    run_col, hint_col = st.columns([1, 2])
    with run_col:
        start = st.button("开始分析", type="primary", width="stretch")
    with hint_col:
        st.markdown(
            '<div class="rb-note">运行期间会实时显示采集 / 分析进度，'
            "中断后已抓的数据仍在 data/ 里，下次可用「复用本地缓存」继续。</div>",
            unsafe_allow_html=True,
        )

    if not start:
        if st.session_state["result"] is not None:
            st.success("已有分析结果，切到「② 分析看板」查看。")
        return

    params = {
        "subreddits": subreddits,
        "keywords": keywords,
        "days": days,
        "limit": limit,
        "comments": comments,
        "collect_mode": collect_mode,
    }
    settings = _build_settings(conn, params, mode)

    if mode == "live" and not settings.has_reddit_credentials:
        st.error("在线采集需要先填 Reddit Client ID / Secret / User-Agent（左侧边栏）。")
        return
    if mode == "cache" and not settings.raw_data_path.is_file():
        st.error("没有找到 data/raw_data.json，请先在线采集一次。")
        return
    if not settings.subreddits:
        st.error("至少选择一个目标板块。")
        return
    if settings.use_llm and not settings.has_llm_credentials:
        st.error("已勾选 AI 分析但缺少 API Key：填 Key，或关掉「启用 AI 语义分析」走规则模式。")
        return
    if settings.collect_mode in {"search", "both"} and not settings.keywords:
        st.error("关键词搜索模式至少需要一个信号词。")
        return

    try:
        settings.validate()
    except ConfigError as exc:
        st.error(str(exc))
        return

    if "your_name" in settings.reddit_user_agent and mode == "live":
        st.warning(
            "User-Agent 里还是 your_name 占位符。Reddit 可能因此返回 403，"
            "建议改成 script:reddit-backlog-agent:v1.0 (by u/你的用户名)。已继续执行。"
        )

    _execute(settings, mode)


def _execute(settings: Settings, mode: str) -> None:
    log_sink: list[str] = []
    handler = _LogCapture(log_sink)
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    bar = st.progress(0.0, text="准备中…")
    started = time.perf_counter()

    try:
        if mode == "demo":
            if not DEMO_FIXTURE.is_file():
                raise FileNotFoundError(f"示例数据缺失：{DEMO_FIXTURE}")
            payload = json.loads(DEMO_FIXTURE.read_text(encoding="utf-8"))
            meta = payload.get("meta") or {}
            if meta.get("subreddits"):
                settings.subreddits = list(meta["subreddits"])
            if meta.get("keywords"):
                settings.keywords = list(meta["keywords"])
            bar.progress(0.42, text="已载入示例数据")
        elif mode == "cache":
            payload = scraper.load_posts(settings)
            bar.progress(0.42, text=f"已载入缓存 {len(payload.get('posts', []))} 帖")
        else:
            payload = scraper.collect(
                settings,
                progress=lambda msg, frac: bar.progress(
                    min(0.42, 0.42 * float(frac)), text=f"采集 · {msg}"
                ),
            )

        posts = payload.get("posts") or []
        if not posts:
            raise AnalyzerError(
                "没有可分析的帖子。请检查板块拼写、放宽时间范围，或降低评论赞数门槛。"
            )

        result = analyze(
            payload,
            settings,
            progress=lambda msg, frac: bar.progress(
                min(0.93, 0.45 + 0.48 * float(frac)), text=f"分析 · {msg}"
            ),
        )

        bar.progress(0.95, text="正在生成报告与 Excel…")
        paths = write_reports(result, payload, settings)
        bar.progress(1.0, text="完成")

    except ConfigError as exc:
        bar.empty()
        st.error(f"配置错误：{exc}")
        _render_log(log_sink)
        return
    except FileNotFoundError as exc:
        bar.empty()
        st.error(str(exc))
        _render_log(log_sink)
        return
    except AnalyzerError as exc:
        bar.empty()
        st.error(f"分析失败：{exc}")
        _render_log(log_sink)
        return
    except Exception as exc:  # noqa: BLE001 - 界面必须给出可读的失败原因
        bar.empty()
        st.error(f"运行失败：{type(exc).__name__}: {exc}")
        with st.expander("详细堆栈"):
            st.exception(exc)
        _render_log(log_sink)
        return
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)

    elapsed = time.perf_counter() - started
    st.session_state["result"] = result
    st.session_state["payload"] = payload
    st.session_state["paths"] = paths
    st.session_state["run_log"] = list(log_sink)
    st.toast(f"完成：{len(result.items)} 条功能愿望，用时 {elapsed:.1f}s")

    _goto(NAV_ITEMS[1])


def _render_log(log_sink: list[str]) -> None:
    if log_sink:
        with st.expander("运行日志", expanded=True):
            st.code("\n".join(log_sink[-80:]), language="log")


# --------------------------------------------------------------------------- #
# 页面二：分析看板
# --------------------------------------------------------------------------- #


def _page_dashboard() -> None:
    result = st.session_state["result"]
    payload = st.session_state["payload"]

    if result is None:
        st.info("还没有结果。先到「① 配置任务」跑一次；没有凭证可以先选「内置示例数据」看效果。")
        if st.button("去配置任务"):
            _goto(NAV_ITEMS[0])
        return

    posts = payload.get("posts") or []
    comments = sum(len(p.get("top_comments") or []) for p in posts)
    items = result.items
    counts = {label: sum(1 for it in items if priority_of(it.demand_score) == label) for label in PRIORITY_COLORS}

    cols = st.columns(6)
    cols[0].metric("帖子样本", f"{len(posts)}", border=True)
    cols[1].metric("高赞评论", f"{comments}", border=True)
    cols[2].metric("功能愿望", f"{len(items)}", border=True)
    cols[3].metric("P0", counts["P0"], border=True, help=PRIORITY_HINT["P0"])
    cols[4].metric("P1", counts["P1"], border=True, help=PRIORITY_HINT["P1"])
    cols[5].metric(
        "分析方法",
        "大模型" if result.method == "llm" else "规则",
        border=True,
        help="规则模式只做关键词归类，结论仅供结构参考。",
    )

    if result.degraded:
        st.warning("本次 AI 调用中途失败并降级为规则模式，结论仅供参考，建议检查模型配置后重跑。")
    elif result.method != "llm":
        st.info("当前为规则聚类结果，只用于验证流程与报告结构；配置大模型后重新分析才能得到语义级归类。")

    st.markdown("#### 执行摘要")
    with st.container(border=True):
        st.markdown(result.summary or "_（本次未生成摘要）_")

    df = build_dataframe(result)
    if df.empty:
        st.warning("结果为空。")
        return

    st.markdown("#### 机会分布")
    chart_left, chart_right = st.columns([1.15, 1])

    with chart_left:
        fig = go.Figure()
        for label in PRIORITY_COLORS:
            subset = df[df["Priority"] == label]
            if subset.empty:
                continue
            offsets = [0.0, 0.18, -0.18, 0.36, -0.36, 0.54, -0.54]
            slots: dict[tuple[float, float], int] = {}
            xs: list[float] = []
            for evidence, score in zip(subset["Evidence Posts"], subset["Demand Score"]):
                key = (float(evidence), round(float(score), 1))
                slot = slots.get(key, 0)
                slots[key] = slot + 1
                xs.append(float(evidence) + offsets[min(slot, len(offsets) - 1)])
            fig.add_trace(
                go.Scatter(
                    x=xs,
                    y=subset["Demand Score"],
                    mode="markers",
                    name=f"{label}（{len(subset)} 条）",
                    marker=dict(
                        size=14,
                        color=PRIORITY_COLORS[label],
                        line=dict(width=1.2, color="rgba(255,255,255,.85)"),
                        opacity=0.92,
                    ),
                    customdata=subset[["Feature", "Category", "Evidence Posts"]].to_numpy(),
                    hovertemplate="<b>%{customdata[0]}</b><br>"
                    "类别：%{customdata[1]}<br>"
                    "强度：%{y:.1f}<br>证据帖数：%{customdata[2]}<extra></extra>",
                )
            )
        fig.add_hline(
            y=8,
            line_dash="dot",
            line_color="rgba(128,128,128,.55)",
            annotation_text="P0 门槛 8.0",
            annotation_position="top left",
            annotation_font_size=11,
        )
        min_ev = float(df["Evidence Posts"].min())
        max_ev = float(df["Evidence Posts"].max())
        fig.update_layout(
            template="streamlit",
            height=400,
            margin=dict(l=10, r=16, t=16, b=10),
            xaxis=dict(
                title="证据帖数",
                range=[min_ev - 1.0, max_ev + 1.0],
                dtick=1,
                zeroline=False,
            ),
            yaxis=dict(title="需求强度（0-10）", range=[3.5, 10.3]),
            legend=dict(
                orientation="h",
                yanchor="top",
                y=-0.18,
                x=0,
                font=dict(size=11),
            ),
        )
        st.plotly_chart(fig, width="stretch")
        st.caption(
            "优先级矩阵：横轴是证据帖数（有多少帖子在说这件事），纵轴是需求强度，右上角是优先验证方向。"
            "同一坐标上的多条愿望做了微小横向错位，避免点重叠。"
        )

    with chart_right:
        grouped = (
            df.groupby("Category", as_index=False)
            .agg(条数=("Feature", "count"), 平均强度=("Demand Score", "mean"))
            .sort_values("平均强度")
        )
        bar = go.Figure(
            go.Bar(
                x=grouped["平均强度"],
                y=grouped["Category"],
                orientation="h",
                marker=dict(
                    color=grouped["平均强度"],
                    colorscale=[[0, "#c7d8f5"], [1, "#1d4ed8"]],
                    line=dict(width=0),
                ),
                text=[f"{row.条数} 条 · {row.平均强度:.1f}" for row in grouped.itertuples()],
                textposition="outside",
                cliponaxis=False,
                hovertemplate="%{y}<br>平均强度 %{x:.1f}<extra></extra>",
            )
        )
        bar.update_layout(
            template="streamlit",
            height=400,
            margin=dict(l=10, r=80, t=16, b=10),
            xaxis=dict(
                title="平均需求强度",
                range=[0, max(10.0, float(grouped["平均强度"].max()) * 1.3)],
            ),
            yaxis=dict(title=""),
        )
        st.plotly_chart(bar, width="stretch")
        st.caption(
            "痛点类别分布：条数越多说明抱怨越集中，平均强度越高说明越痛。"
            "类别由模型归类，跨类别比较只看相对强弱。"
        )

    st.markdown("#### 功能愿望清单")
    f1, f2, f3, f4 = st.columns([1, 1, 1.2, 1])
    with f1:
        prio_filter = st.multiselect(
            "优先级", list(PRIORITY_COLORS), default=list(PRIORITY_COLORS), key="f_prio"
        )
    with f2:
        cat_options = sorted(df["Category"].unique().tolist())
        cat_filter = st.multiselect("类别", cat_options, default=cat_options, key="f_cat")
    with f3:
        keyword_filter = st.text_input("搜索功能名 / 痛点", placeholder="输入关键词过滤")
    with f4:
        show_all = st.checkbox("显示全部列", help="展开意图、情绪、来源板块、代表原话与原帖链接")

    view = df[df["Priority"].isin(prio_filter) & df["Category"].isin(cat_filter)]
    if keyword_filter.strip():
        needle = keyword_filter.strip().lower()
        mask = (
            view["Feature"].str.lower().str.contains(needle, regex=False)
            | view["Pain Point"].str.lower().str.contains(needle, regex=False)
        )
        view = view[mask]

    compact_columns = [
        "Rank",
        "Feature",
        "Priority",
        "Demand Score",
        "Evidence Posts",
        "Pain Point",
    ]
    full_columns = [
        "Rank",
        "Feature",
        "Priority",
        "Demand Score",
        "Category",
        "Evidence Posts",
        "Pain Point",
        "Intent",
        "Sentiment",
        "Subreddits",
        "Top Quote",
        "Sample Link",
    ]
    if show_all:
        view = view[full_columns]
    else:
        # 紧凑视图只放关键列：痛点截断到 26 字，避免把表格撑出横向滚动
        view = view.copy()
        view["Pain Point"] = [
            text if len(text) <= 26 else text[:26].rstrip() + "…" for text in view["Pain Point"]
        ]
        view = view[compact_columns]

    st.caption(f"共 {len(view)} 条（总计 {len(df)} 条）")
    st.dataframe(
        view,
        width="stretch",
        hide_index=True,
        height=min(560, 46 + 36 * max(1, len(view))),
        column_config={
            "Rank": st.column_config.NumberColumn("#", width="small"),
            "Feature": st.column_config.TextColumn("功能愿望", width="medium"),
            "Priority": st.column_config.TextColumn("优先级", width="small"),
            "Demand Score": st.column_config.ProgressColumn(
                "需求强度", min_value=0, max_value=10, format="%.1f"
            ),
            "Category": st.column_config.TextColumn("类别", width="small"),
            "Intent": st.column_config.TextColumn("意图", width="small"),
            "Sentiment": st.column_config.TextColumn("情绪", width="small"),
            "Evidence Posts": st.column_config.NumberColumn("证据帖数", width="small"),
            "Subreddits": st.column_config.TextColumn("来源板块", width="small"),
            "Pain Point": st.column_config.TextColumn("核心痛点", width="large"),
            "Top Quote": st.column_config.TextColumn("代表原话", width="large"),
            "Sample Link": st.column_config.LinkColumn("原帖", display_text="打开"),
        },
    )

    st.markdown("#### 用户原话")
    st.caption("这些句子都逐字回查过原始帖子/评论，可直接用于落地页文案与访谈提纲。")
    names = [f"{it.feature_name}（{it.demand_score:.1f}）" for it in items]
    picked = st.selectbox("选择一条愿望", names, key="quote_pick")
    item = items[names.index(picked)]

    st.markdown(f"**{html.escape(item.feature_name)}** — {html.escape(item.pain_description)}")
    meta_line = (
        f"类别 {CATEGORY_LABELS.get(item.category, item.category)} · "
        f"证据 {item.evidence_count} 帖 · 来源 {', '.join('r/' + s for s in item.subreddits[:6]) or '—'}"
    )
    st.caption(meta_line)

    if not item.quotes:
        st.markdown("_该条没有通过校验的原话引用。_")
    for quote in item.quotes:
        text = html.escape(str(quote.get("quote", "")))
        sub = quote.get("subreddit", "")
        score = quote.get("score", 0)
        link = quote.get("permalink", "")
        footer = f"r/{html.escape(sub)} · {score} upvotes" if sub else f"{score} upvotes"
        if link:
            footer += f' · <a href="{html.escape(link)}" target="_blank">查看原帖</a>'
        st.markdown(
            f'<div class="rb-quote">“{text}”<br><small>{footer}</small></div>',
            unsafe_allow_html=True,
        )

    with st.expander("运行日志"):
        log_lines = st.session_state.get("run_log") or []
        st.code("\n".join(log_lines[-120:]) if log_lines else "（本次没有日志）", language="log")


# --------------------------------------------------------------------------- #
# 页面三：报告与导出
# --------------------------------------------------------------------------- #


def _timestamp_of(path: Path) -> str:
    """从 reddit_backlog_20261109_0011_report.md 这样的文件名里取出可读时间。"""
    parts = path.stem.split("_")
    for index, part in enumerate(parts):
        if len(part) == 8 and part.isdigit() and index + 1 < len(parts) and parts[index + 1].isdigit():
            stamp = part + parts[index + 1]
            return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[8:10]}:{stamp[10:12]}"
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")


def _page_downloads() -> None:
    paths: dict[str, Path] = st.session_state.get("paths") or {}

    if not paths:
        st.info("还没有生成报告。先到「① 配置任务」跑一次。")
    else:
        result = st.session_state["result"]
        st.markdown("#### 本次产物")
        st.caption(f"共 {len(result.items)} 条功能愿望，四个格式内容一致，按使用场景选一个即可。")

        spec = [
            ("markdown", "Markdown 报告", "给团队看 / 贴进 Notion", "text/markdown", "下载 .md"),
            (
                "excel",
                "Excel 工作簿",
                "排序筛选 / 汇报",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "下载 .xlsx",
            ),
            ("csv", "CSV 表格", "导入 BI / 表格工具", "text/csv", "下载 .csv"),
            ("json", "JSON 快照", "二次开发 / 归档", "application/json", "下载 .json"),
        ]
        columns = st.columns(len(spec))
        for column, (key, label, purpose, mime, button_label) in zip(columns, spec):
            path = paths.get(key)
            with column:
                with st.container(border=True):
                    st.markdown(f"**{label}**")
                    st.caption(purpose)
                    if not path or not Path(path).is_file():
                        st.caption("未生成")
                        continue
                    file_path = Path(path)
                    size_kb = file_path.stat().st_size / 1024
                    st.caption(f"{_timestamp_of(file_path)} 生成 · {size_kb:.1f} KB")
                    st.download_button(
                        button_label,
                        data=file_path.read_bytes(),
                        file_name=file_path.name,
                        mime=mime,
                        width="stretch",
                        key=f"dl_{key}",
                        help=file_path.name,
                    )

        md_path = paths.get("markdown")
        if md_path and Path(md_path).is_file():
            with st.expander("预览报告正文", expanded=False):
                st.markdown(Path(md_path).read_text(encoding="utf-8"))

    st.divider()
    st.markdown("#### 历史运行")
    reports = sorted((PROJECT_ROOT / "reports").glob("*_report.md"), reverse=True)
    if not reports:
        st.caption("reports/ 下还没有报告。")
        return

    rows = []
    for item in reports[:20]:
        stat = item.stat()
        rows.append(
            {
                "生成时间": _timestamp_of(item),
                "报告文件": item.name,
                "大小(KB)": round(stat.st_size / 1024, 1),
            }
        )
    st.dataframe(
        pd.DataFrame(rows),
        width="stretch",
        hide_index=True,
        height=min(420, 46 + 36 * len(rows)),
        column_config={
            "生成时间": st.column_config.TextColumn("生成时间", width="small"),
            "报告文件": st.column_config.TextColumn("报告文件", width="large"),
            "大小(KB)": st.column_config.NumberColumn("大小(KB)", width="small", format="%.1f"),
        },
    )
    st.caption("历史报告文件都在项目 reports/ 目录下，可直接用系统文件管理器打开。")


# --------------------------------------------------------------------------- #
# 页面四：使用说明
# --------------------------------------------------------------------------- #


def _page_help() -> None:
    st.markdown("#### 需要 OAuth 吗？")
    st.markdown(
        """
        **只读采集不需要用户 OAuth 授权。** 本工具做的事情是「读公开帖子」，不涉及代你发帖或点赞，
        所以既不需要账号密码，也不需要浏览器里点「允许」的跳转授权。

        你需要做的只有一件事：在 Reddit 后台**创建一个应用**，拿到三样东西：

        | 需要的东西 | 从哪来 | 说明 |
        |---|---|---|
        | `client_id` | 应用名称下方那串短字符 | 相当于应用账号 |
        | `client_secret` | 标着 secret 的长串 | 相当于密码 |
        | `user_agent` | 自己起 | 建议 `script:reddit-backlog-agent:v1.0 (by u/你的用户名)` |

        PRAW 拿这三个值在后台自动完成 application-only OAuth（client_credentials 流程），
        你不需要写任何授权回调代码。

        **什么情况才需要完整 OAuth？** 只有当你希望以某个账号身份**发帖、评论、点赞、删帖**时，
        才需要 username + password 或 refresh token 的 user-context 授权。本项目的定位是调研工具，
        刻意只做只读，避免账号风险。
        """
    )

    st.markdown("#### 三步拿到凭证")
    st.markdown(
        """
        1. 用浏览器登录 Reddit，打开 <https://www.reddit.com/prefs/apps>，点最下方 **create another app**。
        2. 填 `name`（随意）、选类型 **script**、`redirect uri` 填 `http://localhost:8080`
           （只读模式用不到，但表单必填）。点 **create app**。
        3. 回到列表页，在刚建的应用名下抄下 `client_id` 与 `secret`，填到左侧边栏，
           点「保存到 .env」。
        """
    )
    st.info(
        "Reddit 对 API 有频率限制（OAuth 客户端约 100 请求/分钟）。本工具单帖评论只取 Top N、"
        "不展开深层子线程，正常使用不会触顶；若出现 429，把侧边栏「请求间隔」调到 0.5-1 秒。"
    )

    st.markdown("#### 参数怎么选")
    st.markdown(
        """
        | 参数 | 建议 | 原因 |
        |---|---|---|
        | 板块 | 3-6 个相邻赛道 | 太杂会让聚类失焦，太窄样本不足 |
        | 帖子数 | 首次 50-100，验证后 300-500 | 决定请求量与成本 |
        | 信号词 | 6-10 个抱怨句式 | 抓的是「有情绪的需求」，不是泛泛讨论 |
        | 时间范围 | 365 天 | 更长的数据会混入已过时的产品语境 |
        | 最多分析帖数 | 100-300 | 直接决定 token 花费 |
        """
    )

    st.markdown("#### 常见问题")
    with st.expander("报错 401 / 403 怎么办？"):
        st.markdown(
            "多为凭证错位：`client_id` 与应用名混淆、`secret` 复制不全，或 `user_agent` 用了默认占位符。"
            "重新从 reddit.com/prefs/apps 复制一次即可。"
        )
    with st.expander("为什么评论只取了几条？"):
        st.markdown(
            "为控制请求量，只取每帖按赞数排序的前 N 条，且不展开 `load more` 深层子线程。"
            "深层评论里确实还有信号，但成本会成倍上升，取舍写在报告「方法与局限」里。"
        )
    with st.expander("结果里的需求强度能当市场规模用吗？"):
        st.markdown(
            "不能。它是**相对排序指标**：语义强度 × 0.65 + 证据强度 × 0.35，只回答"
            "「先做哪个」，不回答「能赚多少」。立项前还需要访谈、竞品定价和搜索量验证。"
        )
    with st.expander("不想花钱调模型，能离线跑吗？"):
        st.markdown(
            "可以。左侧关掉「启用 AI 语义分析」即走规则聚类，流程与报告结构完全一致，"
            "但归类是按关键词硬匹配，报告会明确标注为降级版本。"
        )
    with st.expander("命令行还能用吗？"):
        st.markdown(
            "能，界面与命令行共用同一套模块，数据与报告目录也相同：\n\n"
            "```bash\npython main.py --demo\npython main.py --subreddits SaaS,Notion --limit 300\n"
            "python main.py --from-cache --no-llm\n```"
        )


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def main() -> None:
    st.set_page_config(
        page_title="Reddit 需求挖掘 Agent",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    _inject_css()
    _init_state()
    load_dotenv(ENV_FILE)

    conn = _render_sidebar()
    _render_header(conn)

    target = st.session_state.pop("_nav_target", None)
    if target in NAV_ITEMS:
        st.session_state["nav_index"] = target

    nav = st.segmented_control(
        "导航",
        NAV_ITEMS,
        key="nav_index",
        required=True,
        label_visibility="collapsed",
    )
    nav = nav or NAV_ITEMS[0]

    if nav == NAV_ITEMS[0]:
        _page_config(conn)
    elif nav == NAV_ITEMS[1]:
        _page_dashboard()
    elif nav == NAV_ITEMS[2]:
        _page_downloads()
    else:
        _page_help()


if __name__ == "__main__":
    main()
