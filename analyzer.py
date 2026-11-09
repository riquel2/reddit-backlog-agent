"""analyzer.py — AI 语义分析、痛点聚类与需求打分。

流程
----
1. 候选排序：按「关键词命中数 → 热度」挑选，控制分析成本。
2. 分批抽取：每批若干帖子，要求模型输出严格 JSON（feature / pain / score / quotes）。
3. 引文校验：逐条比对原文，丢弃模型编造的 quote（可追溯性最重要）。
4. 跨批归并：items 超过阈值时再做一次「合并同义项」调用，失败则本地相似度兜底。
5. 加权打分：LLM 语义分 × 0.65 + 证据强度（热度 + 评论量 + 关键词）× 0.35。

降级
----
无 API Key 时（``--no-llm``）走规则模式：按类别 + 痛点句式做关键词聚类。
它只保证流程可跑、结构完整，不替代真正的语义聚类，报告中会明确标注。
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from typing import Any, Callable, Iterable

from config import CATEGORY_LABELS, VALID_CATEGORIES, PRESET_KEYWORDS, Settings

logger = logging.getLogger(__name__)

MAX_BATCH_CHARS = 24000
MAX_QUOTES_PER_ITEM = 3

VALID_SENTIMENTS = ("angry", "frustrated", "neutral", "hopeful")
VALID_INTENTS = ("venting", "question", "feature_request", "willing_to_pay", "switching")

_CATEGORY_ENUM = ", ".join(VALID_CATEGORIES)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？])\s+|\n+")
_NON_WORD = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


class AnalyzerError(RuntimeError):
    """分析阶段不可恢复的错误。"""


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = f"""你是资深产品经理兼需求分析师，专门从 Reddit 社区讨论中挖掘可落地的产品机会。
你的输出会被程序直接解析为 JSON，任何多余文字都会导致解析失败。

任务：从给定的 Reddit 帖子与评论中，抽取用户表达的「功能缺失 / 体验痛点 / 付费替代意愿」，
归纳为具体的产品功能愿望，并按需求强度打分。

必须遵守的规则：
1. 只使用输入中出现的信息。禁止臆造用户没说过的话、没出现过的场景或数字。
2. 每条结论至少对应 1 条原文引用（verbatim quote）。引用必须逐字复制输入文本，
   不得改写、不得翻译、不得拼接、不得添加省略号。英文原文保持英文。
3. 合并同义项：指向同一产品能力的多条抱怨（例如 "导入 CSV 太慢" 与 "想要批量导入"）
   合并为一条，不要拆成多条。
4. 丢弃：纯情绪宣泄且无具体对象、闲聊、广告、纯技术报错求助、与产品能力无关的内容。
5. 若本批没有有效信号，返回 {{"items": []}}。宁缺毋滥，不要凑数。

字段定义：
- feature_name：英文功能名，2-5 个词，动作或名词短语，首字母大写，不带句号。
  示例："Bulk CSV Import"、"Slack Notification Integration"、"Offline Mobile Editing"
- pain_description：中文，一句话描述用户遇到的具体问题，不超过 40 字，不加主观推测。
- category：只能取以下枚举之一：{_CATEGORY_ENUM}
- sentiment：用户情绪，枚举 angry / frustrated / neutral / hopeful
- intent：用户意图，枚举 venting（纯吐槽）/ question（求方案）/ feature_request（明确要功能）
  / willing_to_pay（表达付费意愿）/ switching（正在或准备换工具）
- demand_score：1-10 的整数，按下列口径打分，不要凭感觉：
    9-10 = 多帖反复出现 + 明确付费意愿 + 点名现有方案「做不到」
    7-8  = 多次出现，情绪强烈，有具体使用场景
    5-6  = 有提及但分散，情绪温和
    3-4  = 偶发提及，缺乏细节
    1-2  = 边缘提及，更像顺口一说
- source_refs：该结论依据的输入编号数组，只能填输入中出现过的编号，如 ["P1","P3"]
- verbatim_quotes：1-2 个对象，每个 {{"ref":"P1","quote":"逐字原文"}}

严格输出（不要 markdown 代码块）：
{{"items":[{{"feature_name":"","pain_description":"","category":"","sentiment":"","intent":"","demand_score":0,"source_refs":["P1"],"verbatim_quotes":[{{"ref":"P1","quote":""}}]}}]}}"""

MERGE_SYSTEM_PROMPT = """你是产品需求去重专家。输入是一批已抽取的功能愿望条目（带编号）。
请把指向同一产品能力的条目合并，输出严格 JSON。

