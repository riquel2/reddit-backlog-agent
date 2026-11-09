"""离线回归测试：不访问 Reddit，也不调用大模型。

运行：
    cd reddit_backlog_agent
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import scraper  # noqa: E402
from analyzer import Analyzer, BacklogItem, LLMClient, extract_json  # noqa: E402
from config import Settings  # noqa: E402
from reporter import _safe, priority_of, write_reports  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "sample_raw.json"


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class FakeForest(list):
    """模拟 praw 的 CommentForest：只实现被测代码用到的方法。"""

    def __init__(self, comments):
        super().__init__(comments)
        self.replaced_limit = None

    def replace_more(self, limit=0):
        self.replaced_limit = limit

    def list(self):
        return list(self)


class CleanTextTests(unittest.TestCase):
    def test_strips_markdown_links_and_entities(self):
        raw = "**Hello** [world](https://example.com/x) &amp;  more"
        self.assertEqual(scraper.clean_text(raw), "Hello world & more")

    def test_collapses_whitespace_and_quotes(self):
        raw = "> quoted line\n\nsecond   line"
        self.assertEqual(scraper.clean_text(raw), "quoted line second line")

    def test_truncates_with_ellipsis(self):
        result = scraper.clean_text("a" * 50, 20)
        self.assertLessEqual(len(result), 21)
        self.assertTrue(result.endswith("…"))

    def test_noise_detection(self):
        self.assertTrue(scraper.is_noise("[deleted]"))
        self.assertTrue(scraper.is_noise("👍🔥"))
        self.assertTrue(scraper.is_noise("too short"))
        self.assertFalse(
            scraper.is_noise("This import flow is broken and nobody fixed it for months")
        )

    def test_keyword_matching_is_case_insensitive(self):
        hits = scraper.match_keywords("I Wish There Was a better way", ["wish there was"])
        self.assertEqual(hits, ["wish there was"])


class FilterTests(unittest.TestCase):
    def _post(self, **overrides):
        base = dict(stickied=False, over_18=False, score=100, created_utc=2_000_000_000)
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_filters_stickied_and_nsfw(self):
        settings = Settings()
        cutoff = 1_000_000_000
        self.assertEqual(scraper._should_skip(self._post(stickied=True), settings, cutoff), "stickied")
        self.assertEqual(scraper._should_skip(self._post(over_18=True), settings, cutoff), "nsfw")
        self.assertEqual(
            scraper._should_skip(self._post(created_utc=500), settings, cutoff), "out_of_range"
        )
        self.assertIsNone(scraper._should_skip(self._post(), settings, cutoff))


class ExtractPostTests(unittest.TestCase):
    def test_comments_are_sorted_filtered_and_trimmed(self):
        def comment(cid, body, score, sticky=False):
            return SimpleNamespace(
                id=cid,
                body=body,
                score=score,
                author=SimpleNamespace(name=f"u_{cid}"),
                is_submitter=False,
                created_utc=2_000_000_000,
                sticky=sticky,
            )

        forest = FakeForest(
            [
                comment("c1", "Wish there was a bulk import, this takes forever every single week", 5),
                comment("c2", "Same problem here, we waste two days per month on this import", 80),
                comment("c3", "[deleted]", 999),
                comment("c4", "sticky note from a moderator about the rules", 500, sticky=True),
            ]
        )
        submission = SimpleNamespace(
            id="abc123",
            subreddit="SaaS",
            title="Bulk import is broken",
            selftext="We need a better CSV import. I wish there was one that maps columns.",
            score=250,
            upvote_ratio=0.9,
            num_comments=4,
            created_utc=2_000_000_000,
            permalink="/r/SaaS/comments/abc123/x/",
            link_flair_text="Rant",
            author=SimpleNamespace(name="op"),
            comments=forest,
        )

        settings = Settings(comment_limit=2, comment_min_score=1)
        post = scraper._extract_post(submission, settings, ["wish there was"])

        self.assertEqual([c["id"] for c in post["top_comments"]], ["c2", "c1"])
        self.assertEqual(len(post["top_comments"]), 2)
        self.assertEqual(submission.comments.replaced_limit, 0, "必须用 limit=0 避免额外请求")
        self.assertIn("wish there was", post["matched_keywords"])
        self.assertTrue(post["permalink"].startswith("https://www.reddit.com/"))

    def test_comment_limit_zero_skips_forest(self):
        submission = SimpleNamespace(
            id="x1",
            subreddit="SaaS",
            title="No comments wanted",
            selftext="",
            score=10,
            upvote_ratio=1.0,
            num_comments=5,
            created_utc=2_000_000_000,
            permalink="/r/SaaS/x1/",
            link_flair_text=None,
            author=None,
            comments=[],
        )
        post = scraper._extract_post(submission, Settings(comment_limit=0), [])
        self.assertEqual(post["top_comments"], [])
        self.assertEqual(post["author"], "[deleted]")


class JsonParsingTests(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_fenced_json(self):
        self.assertEqual(extract_json('```json\n{"items": [1]}\n```'), {"items": [1]})

    def test_json_with_surrounding_prose(self):
        raw = 'Sure, here you go:\n{"items": [{"x": 1}]}\nLet me know if you need more.'
        self.assertEqual(extract_json(raw)["items"][0]["x"], 1)

    def test_trailing_comma(self):
        self.assertEqual(extract_json('{"a": [1, 2,],}'), {"a": [1, 2]})

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            extract_json("no json at all")


class ScoringTests(unittest.TestCase):
    def test_priority_thresholds(self):
        self.assertEqual(priority_of(8.0), "P0")
        self.assertEqual(priority_of(6.5), "P1")
        self.assertEqual(priority_of(4.5), "P2")
        self.assertEqual(priority_of(4.4), "P3")

    def test_safe_truncates_on_word_boundary(self):
        text = "the quick brown fox jumps over the lazy dog"
        result = _safe(text, 20)
        self.assertTrue(result.endswith("…"))
        self.assertLessEqual(len(result), 21)


class LocalMergeTests(unittest.TestCase):
    def test_similar_names_merge_within_same_category(self):
        analyzer = Analyzer(Settings(use_llm=False))
        items = [
            BacklogItem("Bulk CSV Import", "a", category="data_import_export", post_ids=["p1"]),
            BacklogItem("Bulk CSV Import Tool", "b", category="data_import_export", post_ids=["p2"]),
            BacklogItem("Slack Integration", "c", category="integration", post_ids=["p3"]),
        ]
        merged = analyzer._merge_local(items)
        self.assertEqual(len(merged), 2)
        csv_item = next(item for item in merged if item.category == "data_import_export")
        self.assertEqual(sorted(csv_item.post_ids), ["p1", "p2"])


class HeuristicPipelineTests(unittest.TestCase):
    def test_end_to_end_generates_all_artifacts(self):
        payload = load_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(use_llm=False, demo=True, output_dir=Path(tmp))
            result = Analyzer(settings).run(payload)

            self.assertEqual(result.method, "heuristic")
            self.assertGreaterEqual(len(result.items), 5)
            self.assertTrue(all(1.0 <= item.demand_score <= 10.0 for item in result.items))
            self.assertEqual(
                [item.demand_score for item in result.items],
                sorted([item.demand_score for item in result.items], reverse=True),
            )

            paths = write_reports(result, payload, settings)
            for key in ("markdown", "csv", "json", "excel"):
                self.assertIn(key, paths)
                self.assertTrue(paths[key].is_file(), f"{key} 未生成")

            markdown = paths["markdown"].read_text(encoding="utf-8")
            for heading in ("执行摘要", "优先级矩阵", "功能愿望清单", "用户原话墙", "方法与局限"):
                self.assertIn(heading, markdown)

            post_ids = {p["id"] for p in payload["posts"]}
            for item in result.items:
                self.assertTrue(set(item.post_ids) <= post_ids)
                for quote in item.quotes:
                    self.assertIn(quote["post_id"], post_ids)

            from openpyxl import load_workbook

            workbook = load_workbook(paths["excel"])
            self.assertIn("Backlog", workbook.sheetnames)
            self.assertEqual(workbook["Backlog"].freeze_panes, "A2")


class LLMPipelineTests(unittest.TestCase):
    def test_llm_items_are_validated_and_hallucinated_quotes_dropped(self):
        # 只保留一篇帖子，确保 P1 引用可确定地映射到它
        full = load_fixture()
        payload = {"meta": full["meta"], "posts": [full["posts"][0]]}
        settings = Settings(
            use_llm=True,
            llm_api_key="test-key",
            llm_model="mock-model",
            merge_min_items=100,
        )
        valid_quote = "I wish there was a bulk CSV import that just understood our schema."
        fake_quote = "This sentence was invented by the model and appears nowhere in the source."

        def fake_chat(self, system, user, temperature=None):
            if "执行摘要" in system:
                return {"summary_markdown": "- 模拟摘要：批量导入是最强信号。"}
            if "第 1/" not in user:
                return {"items": []}
            return {
                "items": [
                    {
                        "feature_name": "Bulk CSV Import",
                        "pain_description": "用户需要能自动映射列的批量导入",
                        "category": "data_import_export",
                        "sentiment": "frustrated",
                        "intent": "willing_to_pay",
                        "demand_score": 9,
                        "source_refs": ["P1"],
                        "verbatim_quotes": [
                            {"ref": "P1", "quote": valid_quote},
                            {"ref": "P1", "quote": fake_quote},
                        ],
                    }
                ]
            }

        with patch.object(LLMClient, "chat_json", fake_chat):
            result = Analyzer(settings).run(payload)

        self.assertEqual(result.method, "llm")
        self.assertEqual(len(result.items), 1)
        item = result.items[0]
        self.assertEqual(item.feature_name, "Bulk CSV Import")
        self.assertEqual(len(item.quotes), 1, "编造的引用必须被过滤")
        self.assertEqual(item.quotes[0]["quote"], valid_quote)
        self.assertGreaterEqual(item.demand_score, 7.5)
        self.assertIn("模拟摘要", result.summary)

    def test_unknown_category_falls_back_to_other(self):
        payload = load_fixture()
        settings = Settings(use_llm=True, llm_api_key="k", llm_model="m", merge_min_items=100)

        def fake_chat(self, system, user, temperature=None):
            if "执行摘要" in system:
                return {"summary_markdown": "- ok"}
            if "第 1/" not in user:
                return {"items": []}
            return {
                "items": [
                    {
                        "feature_name": "Weird Thing",
                        "pain_description": "x",
                        "category": "made_up_category",
                        "demand_score": 99,
                        "source_refs": ["P1"],
                        "verbatim_quotes": [],
                    }
                ]
            }

        with patch.object(LLMClient, "chat_json", fake_chat):
            result = Analyzer(settings).run(payload)

        self.assertEqual(result.items[0].category, "other")
        self.assertLessEqual(result.items[0].llm_demand_score, 10.0)

    def test_empty_payload_raises(self):
        with self.assertRaises(Exception):
            Analyzer(Settings(use_llm=False)).run({"posts": []})


if __name__ == "__main__":
    unittest.main(verbosity=2)
