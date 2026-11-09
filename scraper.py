"""scraper.py — 基于 PRAW 8 的 Reddit 数据采集模块。

设计要点
--------
1. 只读、公开数据：默认以 read-only 模式运行，不需要账号密码。
2. 成本可控：评论用 ``replace_more(limit=0)`` 丢弃 "load more" 节点，
   不产生额外网络请求；只保留按 score 排序后的前 N 条。
3. 抗中断：每篇帖子单独落盘缓存，汇总文件原子写入，重跑自动续传。
4. 抗限流：PRAW 自身遵守 ``X-Ratelimit-*`` 头；这里再包一层退避重试，
   并把单帖失败降级为"跳过"而不是中断整轮任务。
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import praw
import prawcore
from praw.exceptions import RedditAPIException
from praw.models import MoreComments
from tqdm import tqdm

from config import Settings

CACHE_SCHEMA_VERSION = 1
MIN_COMMENT_CHARS = 24
FLUSH_EVERY = 20

_MD_LINK = re.compile(r"\[([^\]]*)\]\((?:https?://|/)[^)]*\)")
_URL = re.compile(r"https?://\S+")
_QUOTE_PREFIX = re.compile(r"^\s*>+\s?", re.MULTILINE)
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_WS = re.compile(r"\s+")

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 文本清洗
# --------------------------------------------------------------------------- #


def clean_text(text: str | None, max_len: int | None = None) -> str:
    """把 Markdown / HTML 噪声压平成适合喂给大模型的纯文本。"""
    if not text:
        return ""
    text = html.unescape(text)
    text = _QUOTE_PREFIX.sub("", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _URL.sub(" ", text)
    text = _BOLD.sub(r"\1", text)
    text = text.replace("\u200b", "").replace("\xa0", " ")
    text = _WS.sub(" ", text).strip()
    if max_len is not None and len(text) > max_len:
        text = text[:max_len].rstrip() + "…"
    return text


def is_noise(text: str) -> bool:
    """过滤掉没有分析价值的评论（删除、纯表情、纯标点、过短）。"""
    if not text:
        return True
    lowered = text.strip().lower()
    if lowered in {"[deleted]", "[removed]", "deleted", "removed"}:
        return True
    if len(lowered) < MIN_COMMENT_CHARS:
        return True
    # 没有任何字母/汉字，说明是表情或符号堆叠
    return not re.search(r"[A-Za-z\u4e00-\u9fff]", text)


def match_keywords(text: str, keywords: list[str]) -> list[str]:
    """返回文本命中的信号词（不分大小写），用于需求加权与标签。"""
    if not text or not keywords:
        return []
    lowered = text.lower()
    return [kw for kw in keywords if kw.lower() in lowered]


# --------------------------------------------------------------------------- #
# PRAW 客户端
# --------------------------------------------------------------------------- #


def build_reddit(settings: Settings) -> praw.Reddit:
    """构造 PRAW 实例。

    PRAW 8 变化提示：``token_manager`` 已移除，刷新令牌直接通过
    ``refresh_token`` 传入；``APIException`` 已移除，统一使用
    ``RedditAPIException``。
    """
    kwargs: dict[str, Any] = {
        "client_id": settings.reddit_client_id,
        "client_secret": settings.reddit_client_secret,
        "user_agent": settings.reddit_user_agent,
        # 文档：Reddit 要求的等待时间 <= ratelimit_seconds 时 PRAW 自动 sleep，
        # 超过则该次请求抛异常。这里放宽到 300s 以减少 429 直接失败。
        "ratelimit_seconds": 300,
        "timeout": 30,
        "check_for_updates": False,
    }
    if settings.reddit_refresh_token:
        kwargs["refresh_token"] = settings.reddit_refresh_token
    elif settings.reddit_username and settings.reddit_password:
        kwargs["username"] = settings.reddit_username
        kwargs["password"] = settings.reddit_password

    reddit = praw.Reddit(**kwargs)

    if reddit.read_only:
        logger.info("Reddit 客户端处于只读模式（仅访问公开数据）。")
    else:
        # PRAW 8：只读模式下 user.me() 会抛 ReadOnlyException，因此只在已授权时校验。
        try:
            me = reddit.user.me()
            logger.info("Reddit 授权成功，当前账号：u/%s", getattr(me, "name", "?"))
        except (prawcore.exceptions.PrawcoreException, RedditAPIException) as exc:
            logger.warning("凭证校验失败（将继续尝试只读采集）：%s", exc)
    return reddit


# --------------------------------------------------------------------------- #
# 缓存与持久化
# --------------------------------------------------------------------------- #


def _atomic_write_json(path: Path, payload: Any) -> None:
    """先写临时文件再原子替换，避免 Ctrl+C 留下半个 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    tmp.replace(path)