规则：
1. 每个编号必须且只能出现在一个 group 的 member_indices 中，不得遗漏、不得重复。
2. 只有确实是「同一个产品能力」才合并；只是相关、上下游关系或同一类目的不同功能，不要合并。
3. canonical_feature_name 用英文，取最清晰、最通用的一种表述，2-5 个词。
4. pain_description 用中文概括合并后的核心痛点，不超过 40 字。
5. 不得新增输入中不存在的功能。
6. demand_score 为合并后的整体强度，1-10 整数。

输出格式：
{"groups":[{"canonical_feature_name":"","pain_description":"","category":"","member_indices":[0,2],"demand_score":8}]}"""

SUMMARY_SYSTEM_PROMPT = """你是给独立开发者做立项决策的产品顾问。根据给定的功能愿望清单（已按需求强度排序），
写一份中文执行摘要，供 5 分钟内读完。输出严格 JSON：{"summary_markdown":"..."}

要求：
1. 第一行是一句结论：这个赛道当前最值得切入的 1 个机会，以及理由（不超过 60 字）。
2. 随后用 3-5 个要点覆盖：最集中的痛点、用户现在怎么绕过、付费意愿信号、竞争空位/切入角度。
3. 只用输入中出现的信息，不编造数字、不引用不存在的帖子。
4. 使用 Markdown 无序列表，不要标题层级，不要代码块，总长度 300 字以内。"""


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class BacklogItem:
    feature_name: str
    pain_description: str
    category: str = "other"
    sentiment: str = "neutral"
    intent: str = "question"
    llm_demand_score: float = 5.0
    demand_score: float = 5.0
    evidence_count: int = 0
    subreddits: list[str] = field(default_factory=list)
    quotes: list[dict[str, Any]] = field(default_factory=list)
    post_ids: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    verified_quotes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AnalysisResult:
    summary: str
    items: list[BacklogItem]
    method: str
    model: str
    stats: dict[str, Any] = field(default_factory=dict)
    degraded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "method": self.method,
            "model": self.model,
            "stats": self.stats,
            "degraded": self.degraded,
            "items": [item.to_dict() for item in self.items],
        }


# --------------------------------------------------------------------------- #
# JSON 解析（模型输出不可信，必须容错）
# --------------------------------------------------------------------------- #


def extract_json(text: str) -> dict[str, Any]:
    """从模型输出里稳健地取出 JSON 对象。"""
    if not text or not text.strip():
        raise ValueError("模型返回为空")

    raw = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1).strip()

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = raw.find("{")
    if start == -1:
        raise ValueError("模型输出中没有 JSON 对象")

    depth = 0
    in_string = False
    escaped = False
    end = -1
    for index in range(start, len(raw)):
        char = raw[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index
                break

    if end == -1:
        raise ValueError("模型输出的 JSON 括号不闭合")

    candidate = raw[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
        return json.loads(repaired)


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(low, min(high, number))


def _norm_text(text: str) -> str:
    return _NON_WORD.sub("", (text or "").lower())


# --------------------------------------------------------------------------- #
# 大模型客户端
# --------------------------------------------------------------------------- #


class LLMClient:
    """OpenAI 兼容客户端（OpenAI / DeepSeek / Qwen / Ollama 均可）。"""

    def __init__(self, settings: Settings) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise AnalyzerError("缺少 openai 依赖，请运行 pip install -r requirements.txt") from exc

        if not settings.has_llm_credentials:
            raise AnalyzerError("未配置大模型凭证（LLM_API_KEY / LLM_BASE_URL）")

        kwargs: dict[str, Any] = {
            "api_key": settings.llm_api_key or "local-no-key",
            "timeout": settings.llm_timeout,
            "max_retries": 0,  # 重试策略由本模块控制，便于统计与退避
        }
        if settings.llm_base_url:
            kwargs["base_url"] = settings.llm_base_url
        self.client = OpenAI(**kwargs)
        self.settings = settings
        self.json_mode = True
        self.usage_tokens = 0
        self.calls = 0

    def chat_json(self, system: str, user: str, temperature: float | None = None) -> dict[str, Any]:
        """调用模型并解析 JSON，带重试与 JSON mode 自动降级。"""
        from openai import (
            APIConnectionError,
            APIStatusError,
            APITimeoutError,
            BadRequestError,
            RateLimitError,
        )

        temp = self.settings.llm_temperature if temperature is None else temperature
        last_error: Exception | None = None

        for attempt in range(1, self.settings.llm_max_retries + 1):
            payload: dict[str, Any] = {
                "model": self.settings.llm_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": temp,
            }
            if self.json_mode:
                payload["response_format"] = {"type": "json_object"}

            try:
                response = self.client.chat.completions.create(**payload)
                self.calls += 1
                if getattr(response, "usage", None):
                    self.usage_tokens += int(getattr(response.usage, "total_tokens", 0) or 0)
                content = (response.choices[0].message.content or "").strip()
                return extract_json(content)
            except BadRequestError as exc:
                message = str(exc).lower()
                if self.json_mode and (
                    "response_format" in message or "json" in message or "not support" in message
                ):
                    self.json_mode = False
                    logger.warning("模型不支持 JSON mode，已切换为普通输出解析：%s", exc)
                    continue
                raise AnalyzerError(f"模型请求被拒绝（400）：{exc}") from exc
            except (RateLimitError, APITimeoutError, APIConnectionError) as exc:
                last_error = exc
                wait = min(2**attempt, 30)
                logger.warning("模型调用失败（第 %s 次），%ss 后重试：%s", attempt, wait, exc)
                time.sleep(wait)
            except APIStatusError as exc:
                status = getattr(exc, "status_code", 0) or 0
                if 500 <= status < 600:
                    last_error = exc
                    wait = min(2**attempt, 30)
                    logger.warning("模型服务端 %s，%ss 后重试。", status, wait)
                    time.sleep(wait)
                else:
                    raise AnalyzerError(f"模型调用失败（HTTP {status}）：{exc}") from exc
            except ValueError as exc:
                last_error = exc
                logger.warning("模型输出无法解析为 JSON（第 %s 次）：%s", attempt, exc)

        raise AnalyzerError(f"模型调用重试 {self.settings.llm_max_retries} 次后仍失败：{last_error}")


# --------------------------------------------------------------------------- #
# 分析器
# --------------------------------------------------------------------------- #


class Analyzer:
    def __init__(self, settings: Settings, progress: Callable[[str, float], None] | None = None) -> None:
        self.settings = settings
        self.progress = progress
        self.llm: LLMClient | None = None
        self.stats: dict[str, Any] = {
            "posts_analyzed": 0,
            "batches": 0,
            "batch_errors": 0,
            "items_raw": 0,
            "items_merged": 0,
            "quotes_rejected": 0,
            "llm_calls": 0,
            "llm_tokens": 0,
        }

    # ---------------------------------------------------------------- #
    # 入口
    # ---------------------------------------------------------------- #
    def _notify(self, message: str, fraction: float) -> None:
        if self.progress:
            try:
                self.progress(message, max(0.0, min(1.0, fraction)))
            except Exception:  # noqa: BLE001 - 进度回调异常不得影响分析
                pass

    def run(self, payload: dict[str, Any]) -> AnalysisResult:
        posts = [p for p in payload.get("posts", []) if p.get("title")]
        if not posts:
            raise AnalyzerError("采集结果为空，无法分析。请放宽关键词、提高 --limit 或更换板块。")

        selected = self._select_posts(posts)
        self.stats["posts_analyzed"] = len(selected)
        logger.info("参与分析的帖子：%s / %s 篇", len(selected), len(posts))
        self._notify(f"已筛选 {len(selected)} 篇高信号帖子，开始语义分析", 0.05)

        if self.settings.use_llm:
            try:
                items = self._extract_with_llm(selected)
                method = "llm"
                degraded = False
            except AnalyzerError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("AI 分析异常，降级为规则模式：%s", exc)
                items = self._extract_heuristic(selected)
                method = "heuristic"
                degraded = True
        else:
            items = self._extract_heuristic(selected)
            method = "heuristic"
            degraded = False

        if not items:
            raise AnalyzerError(
                "本次样本没有提取到有效需求信号。建议更换板块 / 关键词，或提高 --limit 后重试。"
            )

        items = self._merge(items)
        self._notify("AI 抽取完成，正在加权打分与生成摘要", 0.85)
        self._score(items, posts)
        items.sort(key=lambda it: (it.demand_score, it.evidence_count), reverse=True)
        items = items[:40]

        summary = self._build_summary(items, payload, method)
        self.stats["items_merged"] = len(items)
        if self.llm:
            self.stats["llm_calls"] = self.llm.calls
            self.stats["llm_tokens"] = self.llm.usage_tokens

        return AnalysisResult(
            summary=summary,
            items=items,
            method=method,
            model=self.settings.llm_model if method == "llm" else "heuristic-rules",
            stats=dict(self.stats),
            degraded=degraded,
        )

    # ---------------------------------------------------------------- #
    # 候选选择与批次
    # ---------------------------------------------------------------- #
    def _select_posts(self, posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        limit = max(1, int(self.settings.max_posts_to_analyze))

        def key(post: dict[str, Any]) -> tuple[int, float, int]:
            return (
                len(post.get("matched_keywords") or []),
                math.log1p(max(post.get("score", 0), 0)),
                post.get("num_comments", 0),
            )

        ranked = sorted(posts, key=key, reverse=True)
        return ranked[:limit]

    def _render_post(self, ref: str, post: dict[str, Any]) -> str:
        lines = [
            f"[{ref}] r/{post.get('subreddit', '?')} | score={post.get('score', 0)} "
            f"| comments={post.get('num_comments', 0)} "
            f"| keywords={','.join(post.get('matched_keywords') or []) or '-'}",
            f"TITLE: {post.get('title', '')}",
        ]
        selftext = (post.get("selftext") or "").strip()
        if selftext:
            body = selftext[: self.settings.max_chars_per_post]
            lines.append(f"BODY: {body}")
        comments = post.get("top_comments") or []
        if comments:
            lines.append("TOP COMMENTS:")
            for comment in comments[: self.settings.comment_limit]:
                snippet = (comment.get("body") or "")[: self.settings.max_chars_per_comment]
                lines.append(f"- (score {comment.get('score', 0)}) {snippet}")
        return "\n".join(lines)

    def _build_batches(
        self, posts: list[dict[str, Any]]
    ) -> Iterable[tuple[int, list[tuple[str, dict[str, Any]]]]]:
        batch: list[tuple[str, dict[str, Any]]] = []
        size = 0
        index = 0
        for post in posts:
            ref = f"P{len(batch) + 1}"
            block = self._render_post(ref, post)
            if batch and (len(batch) >= self.settings.batch_size or size + len(block) > MAX_BATCH_CHARS):
                index += 1
                yield index, batch
                batch, size = [], 0
                ref = "P1"
                block = self._render_post(ref, post)
            batch.append((ref, post))
            size += len(block) + 2
        if batch:
            index += 1
            yield index, batch

    # ---------------------------------------------------------------- #
    # LLM 抽取
    # ---------------------------------------------------------------- #
    def _extract_with_llm(self, posts: list[dict[str, Any]]) -> list[BacklogItem]:
        self.llm = LLMClient(self.settings)
        items: list[BacklogItem] = []
        batches = list(self._build_batches(posts))
        self.stats["batches"] = len(batches)

        for index, batch in batches:
            self._notify(
                f"正在分析第 {index}/{len(batches)} 批（累计 {len(items)} 条候选）",
                0.05 + 0.75 * (index - 1) / max(1, len(batches)),
            )
            ref_map = {ref: post for ref, post in batch}
            blocks = [self._render_post(ref, post) for ref, post in batch]
            user_prompt = (
                f"赛道样本（第 {index}/{len(batches)} 批，时间范围 {self.settings.time_range_days} 天）。\n"
                f"请按系统指令抽取功能愿望，只输出 JSON。\n\n" + "\n\n---\n\n".join(blocks)
            )
            try:
                payload = self.llm.chat_json(SYSTEM_PROMPT, user_prompt)
            except AnalyzerError as exc:
                self.stats["batch_errors"] += 1
                logger.error("第 %s/%s 批分析失败：%s", index, len(batches), exc)
                continue

            raw_items = payload.get("items") or []
            if not isinstance(raw_items, list):
                self.stats["batch_errors"] += 1
                logger.error("第 %s 批返回结构异常，已跳过。", index)
                continue

            for raw in raw_items:
                item = self._normalize_raw_item(raw, ref_map)
                if item:
                    items.append(item)
                    self.stats["items_raw"] += 1

            logger.info(
                "第 %s/%s 批完成：新增 %s 条，累计 %s 条。",
                index,
                len(batches),
                len(raw_items),
                len(items),
            )

        if not items:
            if self.stats["batch_errors"] >= len(batches):
                raise AnalyzerError(
                    "所有批次都调用失败，请检查 API Key / Base URL / 模型名是否正确。"
                )
            logger.warning("AI 未提取到有效条目，降级为规则模式。")
            return self._extract_heuristic(posts)

        logger.info("AI 原始条目 %s 条，平均每批 %.1f 条。", len(items), len(items) / max(1, len(batches)))
        return items

    def _normalize_raw_item(
        self, raw: Any, ref_map: dict[str, dict[str, Any]]
    ) -> BacklogItem | None:
        if not isinstance(raw, dict):
            return None
        name = str(raw.get("feature_name") or "").strip()
        if not name:
            return None
        name = name.strip("。.；;")[:80]
        description = str(raw.get("pain_description") or "").strip()[:120] or name

        category = str(raw.get("category") or "other").strip().lower()
        if category not in VALID_CATEGORIES:
            category = "other"
        sentiment = str(raw.get("sentiment") or "neutral").strip().lower()
        if sentiment not in VALID_SENTIMENTS:
            sentiment = "neutral"
        intent = str(raw.get("intent") or "question").strip().lower()
        if intent not in VALID_INTENTS:
            intent = "question"

        refs = raw.get("source_refs") or []
        if isinstance(refs, str):
            refs = [refs]
        post_ids: list[str] = []
        subreddits: list[str] = []
        for ref in refs:
            post = ref_map.get(str(ref))
            if not post:
                continue
            pid = str(post.get("id"))
            if pid not in post_ids:
                post_ids.append(pid)
                subreddit = post.get("subreddit")
                if subreddit and subreddit not in subreddits:
                    subreddits.append(subreddit)

        quotes: list[dict[str, Any]] = []
        raw_quotes = raw.get("verbatim_quotes") or []
        if isinstance(raw_quotes, dict):
            raw_quotes = [raw_quotes]
        for entry in raw_quotes:
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("quote") or "").strip()
            if not text:
                continue
            ref = str(entry.get("ref") or "")
            post = ref_map.get(ref) or (ref_map.get(str(refs[0])) if refs else None)
            if not post:
                continue
            if not self._quote_is_grounded(text, post):
                self.stats["quotes_rejected"] += 1
                logger.debug("丢弃无法在原文中找到的引用：%r", text[:60])
                continue
            quotes.append(
                {
                    "quote": text[:600],
                    "post_id": str(post.get("id")),
                    "subreddit": post.get("subreddit", ""),
                    "score": int(post.get("score", 0) or 0),
                    "permalink": post.get("permalink", ""),
                    "kind": "selftext" if len(text) > 120 else "comment_or_title",
                }
            )

        if not post_ids:
            return None

        keywords: list[str] = []
        for pid in post_ids:
            for post in ref_map.values():
                if str(post.get("id")) == pid:
                    for kw in post.get("matched_keywords") or []:
                        if kw not in keywords:
                            keywords.append(kw)

        return BacklogItem(
            feature_name=name,
            pain_description=description,
            category=category,
            sentiment=sentiment,
            intent=intent,
            llm_demand_score=_clamp(raw.get("demand_score"), 1, 10, 5.0),
            demand_score=_clamp(raw.get("demand_score"), 1, 10, 5.0),
            evidence_count=len(post_ids),
            subreddits=subreddits,
            quotes=quotes[:MAX_QUOTES_PER_ITEM],
            post_ids=post_ids,
            keywords=keywords[:6],
            verified_quotes=len(quotes),
        )

    @staticmethod
    def _quote_is_grounded(quote: str, post: dict[str, Any]) -> bool:
        """确认引用真实存在于原帖/评论中，防止模型幻觉。"""
        haystack = _norm_text(
            " ".join(
                [post.get("title") or "", post.get("selftext") or ""]
                + [c.get("body") or "" for c in (post.get("top_comments") or [])]
            )
        )
        needle = _norm_text(quote)
        if len(needle) < 10:
            return False
        if needle in haystack:
            return True
        # 容忍模型截断或加省略号：用前 40 个有效字符做包含判断
        head = needle[:40]
        return len(head) >= 10 and head in haystack

    # ---------------------------------------------------------------- #
    # 归并
    # ---------------------------------------------------------------- #
    def _merge(self, items: list[BacklogItem]) -> list[BacklogItem]:
        if len(items) <= 1:
            return items

        should_merge = self.settings.merge_with_llm
        if should_merge is None:
            should_merge = bool(self.llm) and len(items) >= self.settings.merge_min_items

        if should_merge and self.llm:
            try:
                merged = self._merge_with_llm(items)
                logger.info("AI 归并：%s 条 → %s 条", len(items), len(merged))
                return merged
            except Exception as exc:  # noqa: BLE001
                logger.warning("AI 归并失败，改用本地相似度合并：%s", exc)

        merged = self._merge_local(items)
        logger.info("本地归并：%s 条 → %s 条", len(items), len(merged))
        return merged

    def _merge_with_llm(self, items: list[BacklogItem]) -> list[BacklogItem]:
        assert self.llm is not None
        listing = "\n".join(
            f"{i}. {item.feature_name} | {item.pain_description} | {item.category} | "
            f"score={item.llm_demand_score:.0f} | 证据={len(item.post_ids)}"
            for i, item in enumerate(items)
        )
        payload = self.llm.chat_json(
            MERGE_SYSTEM_PROMPT,
            f"共 {len(items)} 条待归并：\n{listing}\n\n只输出 JSON。",
            temperature=0.1,
        )
        groups = payload.get("groups")
        if not isinstance(groups, list) or not groups:
            raise AnalyzerError("归并结果为空")

        used: set[int] = set()
        merged_items: list[BacklogItem] = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            indices = group.get("member_indices") or []
            valid = []
            for index in indices:
                try:
                    idx = int(index)
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < len(items) and idx not in used:
                    valid.append(idx)
            if not valid:
                continue
            used.update(valid)
            merged_items.append(
                self._combine(
                    [items[i] for i in valid],
                    name=str(group.get("canonical_feature_name") or "").strip() or None,
                    description=str(group.get("pain_description") or "").strip() or None,
                    category=str(group.get("category") or "").strip().lower() or None,
                    llm_score=group.get("demand_score"),
                )
            )

        for index, item in enumerate(items):
            if index not in used:
                merged_items.append(item)
        return merged_items

    def _merge_local(self, items: list[BacklogItem]) -> list[BacklogItem]:
        merged: list[list[BacklogItem]] = []
        for item in items:
            target: list[BacklogItem] | None = None
            for bucket in merged:
                head = bucket[0]
                if head.category == item.category and _similar(head.feature_name, item.feature_name) >= 0.72:
                    target = bucket
                    break
            if target is None:
                merged.append([item])
            else:
                target.append(item)

        return [
            self._combine(bucket) if len(bucket) > 1 else bucket[0] for bucket in merged
        ]

    def _combine(
        self,
        group: list[BacklogItem],
        name: str | None = None,
        description: str | None = None,
        category: str | None = None,
        llm_score: Any = None,
    ) -> BacklogItem:
        """把多条同义条目合成一条，证据与引文取并集。"""
        group = sorted(group, key=lambda it: (len(it.post_ids), len(it.quotes)), reverse=True)
        head = group[0]

        post_ids: list[str] = []
        quotes: list[dict[str, Any]] = []
        subreddits: list[str] = []
        keywords: list[str] = []
        seen_quotes: set[str] = set()

        for item in group:
            for pid in item.post_ids:
                if pid not in post_ids:
                    post_ids.append(pid)
            for sub in item.subreddits:
                if sub and sub not in subreddits:
                    subreddits.append(sub)
            for kw in item.keywords:
                if kw and kw not in keywords:
                    keywords.append(kw)
            for quote in item.quotes:
                key = _norm_text(quote.get("quote", ""))[:80]
                if key and key not in seen_quotes:
                    seen_quotes.add(key)
                    quotes.append(quote)

        quotes.sort(key=lambda q: q.get("score", 0), reverse=True)

        weights = [max(1, len(it.post_ids)) for it in group]
        scores = [it.llm_demand_score for it in group]
        weighted = sum(s * w for s, w in zip(scores, weights)) / sum(weights)
        final_llm_score = _clamp(llm_score, 1, 10, weighted) if llm_score is not None else weighted

        category_value = (category or head.category) if (category or head.category) in VALID_CATEGORIES else head.category

        return BacklogItem(
            feature_name=(name or head.feature_name)[:80],
            pain_description=(description or head.pain_description)[:120],
            category=category_value,
            sentiment=head.sentiment,
            intent=head.intent,
            llm_demand_score=final_llm_score,
            demand_score=final_llm_score,
            evidence_count=len(post_ids),
            subreddits=subreddits[:6],
            quotes=quotes[:MAX_QUOTES_PER_ITEM],
            post_ids=post_ids,
            keywords=keywords[:6],
            verified_quotes=min(len(quotes), MAX_QUOTES_PER_ITEM),
        )

    # ---------------------------------------------------------------- #
    # 打分
    # ---------------------------------------------------------------- #
    def _score(self, items: list[BacklogItem], all_posts: list[dict[str, Any]]) -> None:
        """需求强度 = 语义分 0.65 + 证据强度 0.35，兼顾"喊得响"与"真有人讨论"。"""
        post_map = {str(p.get("id")): p for p in all_posts}
        weights: list[float] = []

        for item in items:
            weight = 0.0
            for pid in item.post_ids:
                post = post_map.get(pid)
                if not post:
                    continue
                weight += 1.0
                weight += math.log1p(max(post.get("score", 0), 0)) * 0.7
                weight += math.log1p(max(post.get("num_comments", 0), 0)) * 0.3
                weight += 0.2 * len(post.get("matched_keywords") or [])
            weight += 0.3 * len(item.quotes)
            weights.append(weight)

        reference = max(weights) if weights else 1.0
        reference = max(reference, 1e-6)

        for item, weight in zip(items, weights):
            evidence_score = 10.0 * min(1.0, weight / reference)
            final = 0.65 * item.llm_demand_score + 0.35 * evidence_score
            item.demand_score = round(max(0.1, min(10.0, final)), 1)
            item.evidence_count = len(item.post_ids)

    # ---------------------------------------------------------------- #
    # 摘要
    # ---------------------------------------------------------------- #
    def _build_summary(
        self, items: list[BacklogItem], payload: dict[str, Any], method: str
    ) -> str:
        meta = payload.get("meta", {})
        posts = payload.get("posts", [])
        subreddits = ", ".join(f"r/{s}" for s in meta.get("subreddits", [])) or "多个板块"
        top = items[:12]
        listing = "\n".join(
            f"- {it.feature_name}（{CATEGORY_LABELS.get(it.category, it.category)}，"
            f"强度 {it.demand_score}/10，证据 {it.evidence_count} 帖）：{it.pain_description}"
            for it in top
        )

        if self.llm and method == "llm":
            try:
                payload_json = self.llm.chat_json(
                    SUMMARY_SYSTEM_PROMPT,
                    f"样本：{len(posts)} 篇帖子，来自 {subreddits}，"
                    f"时间范围 {meta.get('time_range_days', '?')} 天。\n"
                    f"功能愿望清单（按强度排序，前 {len(top)} 条）：\n{listing}\n\n只输出 JSON。",
                    temperature=0.3,
                )
                summary = str(payload_json.get("summary_markdown") or "").strip()
                if summary:
                    return summary
            except Exception as exc:  # noqa: BLE001
                logger.warning("摘要生成失败，使用本地模板：%s", exc)

        return self._local_summary(items, posts, subreddits)

    def _local_summary(
        self, items: list[BacklogItem], posts: list[dict[str, Any]], subreddits: str
    ) -> str:
        top = items[:3]
        if not top:
            return "- 样本不足，未形成可读结论。"
        leader = top[0]
        categories: dict[str, int] = {}
        for item in items:
            categories[item.category] = categories.get(item.category, 0) + 1
        top_categories = sorted(categories.items(), key=lambda kv: kv[1], reverse=True)[:3]
        category_text = "、".join(
            f"{CATEGORY_LABELS.get(key, key)}（{value} 条）" for key, value in top_categories
        )
        intent_counts: dict[str, int] = {}
        for item in items:
            intent_counts[item.intent] = intent_counts.get(item.intent, 0) + 1
        pay_signal = intent_counts.get("willing_to_pay", 0) + intent_counts.get("switching", 0)

        next_best = "；".join(f"{it.feature_name}（{it.demand_score}/10）" for it in top[1:])
        lines = [
            f"- **最值得切入的机会**：{leader.feature_name} —— {leader.pain_description}",
            f"- **样本规模**：{len(posts)} 篇帖子，来自 {subreddits}，"
            f"归纳为 {len(items)} 条功能愿望。",
            f"- **痛点分布**：{category_text}。",
            f"- **付费/迁移信号**：{pay_signal} 条愿望带有明确付费意愿或换工具倾向。",
            f"- **次优机会**：{next_best or '暂无'}",
            "- **提示**：当前为规则模式生成，仅作结构参考，建议配置大模型后重跑以获得语义级聚类。",
        ]
        return "\n".join(lines)

    # ---------------------------------------------------------------- #
    # 规则降级模式
    # ---------------------------------------------------------------- #
    def _extract_heuristic(self, posts: list[dict[str, Any]]) -> list[BacklogItem]:
        buckets: dict[str, list[dict[str, Any]]] = {}
        for post in posts:
            texts: list[tuple[str, str]] = [
                ("title", post.get("title") or ""),
                ("selftext", post.get("selftext") or ""),
            ]
            texts += [("comment", c.get("body") or "") for c in (post.get("top_comments") or [])]

            for kind, text in texts:
                for sentence in _SENTENCE_SPLIT.split(text):
                    sentence = sentence.strip()
                    if len(sentence) < 30:
                        continue
                    category = _classify_sentence(sentence)
                    if not category:
                        continue
                    buckets.setdefault(category, []).append(
                        {
                            "sentence": sentence[:400],
                            "post": post,
                            "kind": kind,
                            "weight": 1.0 + math.log1p(max(post.get("score", 0), 0)) * 0.5,
                        }
                    )

        items: list[BacklogItem] = []
        for category, entries in buckets.items():
            distinct_posts = {str(e["post"].get("id")) for e in entries}
            if len(distinct_posts) < self.settings.min_evidence_posts:
                continue
            entries.sort(key=lambda e: (e["post"].get("score", 0), e["weight"]), reverse=True)

            post_ids: list[str] = []
            subreddits: list[str] = []
            quotes: list[dict[str, Any]] = []
            intents: list[str] = []
            for entry in entries:
                post = entry["post"]
                pid = str(post.get("id"))
                if pid not in post_ids:
                    post_ids.append(pid)
                if post.get("subreddit") and post["subreddit"] not in subreddits:
                    subreddits.append(post["subreddit"])
                intents.append(_guess_intent(entry["sentence"]))
                # 同一帖子只取一条引文，避免示例重复
                if len(quotes) < MAX_QUOTES_PER_ITEM and pid not in {q["post_id"] for q in quotes}:
                    quotes.append(
                        {
                            "quote": entry["sentence"],
                            "post_id": pid,
                            "subreddit": post.get("subreddit", ""),
                            "score": int(post.get("score", 0) or 0),
                            "permalink": post.get("permalink", ""),
                            "kind": entry["kind"],
                        }
                    )

            label = CATEGORY_LABELS.get(category, category)
            sample = _trim_words(quotes[0]["quote"], 80) if quotes else ""
            description = f"{label}类抱怨集中出现" + (f"，典型表述：{sample}" if sample else "")
            intensity = min(10.0, 4.0 + math.log1p(len(distinct_posts)) * 2.2)
            # 只要桶内出现过付费/迁移表述，就认为该需求带商业信号（比取众数更有用）
            if "willing_to_pay" in intents:
                intent = "willing_to_pay"
            elif "switching" in intents:
                intent = "switching"
            else:
                intent = max(set(intents), key=intents.count) if intents else "feature_request"

            items.append(
                BacklogItem(
                    feature_name=_feature_name_for(category),
                    pain_description=description,
                    category=category,
                    sentiment="angry" if intent == "venting" else "frustrated",
                    intent=intent,
                    llm_demand_score=intensity,
                    demand_score=intensity,
                    evidence_count=len(distinct_posts),
                    subreddits=subreddits,
                    quotes=quotes,
                    post_ids=post_ids,
                    keywords=[],
                    verified_quotes=len(quotes),
                )
            )
        logger.info("规则模式：命中 %s 个类别，生成 %s 条。", len(buckets), len(items))
        return items


# --------------------------------------------------------------------------- #
# 规则辅助
# --------------------------------------------------------------------------- #

CATEGORY_RULES: list[tuple[str, str]] = [
    ("data_import_export", r"\b(import|export|csv|excel|backup|migrat|sync my data)\w*"),
    ("integration", r"\b(integrat|zapier|api|slack|jira|webhook|connector|plugin)\w*"),
    ("performance", r"\b(slow|lag|freez|crash|timeout|hang|takes forever|load time)\w*"),
    ("ux_usability", r"\b(confus|clunky|usability|hard to use|too many clicks|interface|ui|ux)\b"),
    ("mobile", r"\b(mobile|ios|android|app store|phone|tablet)\w*"),
    ("collaboration", r"\b(team|collaborat|share|permission|invite|multiplayer)\w*"),
    ("automation_ai", r"\b(automat|ai|gpt|llm|workflow|script)\w*"),
    ("pricing_billing", r"\b(price|pricing|expensive|subscription|billing|refund|paywall|per seat)\b"),
    ("search_organization", r"\b(search|filter|tag|organiz|folder|sort)\w*"),
    ("reporting_analytics", r"\b(report|dashboard|analytic|chart|metric|insight)\w*"),
    ("onboarding_support", r"\b(onboard|tutorial|documentation|support|help center)\w*"),
    ("reliability_security", r"\b(downtime|outage|bug|security|privacy|2fa|sso)\w*"),
]

_CATEGORY_PATTERNS = [(cat, re.compile(pattern, re.IGNORECASE)) for cat, pattern in CATEGORY_RULES]

SIGNAL_CUES = tuple({kw.lower() for kw in PRESET_KEYWORDS}) + (
    "i need",
    "we need",
    "please add",
    "should have",
    "anyone else",
    "why can't",
    "no option",
    "can't find",
    "support for",
    "would love",
    "tired of",
    "waste of time",
)


def _classify_sentence(sentence: str) -> str | None:
    """规则模式：一句话必须同时命中「痛点句式」与「功能类别」才计入。"""
    lowered = sentence.lower()
    has_cue = any(cue in lowered for cue in SIGNAL_CUES)
    if not has_cue:
        return None
    for category, pattern in _CATEGORY_PATTERNS:
        if pattern.search(sentence):
            return category
    return None


def _feature_name_for(category: str) -> str:
    label = category.replace("_", " ").title()
    return f"{label} Improvement"


def _trim_words(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,.;:") + "…"


def _guess_intent(sentence: str) -> str:
    """规则模式下用句式判断用户意图（先付费，再迁移，再功能请求，最后吐槽）。"""
    lowered = sentence.lower()
    if any(p in lowered for p in ("would pay", "pay for", "worth paying", "take my money", "shut up and")):
        return "willing_to_pay"
    if any(p in lowered for p in ("switched from", "switching", "alternative to", "moved to", "cancelled")):
        return "switching"
    if any(
        p in lowered
        for p in ("wish there was", "please add", "feature request", "should have", "no way to", "looking for")
    ):
        return "feature_request"
    if any(p in lowered for p in ("annoying", "hate", "frustrat", "tired of", "deal breaker")):
        return "venting"
    return "question"


def _similar(left: str, right: str) -> float:
    a = _norm_text(left)
    b = _norm_text(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    tokens_a, tokens_b = set(a.split()), set(b.split())
    if tokens_a and tokens_b:
        jaccard = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
        ratio = max(ratio, jaccard)
    return ratio


# --------------------------------------------------------------------------- #
# 模块级入口
# --------------------------------------------------------------------------- #


def analyze(
    payload: dict[str, Any],
    settings: Settings,
    progress: Callable[[str, float], None] | None = None,
) -> AnalysisResult:
    return Analyzer(settings, progress=progress).run(payload)
