"""main.py — 主控脚本：串联 采集 → 分析 → 报告。

用法示例
--------
    python main.py --demo
    python main.py --subreddits SaaS,Notion --days 180 --limit 300
    python main.py --mode search --keywords "wish there was,alternative to"
    python main.py --from-cache --no-llm
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import scraper
from analyzer import AnalyzerError, analyze
from config import PROJECT_ROOT, ConfigError, Settings, build_settings
from reporter import priority_of, write_reports

DEMO_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "sample_raw.json"
LOG_FILE = "run.log"

logger = logging.getLogger("main")


def _ensure_utf8_stdout() -> None:
    """Windows 控制台默认 GBK，直接 print 中文/符号可能抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            continue


def setup_logging(settings: Settings) -> None:
    level = logging.WARNING if settings.quiet else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(settings.output_dir / LOG_FILE, encoding="utf-8"))
    except OSError:
        logger.warning("无法写入日志文件，仅输出到控制台。")

    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )
    for noisy in ("urllib3", "httpx", "httpcore", "openai", "prawcore", "chardet"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def load_demo(settings: Settings) -> dict[str, Any]:
    """加载内置样例数据，用于零密钥演示 / 回归测试。"""
    if not DEMO_FIXTURE.is_file():
        raise FileNotFoundError(f"演示数据缺失：{DEMO_FIXTURE}")
    with DEMO_FIXTURE.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)

    meta = payload.get("meta") or {}
    if meta.get("subreddits"):
        settings.subreddits = list(meta["subreddits"])
    if meta.get("keywords"):
        settings.keywords = list(meta["keywords"])
    if meta.get("time_range_days"):
        settings.time_range_days = int(meta["time_range_days"])
    settings.use_llm = False
    logger.info("演示模式：使用内置样例数据，不访问 Reddit 与大模型。")
    return payload


def run(settings: Settings) -> int:
    started = time.perf_counter()

    if settings.demo:
        payload = load_demo(settings)
    elif settings.from_cache:
        payload = scraper.load_posts(settings)
        logger.info("已从缓存载入 %s 篇帖子。", len(payload.get("posts", [])))
    else:
        payload = scraper.collect(settings)

    posts = payload.get("posts") or []
    if not posts:
        raise AnalyzerError(
            "没有可分析的帖子。可能原因：板块拼写错误、时间范围内无内容、"
            "关键词过窄或点赞门槛过高（--comment-min-score）。"
        )

    logger.info("开始语义分析（%s 模式）…", "大模型" if settings.use_llm else "规则降级")
    result = analyze(payload, settings)

    paths = write_reports(result, payload, settings)
    elapsed = time.perf_counter() - started
    _print_summary(result, paths, payload, elapsed)
    return 0


def _print_summary(
    result: Any, paths: dict[str, Path], payload: dict[str, Any], elapsed: float
) -> None:
    posts = payload.get("posts", [])
    comments = sum(len(p.get("top_comments") or []) for p in posts)
    p0 = sum(1 for item in result.items if priority_of(item.demand_score) == "P0")
    p1 = sum(1 for item in result.items if priority_of(item.demand_score) == "P1")

    rule = "─" * 66
    print(f"\n{rule}")
    print("  Reddit 赛道需求报告已生成")
    print(f"  样本　　{len(posts)} 篇帖子 · {comments} 条高赞评论")
    print(
        f"  结论　　{len(result.items)} 条功能愿望（P0 {p0} · P1 {p1}）"
        f"｜分析方法：{'大模型' if result.method == 'llm' else '规则降级'}"
        f"｜模型：{result.model}"
    )
    if result.degraded:
        print("  注意　　本次发生降级，结论仅供参考，建议检查 API 配置后重跑。")
    print("  Top 3")
    for index, item in enumerate(result.items[:3], start=1):
        print(
            f"    {index}. {item.feature_name[:46]:<46} {item.demand_score:>4}/10  "
            f"{priority_of(item.demand_score)}"
        )
    print("  产物")
    for label, path in paths.items():
        print(f"    {label:<9}{path}")
    print(f"  用时　　{elapsed:.1f}s")
    print(f"{rule}\n")


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdout()
    try:
        settings = build_settings(argv)
    except ConfigError as exc:
        print(f"\n[配置错误] {exc}\n", file=sys.stderr)
        return 1

    setup_logging(settings)

    try:
        return run(settings)
    except KeyboardInterrupt:
        print(
            "\n已中断。已抓取的数据保存在 data/，可用 --from-cache 继续分析。",
            file=sys.stderr,
        )
        return 130
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 2
    except AnalyzerError as exc:
        logger.error("分析失败：%s", exc)
        return 3
    except Exception as exc:  # noqa: BLE001 - 顶层兜底，保证有意义的退出码
        logger.exception("未预期的错误：%s", exc)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