class PostCache:
    """按帖子 ID 分文件缓存，支持增量续跑。"""

    def __init__(self, cache_dir: Path) -> None:
        self.dir = cache_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, post_id: str) -> Path:
        return self.dir / f"{post_id}.json"

    def get(self, post_id: str) -> dict[str, Any] | None:
        path = self._path(post_id)
        if not path.is_file():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if payload.get("cache_schema") != CACHE_SCHEMA_VERSION:
                return None
            return payload
        except (OSError, json.JSONDecodeError):
            logger.debug("缓存损坏，忽略：%s", path)
            return None

    def put(self, post: dict[str, Any]) -> None:
        payload = dict(post)
        payload["cache_schema"] = CACHE_SCHEMA_VERSION
        payload["cached_at"] = datetime.now(timezone.utc).isoformat()
        try:
            _atomic_write_json(self._path(str(post["id"])), payload)
        except OSError as exc:
            logger.warning("缓存写入失败（不影响主流程）：%s", exc)


# --------------------------------------------------------------------------- #
# 采集
# --------------------------------------------------------------------------- #


def _should_skip(submission: Any, settings: Settings, cutoff_ts: float) -> str | None:
    """返回跳过原因；None 表示保留。置顶公告与 NSFW 按需求直接剔除。"""
    if getattr(submission, "stickied", False):
        return "stickied"
    if getattr(submission, "over_18", False):
        return "nsfw"
    score = int(getattr(submission, "score", 0) or 0)
    if score < settings.post_min_score:
        return "low_score"
    created = float(getattr(submission, "created_utc", 0) or 0)
    if created and created < cutoff_ts:
        return "out_of_range"
    return None


def _author_name(obj: Any) -> str:
    author = getattr(obj, "author", None)
    if author is None:
        return "[deleted]"
    return str(getattr(author, "name", "") or "[deleted]")


def _top_comments(submission: Any, settings: Settings) -> list[dict[str, Any]]:
    """抽取前 N 条高赞评论。

    ``replace_more(limit=0)`` 只是移除 "load more comments" 占位节点，
    不会发起额外请求（文档：limit=0 simply removes all MoreComments）。
    """
    if settings.comment_limit <= 0:
        return []
    try:
        submission.comments.replace_more(limit=0)
        flat = submission.comments.list()
    except (prawcore.exceptions.PrawcoreException, RedditAPIException) as exc:
        logger.warning("评论拉取失败，跳过该帖评论：%s", exc)
        return []

    collected: list[dict[str, Any]] = []
    for comment in flat:
        if isinstance(comment, MoreComments):
            continue
        if getattr(comment, "sticky", False):
            continue
        body = clean_text(getattr(comment, "body", ""), settings.max_chars_per_comment)
        if is_noise(body):
            continue
        score = int(getattr(comment, "score", 0) or 0)
        if score < settings.comment_min_score:
            continue
        collected.append(
            {
                "id": str(getattr(comment, "id", "")),
                "body": body,
                "score": score,
                "author": _author_name(comment),
                "is_submitter": bool(getattr(comment, "is_submitter", False)),
                "created_utc": float(getattr(comment, "created_utc", 0) or 0),
            }
        )

    collected.sort(key=lambda item: item["score"], reverse=True)
    return collected[: settings.comment_limit]


