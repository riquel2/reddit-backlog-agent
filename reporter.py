"""reporter.py — 把结构化分析结果渲染成可交付的报告。

产出
----
- ``*_report.md``   ：给人和 AI 阅读的主报告
- ``*_backlog.xlsx``：给团队排期用的多 Sheet 工作簿
- ``*_backlog.csv`` ：给 BI / 表格工具导入
- ``*_analysis.json``：结构化快照，便于二次处理或对比历史
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl.formatting.rule import CellIsRule, DataBarRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from analyzer import AnalysisResult, BacklogItem
from config import CATEGORY_LABELS, Settings

logger = logging.getLogger(__name__)

INTENT_LABELS = {
    "venting": "纯吐槽",
    "question": "求方案",
    "feature_request": "明确要功能",
    "willing_to_pay": "愿意付费",
    "switching": "准备换工具",
}

SENTIMENT_LABELS = {
    "angry": "愤怒",
    "frustrated": "挫败",
    "neutral": "中性",
    "hopeful": "期待",
}

PRIORITY_META = {
    "P0": ("立刻做", "需求强度 ≥ 8，MVP 必须覆盖"),
    "P1": ("下一个迭代", "强度 6.5-8，有明确场景与复现"),
    "P2": ("持续观察", "强度 4.5-6.5，信号分散"),
    "P3": ("暂不投入", "强度 < 4.5，缺乏支撑证据"),
}

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def priority_of(score: float) -> str:
    if score >= 8.0:
        return "P0"
    if score >= 6.5:
        return "P1"
    if score >= 4.5:
        return "P2"
    return "P3"


def _safe(text: Any, limit: int | None = None) -> str:
    """清理控制字符与多余空白，防止 Excel / Markdown 渲染异常。"""
    value = "" if text is None else str(text)
    value = _CONTROL_CHARS.sub("", value)
    value = re.sub(r"\s+", " ", value).strip()
    if limit is not None and len(value) > limit:
        cut = value[:limit]
        space = cut.rfind(" ")
        if space > limit * 0.6:
            cut = cut[:space]
        value = cut.rstrip(" ,.;:，。；：") + "…"
    return value


def _md(text: Any, limit: int | None = None) -> str:
    """Markdown 表格单元格转义。"""
    return _safe(text, limit).replace("|", "\\|")


def _quote_md(quote: dict[str, Any]) -> str:
    body = _safe(quote.get("quote"), 500)
    subreddit = _safe(quote.get("subreddit"))
    score = quote.get("score", 0)
    link = _safe(quote.get("permalink"))
    meta = f"r/{subreddit} · 帖子 {score} 赞" if subreddit else f"帖子 {score} 赞"
    if link:
        return f'> "{body}"\n> — [{meta}]({link})'
    return f'> "{body}"\n> — {meta}'


def _top_items_table(items: list[BacklogItem], limit: int = 20) -> str:
    rows = [
        "| # | 功能愿望 | 优先级 | 强度 | 类别 | 证据帖数 | 核心痛点 |",
        "|---|---|---|---|---|---|---|",
    ]
    for index, item in enumerate(items[:limit], start=1):
        rows.append(
            f"| {index} | **{_md(item.feature_name)}** | {priority_of(item.demand_score)} "
            f"| {item.demand_score:.1f} | {CATEGORY_LABELS.get(item.category, item.category)} "
            f"| {item.evidence_count} | {_md(item.pain_description, 60)} |"
        )
    return "\n".join(rows)


def _distribution_table(items: list[BacklogItem]) -> str:
    counts: dict[str, list[float]] = {}
    for item in items:
        counts.setdefault(item.category, []).append(item.demand_score)
    if not counts:
        return "_无数据_"

    ordered = sorted(counts.items(), key=lambda kv: (len(kv[1]), sum(kv[1])), reverse=True)
    averages = {category: sum(scores) / len(scores) for category, scores in counts.items()}
    peak = max(averages.values()) or 1.0
    rows = ["| 类别 | 条目数 | 平均强度 | 强度分布 |", "|---|---|---|---|"]
    for category, scores in ordered:
        bar = "█" * max(1, round(averages[category] / peak * 12))
        rows.append(
            f"| {CATEGORY_LABELS.get(category, category)} | {len(scores)} "
            f"| {averages[category]:.1f} | {bar} |"
        )
    return "\n".join(rows)


def _priority_summary(items: list[BacklogItem]) -> str:
    groups: dict[str, list[BacklogItem]] = {"P0": [], "P1": [], "P2": [], "P3": []}
    for item in items:
        groups[priority_of(item.demand_score)].append(item)

    rows = ["| 优先级 | 定义 | 条目数 | 代表功能 | 建议动作 |", "|---|---|---|---|---|"]
    for key in ("P0", "P1", "P2", "P3"):
        bucket = groups[key]
        names = "、".join(_md(it.feature_name) for it in bucket[:3]) or "—"
        action, rule = PRIORITY_META[key]
        rows.append(f"| **{key}** | {rule} | {len(bucket)} | {names} | {action} |")
    return "\n".join(rows)


# --------------------------------------------------------------------------- #
# DataFrame
# --------------------------------------------------------------------------- #


def build_dataframe(result: AnalysisResult) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for rank, item in enumerate(result.items, start=1):
        top_quote = item.quotes[0]["quote"] if item.quotes else ""
        records.append(
            {
                "Rank": rank,
                "Feature": _safe(item.feature_name, 80),
                "Priority": priority_of(item.demand_score),
                "Demand Score": round(item.demand_score, 1),
                "Category": CATEGORY_LABELS.get(item.category, item.category),
                "Intent": INTENT_LABELS.get(item.intent, item.intent),
                "Sentiment": SENTIMENT_LABELS.get(item.sentiment, item.sentiment),
                "Evidence Posts": item.evidence_count,
                "Subreddits": ", ".join(f"r/{s}" for s in item.subreddits[:6]),
                "Pain Point": _safe(item.pain_description, 200),
                "Top Quote": _safe(top_quote, 400),
                "Sample Link": _safe(item.quotes[0].get("permalink", "")) if item.quotes else "",
            }
        )
    return pd.DataFrame.from_records(records)


def build_quotes_dataframe(result: AnalysisResult) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for item in result.items:
        for quote in item.quotes:
            rows.append(
                {
                    "Feature": _safe(item.feature_name, 80),
                    "Priority": priority_of(item.demand_score),
                    "Demand Score": round(item.demand_score, 1),
                    "Quote": _safe(quote.get("quote"), 600),
                    "Subreddit": f"r/{quote['subreddit']}" if quote.get("subreddit") else "",
                    "Upvotes": quote.get("score", 0),
                    "Link": _safe(quote.get("permalink", "")),
                }
            )
    return pd.DataFrame.from_records(rows)


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #


def render_markdown(
    result: AnalysisResult, payload: dict[str, Any], settings: Settings
) -> str:
    meta = {**payload.get("meta", {}), **settings.public_meta()}
    posts = payload.get("posts", [])
    subreddit_text = "、".join(f"r/{s}" for s in meta.get("subreddits", [])) or "多板块"
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    method_note = (
        "大模型语义抽取 + 原文校验"
        if result.method == "llm"
        else "规则关键词归类（未启用大模型，结果为降级版本）"
    )

    intent_counts: dict[str, int] = {}
    for item in result.items:
        intent_counts[item.intent] = intent_counts.get(item.intent, 0) + 1
    pay_signal = intent_counts.get("willing_to_pay", 0) + intent_counts.get("switching", 0)

    parts: list[str] = []
    parts.append(f"# Reddit 赛道需求与愿望清单：{subreddit_text}\n")
    parts.append(
        f"> 生成时间：{generated} ｜ 样本：{len(posts)} 篇帖子 / "
        f"{sum(len(p.get('top_comments') or []) for p in posts)} 条高赞评论 ｜ "
        f"时间范围：近 {meta.get('time_range_days', '?')} 天 ｜ 方法：{method_note} ｜ "
        f"模型：{result.model}\n"
    )

    if result.degraded:
        parts.append(
            "> ⚠️ **本次运行发生降级**：AI 分析不可用，已回退到规则模式，结论仅供结构参考。\n"
        )

    parts.append("## 一、执行摘要\n")
    parts.append(result.summary.strip() or "_模型未返回摘要_")
    parts.append("")

    top3 = result.items[:3]
    if top3:
        parts.append("**最值得先验证的三个方向**\n")
        for index, item in enumerate(top3, start=1):
            parts.append(
                f"{index}. **{item.feature_name}**（{item.demand_score:.1f}/10）— "
                f"{item.pain_description}，涉及 {item.evidence_count} 篇帖子。"
            )
        parts.append("")

    parts.append("## 二、优先级矩阵\n")
    parts.append(_priority_summary(result.items))
    parts.append("")
    parts.append(
        f"共归纳 **{len(result.items)}** 条功能愿望，其中 "
        f"**{pay_signal}** 条带有明确付费意愿或迁移倾向。"
        "强度 = 语义强度 × 0.65 + 证据强度（赞数 / 评论量 / 关键词命中）× 0.35，"
        "是**相对排序指标**，不等于市场规模。\n"
    )

    parts.append("## 三、功能愿望清单（按呼声排序）\n")
    parts.append(_top_items_table(result.items, limit=25))
    if len(result.items) > 25:
        parts.append(f"\n_其余 {len(result.items) - 25} 条见 Excel / CSV 附件。_")
    parts.append("")

    parts.append("## 四、Top 10 需求深挖\n")
    for index, item in enumerate(result.items[:10], start=1):
        parts.append(f"### {index}. {item.feature_name} · {item.demand_score:.1f}/10 · {priority_of(item.demand_score)}\n")
        parts.append(
            f"- **类别**：{CATEGORY_LABELS.get(item.category, item.category)}　"
            f"**用户意图**：{INTENT_LABELS.get(item.intent, item.intent)}　"
            f"**情绪**：{SENTIMENT_LABELS.get(item.sentiment, item.sentiment)}"
        )
        parts.append(f"- **痛点**：{item.pain_description}")
        parts.append(
            f"- **证据**：{item.evidence_count} 篇帖子"
            + (f"（{'、'.join('r/' + s for s in item.subreddits[:5])}）" if item.subreddits else "")
            + (f"　**命中信号词**：{', '.join(item.keywords[:5])}" if item.keywords else "")
        )
        if item.quotes:
            parts.append("- **用户原话**：")
            for quote in item.quotes[:2]:
                parts.append("  " + _quote_md(quote).replace("\n", "\n  "))
        parts.append("")

    parts.append("## 五、用户原话墙\n")
    quotes: list[dict[str, Any]] = []
    for item in result.items:
        for quote in item.quotes:
            quotes.append({**quote, "feature": item.feature_name})
    quotes.sort(key=lambda q: q.get("score", 0), reverse=True)
    if quotes:
        seen: set[str] = set()
        shown = 0
        for quote in quotes:
            fingerprint = _safe(quote.get("quote"), 60).lower()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            parts.append(f"**{_safe(quote.get('feature'))}**")
            parts.append(_quote_md(quote))
            parts.append("")
            shown += 1
            if shown >= 12:
                break
    else:
        parts.append("_未提取到可用原话。_")
        parts.append("")

    parts.append("## 六、赛道痛点分布\n")
    parts.append(_distribution_table(result.items))
    parts.append("")

    parts.append("## 七、方法与局限\n")
    if result.method == "llm":
        model_line = (
            "- **模型波动**：语义归类由大模型完成，同一批数据重跑可能有 ±1 条差异；"
            "所有原话均已回查原文，但仍建议点开链接人工复核语境。\n"
        )
    else:
        model_line = (
            "- **规则模式限制**：本次未启用大模型，按「痛点句式 + 关键词」归类，"
            "同一能力可能被拆到相邻类别（例如「按文件夹共享」落入搜索/组织类），"
            "功能名也是类别模板而非具体能力。请配置 `LLM_API_KEY` 后去掉 `--no-llm` 重跑，"
            "才能得到可用的语义聚类结果。\n"
        )
    parts.append(
        "- **数据来源**：Reddit 公开帖子与评论（PRAW 只读模式），已剔除置顶公告、NSFW 内容与低赞噪声评论。\n"
        "- **抽样偏差**：Reddit 以英语用户与重度用户为主，不能代表整体市场；近 365 天热帖会放大争议性话题。\n"
        "- **评论覆盖**：为控制请求量，未展开 \"load more comments\" 深层子线程（PRAW `replace_more(limit=0)`），"
        "长尾评论可能遗漏。\n"
        + model_line
        + "- **指标含义**：`demand_score` 用于横向排序，不代表付费人数或收入预测；"
        "立项前请补充访谈与竞品定价验证。\n"
        f"- **本次调用统计**：{result.stats.get('llm_calls', 0)} 次模型请求，"
        f"约 {result.stats.get('llm_tokens', 0)} tokens，"
        f"丢弃无法溯源的引用 {result.stats.get('quotes_rejected', 0)} 条。\n"
    )

    parts.append("---\n")
    parts.append(
        "_本报告由 reddit_backlog_agent 自动生成。请遵守 Reddit API 使用条款，"
        "仅用于公开内容的分析与研究，不要用于骚扰、画像或再分发用户内容。_"
    )
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Excel
# --------------------------------------------------------------------------- #

_HEADER_FILL = PatternFill("solid", fgColor="1F3864")
_HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)
_BODY_FONT = Font(size=10)
_THIN = Side(style="thin", color="D9D9D9")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)

_PRIORITY_FILLS = {
    "P0": PatternFill("solid", fgColor="FCE4E4"),
    "P1": PatternFill("solid", fgColor="FFF2CC"),
    "P2": PatternFill("solid", fgColor="E2EFDA"),
    "P3": PatternFill("solid", fgColor="F2F2F2"),
}


def _display_width(text: str) -> int:
    """中文字符按 2 个字符宽度估算，避免 Excel 列宽过窄。"""
    return sum(2 if ord(char) > 0x2E7F else 1 for char in text)


def _write_table(
    writer: pd.ExcelWriter,
    df: pd.DataFrame,
    sheet_name: str,
    widths: dict[str, int] | None = None,
    wrap_columns: tuple[str, ...] = (),
) -> None:
    df.to_excel(writer, sheet_name=sheet_name, index=False)
    worksheet = writer.sheets[sheet_name]

    for column_index, column_name in enumerate(df.columns, start=1):
        cell = worksheet.cell(row=1, column=column_index)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

        if widths and column_name in widths:
            width = widths[column_name]
        else:
            sample = df[column_name].astype(str).head(200).tolist() if not df.empty else []
            longest = max([_display_width(column_name)] + [_display_width(v) for v in sample])
            width = min(60, max(10, longest + 2))
        worksheet.column_dimensions[get_column_letter(column_index)].width = width

    worksheet.row_dimensions[1].height = 26
    worksheet.freeze_panes = "A2"

    wrap_letters = _letters_for(df, wrap_columns)
    for row in worksheet.iter_rows(min_row=2, max_row=worksheet.max_row, max_col=len(df.columns)):
        for cell in row:
            cell.font = _BODY_FONT
            cell.border = _BORDER
            cell.alignment = Alignment(
                vertical="top", wrap_text=cell.column_letter in wrap_letters
            )

    if not df.empty:
        worksheet.auto_filter.ref = worksheet.dimensions

    score_column = "Demand Score"
    if score_column in df.columns and not df.empty:
        letter = get_column_letter(list(df.columns).index(score_column) + 1)
        worksheet.conditional_formatting.add(
            f"{letter}2:{letter}{worksheet.max_row}",
            DataBarRule(start_type="num", start_value=0, end_type="num", end_value=10, color="5B9BD5"),
        )

    if "Priority" in df.columns and not df.empty:
        letter = get_column_letter(list(df.columns).index("Priority") + 1)
        for priority, fill in _PRIORITY_FILLS.items():
            worksheet.conditional_formatting.add(
                f"{letter}2:{letter}{worksheet.max_row}",
                CellIsRule(operator="equal", formula=[f'"{priority}"'], fill=fill),
            )


def _letters_for(df: pd.DataFrame, columns: tuple[str, ...]) -> set[str]:
    return {
        get_column_letter(index + 1)
        for index, name in enumerate(df.columns)
        if name in columns
    }


def write_excel(path: Path, result: AnalysisResult, payload: dict[str, Any]) -> None:
    backlog = build_dataframe(result)
    quotes = build_quotes_dataframe(result)
    meta = payload.get("meta", {})
    posts = payload.get("posts", [])

    overview_rows = [
        ("指标", "数值"),
        ("生成时间", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("目标板块", "、".join(f"r/{s}" for s in meta.get("subreddits", []))),
        ("时间范围（天）", meta.get("time_range_days", "")),
        ("采集帖子数", len(posts)),
        ("评论样本数", sum(len(p.get("top_comments") or []) for p in posts)),
        ("功能愿望条目", len(result.items)),
        ("P0 条目", sum(1 for item in result.items if priority_of(item.demand_score) == "P0")),
        ("分析方法", "大模型语义抽取" if result.method == "llm" else "规则关键词归类"),
        ("使用模型", result.model),
        ("模型调用次数", result.stats.get("llm_calls", 0)),
        ("消耗 tokens", result.stats.get("llm_tokens", 0)),
        ("被丢弃的不可溯源引用", result.stats.get("quotes_rejected", 0)),
    ]
    overview = pd.DataFrame(overview_rows[1:], columns=list(overview_rows[0]))
    summary_df = pd.DataFrame({"执行摘要": result.summary.split("\n")})

    signals = pd.DataFrame.from_records(
        [
            {
                "Post ID": p.get("id", ""),
                "Subreddit": f"r/{p.get('subreddit', '')}",
                "Title": _safe(p.get("title"), 200),
                "Score": p.get("score", 0),
                "Comments": p.get("num_comments", 0),
                "Matched Keywords": ", ".join(p.get("matched_keywords") or []),
                "Captured Comments": len(p.get("top_comments") or []),
                "Created": (p.get("created_iso") or "")[:10],
                "Link": p.get("permalink", ""),
            }
            for p in posts[:2000]
        ]
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        _write_table(writer, overview, "Overview", widths={"指标": 26, "数值": 60}, wrap_columns=("数值",))
        _write_table(writer, summary_df, "Summary", widths={"执行摘要": 90}, wrap_columns=("执行摘要",))
        _write_table(
            writer,
            backlog,
            "Backlog",
            widths={
                "Rank": 6,
                "Feature": 34,
                "Priority": 10,
                "Demand Score": 13,
                "Category": 20,
                "Intent": 14,
                "Sentiment": 10,
                "Evidence Posts": 14,
                "Subreddits": 26,
                "Pain Point": 46,
                "Top Quote": 60,
                "Sample Link": 32,
            },
            wrap_columns=("Feature", "Pain Point", "Top Quote", "Subreddits"),
        )
        if not quotes.empty:
            _write_table(
                writer,
                quotes,
                "Quotes",
                widths={
                    "Feature": 30,
                    "Priority": 10,
                    "Demand Score": 13,
                    "Quote": 80,
                    "Subreddit": 20,
                    "Upvotes": 10,
                    "Link": 40,
                },
                wrap_columns=("Quote", "Feature"),
            )
        if not signals.empty:
            _write_table(
                writer,
                signals,
                "Raw Signals",
                widths={
                    "Post ID": 10,
                    "Subreddit": 22,
                    "Title": 70,
                    "Score": 9,
                    "Comments": 10,
                    "Matched Keywords": 30,
                    "Captured Comments": 16,
                    "Created": 12,
                    "Link": 40,
                },
                wrap_columns=("Title",),
            )


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #


def write_reports(
    result: AnalysisResult, payload: dict[str, Any], settings: Settings
) -> dict[str, Path]:
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    base = settings.output_dir / f"reddit_backlog_{stamp}"

    markdown_path = base.with_name(base.name + "_report.md")
    excel_path = base.with_name(base.name + "_backlog.xlsx")
    csv_path = base.with_name(base.name + "_backlog.csv")
    json_path = base.with_name(base.name + "_analysis.json")

    markdown_path.write_text(render_markdown(result, payload, settings), encoding="utf-8")

    dataframe = build_dataframe(result)
    dataframe.to_csv(csv_path, index=False, encoding="utf-8-sig")

    payload_json = {
        "generated_at": datetime.now().isoformat(),
        "meta": {**payload.get("meta", {}), **settings.public_meta()},
        "analysis": result.to_dict(),
    }
    json_path.write_text(
        json.dumps(payload_json, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    paths = {"markdown": markdown_path, "csv": csv_path, "json": json_path}
    try:
        write_excel(excel_path, result, payload)
        paths["excel"] = excel_path
    except Exception as exc:  # noqa: BLE001 - Excel 失败不应让已有产物白跑
        logger.error("Excel 导出失败（Markdown / CSV 已生成）：%s", exc)
    return paths