def _extract_post(submission: Any, settings: Settings, keywords: list[str]) -> dict[str, Any]:
    """把 PRAW Submission 转成扁平 dict（只保留分析需要的字段）。"""
    title = clean_text(getattr(submission, "title", ""), 400)
    selftext = clean_text(getattr(submission, "selftext", ""), settings.max_chars_per_post)
    created = float(getattr(submission, "created_utc", 0) or 0)
    matched = match_keywords(f"{title}\n{selftext}", keywords)

    post: dict[str, Any] = {
        "id": str(getattr(submission, "id", "")),
        "subreddit": str(getattr(submission, "subreddit", "")),
        "title": title,
        "selftext": selftext,
        "score": int(getattr(submission, "score", 0) or 0),
        "upvote_ratio": float(getattr(submission, "upvote_ratio", 0) or 0),
        "num_comments": int(getattr(submission, "num_comments", 0) or 0),
        "created_utc": created,
        "created_iso": datetime.fromtimestamp(created, tz=timezone.utc).isoformat()
        if created
        else "",
        "permalink": f"https://www.reddit.com{getattr(submission, 'permalink', '')}",
        "flair": getattr(submission, "link_flair_text", None) or "",
        "author": _author_name(submission),
        "matched_keywords": matched,
        "top_comments": [],
    }

    if settings.comment_limit > 0 and post["num_comments"] > 0:
        post["top_comments"] = _top_comments(submission, settings)
        for comment in post["top_comments"]:
            for kw in match_keywords(comment["body"], keywords):
                if kw not in post["matched_keywords"]:
                    post["matched_keywords"].append(kw)
    return post


def _guarded(
    iterator: Iterator[Any],
    seen: set[str],
    label: str,
    max_retries: int = 3,
) -> Iterator[Any]:
    """给 PRAW 的 listing 生成器加一层限流/网络错误重试与去重。"""
    retries = 0
    while True:
        try:
            submission = next(iterator)
        except StopIteration:
            return
        except prawcore.exceptions.TooManyRequests:
            retries += 1
            if retries > max_retries:
                logger.error("[%s] 持续被限流，放弃该列表（已采集部分保留）。", label)
                return
            wait = 60 * retries
            logger.warning("[%s] 触发 429，等待 %ss 后重试（%s/%s）。", label, wait, retries, max_retries)
            time.sleep(wait)
            continue
        except prawcore.exceptions.ServerError:
            retries += 1
            if retries > max_retries:
                logger.error("[%s] Reddit 服务端持续异常，放弃该列表。", label)
                return
            time.sleep(5 * retries)
            continue
        except prawcore.exceptions.RequestException as exc:
            retries += 1
            if retries > max_retries:
                logger.error("[%s] 网络请求失败，放弃该列表：%s", label, exc)
                return
            logger.warning("[%s] 网络异常，%ss 后重试：%s", label, 3 * retries, exc)
            time.sleep(3 * retries)
            continue
        except (prawcore.exceptions.Forbidden, prawcore.exceptions.NotFound) as exc:
            logger.error("[%s] 板块不可访问（私有/被封禁/拼写错误）：%s", label, exc)
            return
        except RedditAPIException as exc:
            logger.error("[%s] Reddit API 返回错误，放弃该列表：%s", label, exc)
            return
        except KeyboardInterrupt:
            raise

        retries = 0
        post_id = str(getattr(submission, "id", ""))
        if not post_id or post_id in seen:
            continue
        seen.add(post_id)
        yield submission


def _iter_submissions(reddit: praw.Reddit, name: str, settings: Settings) -> Iterator[Any]:
    """按采集模式产出 submission：板块热帖 / 关键词搜索 / 两者合并。"""
    subreddit = reddit.subreddit(name)
    seen: set[str] = set()

    if settings.collect_mode in {"top", "both"}:
        logger.info("[r/%s] 拉取 top(%s)，上限 %s 篇", name, settings.time_filter, settings.post_limit)
        listing = subreddit.top(time_filter=settings.time_filter, limit=settings.post_limit)
        yield from _guarded(listing, seen, f"r/{name} top")

    if settings.collect_mode in {"search", "both"} and settings.keywords:
        per_kw = max(10, settings.per_keyword_limit)
        for kw in settings.keywords:
            logger.info("[r/%s] 搜索 %r（每词上限 %s 条）", name, kw, per_kw)
            listing = subreddit.search(
                kw, sort="relevance", time_filter=settings.time_filter, limit=per_kw
            )
            yield from _guarded(listing, seen, f"r/{name} q={kw}")
            if settings.request_delay:
                time.sleep(settings.request_delay)


def _load_existing_posts(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        posts = payload.get("posts", [])
        return {str(p["id"]): p for p in posts if p.get("id")}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("已有汇总文件无法读取，将重新开始：%s", exc)
        return {}


# 进度回调签名：(阶段说明, 0-1 的完成度)。UI 层用它驱动进度条，CLI 传 None 即可。
ProgressFn = Callable[[str, float], None]


def collect(settings: Settings, progress: ProgressFn | None = None) -> dict[str, Any]:
    """执行采集，返回 ``{"meta": ..., "posts": [...], "stats": ...}``。"""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    cache = PostCache(settings.cache_dir)
    posts = {} if settings.force_refresh else _load_existing_posts(settings.raw_data_path)
    initial_count = len(posts)

    cutoff_ts = time.time() - settings.time_range_days * 86400
    stats = {
        "seen": 0,
        "kept": 0,
        "from_cache": 0,
        "skipped": {"stickied": 0, "nsfw": 0, "low_score": 0, "out_of_range": 0},
        "failed": 0,
    }

    reddit = build_reddit(settings)
    total_budget = settings.post_limit * max(1, len(settings.subreddits))

    bar = tqdm(
        total=total_budget,
        desc="采集 Reddit",
        unit="帖",
        disable=settings.quiet,
        dynamic_ncols=True,
    )
    if progress:
        progress(f"已连接 Reddit，开始采集 {len(settings.subreddits)} 个板块", 0.0)
    try:
        for name in settings.subreddits:
            for submission in _iter_submissions(reddit, name, settings):
                stats["seen"] += 1
                bar.update(1)
                if progress and stats["seen"] % 5 == 0:
                    fraction = min(0.99, stats["seen"] / max(1, total_budget))
                    progress(
                        f"r/{name}：已扫描 {stats['seen']} 帖，入库 {len(posts)} 帖",
                        round(fraction, 3),
                    )
                post_id = str(getattr(submission, "id", ""))

                cached = None if settings.force_refresh else cache.get(post_id)
                if cached is not None:
                    posts[post_id] = {
                        k: v for k, v in cached.items() if k not in {"cached_at", "cache_schema"}
                    }
                    stats["from_cache"] += 1
                    continue

                reason = _should_skip(submission, settings, cutoff_ts)
                if reason:
                    stats["skipped"][reason] = stats["skipped"].get(reason, 0) + 1
                    continue

                try:
                    post = _extract_post(submission, settings, settings.keywords)
                except Exception as exc:  # noqa: BLE001 - 单帖失败不应中断整轮
                    stats["failed"] += 1
                    logger.warning("解析帖子 %s 失败，已跳过：%s", post_id, exc)
                    continue

                if not post["title"]:
                    stats["failed"] += 1
                    continue

                posts[post_id] = post
                cache.put(post)
                stats["kept"] += 1

                if stats["kept"] % FLUSH_EVERY == 0:
                    _atomic_write_json(settings.raw_data_path, _build_payload(settings, posts, stats))

                if settings.request_delay:
                    time.sleep(settings.request_delay)
    except KeyboardInterrupt:
        logger.warning("采集被中断，正在保存已获取的数据…")
    finally:
        bar.close()
        payload = _build_payload(settings, posts, stats)
        _atomic_write_json(settings.raw_data_path, payload)

    if progress:
        progress(f"采集完成：入库 {len(posts)} 帖", 1.0)

    logger.info(
        "采集完成：新增 %s 篇，缓存命中 %s 篇，累计样本 %s 篇（任务前已有 %s 篇）。",
        stats["kept"],
        stats["from_cache"],
        len(posts),
        initial_count,
    )
    return payload


def _build_payload(
    settings: Settings, posts: dict[str, dict[str, Any]], stats: dict[str, Any]
) -> dict[str, Any]:
    ordered = sorted(
        posts.values(),
        key=lambda p: (p.get("score", 0), p.get("num_comments", 0)),
        reverse=True,
    )
    return {
        "meta": {
            **settings.public_meta(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "post_count": len(ordered),
            "cache_schema": CACHE_SCHEMA_VERSION,
        },
        "stats": stats,
        "posts": ordered,
    }


def load_posts(settings: Settings) -> dict[str, Any]:
    """读取已采集的汇总数据（--from-cache / --demo 使用）。"""
    path = settings.raw_data_path
    if not path.is_file():
        raise FileNotFoundError(
            f"未找到采集数据：{path}。请先运行一次不带 --from-cache 的采集。"
        )
    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    payload.setdefault("posts", [])
    payload.setdefault("meta", {})
    return payload
