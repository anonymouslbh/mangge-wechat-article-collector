from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("mangge_wechat.py")
SPEC = importlib.util.spec_from_file_location("mangge_wechat", MODULE_PATH)
assert SPEC and SPEC.loader
mw = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mw
SPEC.loader.exec_module(mw)


def product(slug: str, price: int, path: str) -> mw.Product:
    return mw.Product(
        slug=slug,
        method="POST",
        path=path,
        price_micros=price,
        billing_unit="request",
        raw={"slug": slug, "priceMicros": price},
    )


def collect_args(**overrides):
    values = {
        "account": "示例公众号",
        "ghid": "",
        "candidate": 0,
        "recent": 1,
        "all": False,
        "start_date": "",
        "end_date": "",
        "metadata_only": False,
        "max_pages": 10,
        "max_articles": 200,
        "state_file": "",
        "output_dir": "",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class ProductTests(unittest.TestCase):
    def test_endpoint_is_derived_from_catalog_fields(self):
        item = product(mw.SEARCH_SLUG, 80_000, "/search/accounts")
        self.assertEqual(
            item.endpoint(),
            "https://api.we-media.cn/openapi/wechat-native-search-accounts/search/accounts",
        )

    def test_matching_adjustment_uses_highest_only(self):
        item = mw.Product(
            slug=mw.CONTENT_SLUG,
            method="POST",
            path="/articles/content",
            price_micros=8_000,
            billing_unit="request",
            raw={
                "valuePriceAdjustmentsBps": {"format": {"analysis": 8750, "text": 0}},
                "priceAdjustmentsBps": {"format": 1000},
            },
        )
        self.assertEqual(item.estimate_micros({"format": "analysis"}), 15_000)
        self.assertEqual(item.estimate_micros({"format": "text"}), 8_800)

    def test_unsafe_catalog_path_is_rejected(self):
        item = product(mw.CONTENT_SLUG, 8_000, "/../private")
        with self.assertRaises(mw.ManggeError):
            item.endpoint()


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.index = mw.ArchiveIndex(self.root / "state.sqlite3")

    def tearDown(self):
        self.index.close()
        self.temp.cleanup()

    def test_search_selection_is_cached_without_exposing_identifier(self):
        items = [
            {
                "accountName": "示例公众号",
                "description": "简介",
                "alias": "ExampleAccount",
                "username": "gh_123456789abc",
                "verification": "示例主体",
            },
            {
                "accountName": "相似账号",
                "username": "gh_abcdef123456",
            },
        ]
        self.index.save_search("示例公众号", items)
        cached = self.index.search_results("示例公众号")
        mapping = mw.select_candidate(self.index, "示例公众号", cached, 0)
        self.assertEqual(mapping["ghid"], "gh_123456789abc")
        public = mw.public_candidates(cached)
        self.assertNotIn("username", json.dumps(public, ensure_ascii=False))

    def test_page_checkpoint_and_body_are_persistent(self):
        item = {
            "title": "文章一",
            "canonicalUrl": "https://mp.weixin.qq.com/s?__biz=abc&mid=1&idx=1&sn=xyz",
            "publishTimestamp": 1_700_000_000,
            "publishTime": "2023-11-14T22:13:20Z",
            "contentType": "article",
        }
        fingerprint = self.index.save_page("gh_123456789abc", "示例公众号", "collection", 1, [item])
        self.index.save_checkpoint("gh_123456789abc", "collection", 2, True)
        rows = self.index.articles("gh_123456789abc")
        self.assertEqual(len(rows), 1)
        self.assertTrue(fingerprint)
        self.index.save_body(rows[0]["article_key"], "正文", {})
        self.assertEqual(self.index.articles("gh_123456789abc")[0]["body_status"], "ok")
        checkpoint = self.index.checkpoint("gh_123456789abc")
        self.assertEqual(checkpoint["next_page"], 2)

    def test_range_uses_beijing_closed_dates(self):
        start, end = mw.parse_date_window("2026-01-01", "2026-01-01")
        self.assertEqual(end - start, 24 * 60 * 60)


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.index = mw.ArchiveIndex(Path(self.temp.name) / "state.sqlite3")
        self.products = {
            mw.SEARCH_SLUG: product(mw.SEARCH_SLUG, 80_000, "/search/accounts"),
            mw.HISTORY_SLUG: product(mw.HISTORY_SLUG, 35_000, "/accounts/articles"),
            mw.CONTENT_SLUG: product(mw.CONTENT_SLUG, 8_000, "/articles/content"),
        }

    def tearDown(self):
        self.index.close()
        self.temp.cleanup()

    def test_recent_one_plan_includes_search_history_and_body(self):
        args = collect_args()
        plan = mw.plan_calls(self.index, args, self.products, "recent", 1, 0, 0)
        self.assertEqual(plan["estimated_max_cost_micros"], 123_000)
        self.assertTrue(plan["needs_account_search"])

    def test_cached_account_removes_search_from_plan(self):
        candidate = {
            "accountName": "示例公众号",
            "username": "gh_123456789abc",
        }
        self.index.save_mapping("示例公众号", candidate, "gh_123456789abc")
        args = collect_args(recent=21, max_pages=2)
        plan = mw.plan_calls(self.index, args, self.products, "recent", 21, 0, 0)
        self.assertEqual(plan["history_pages_max"], 2)
        self.assertEqual(plan["body_calls_max"], 21)
        self.assertEqual(plan["estimated_max_cost_micros"], 238_000)

    def test_metadata_only_has_no_body_calls(self):
        args = collect_args(all=True, recent=0, metadata_only=True, max_pages=3)
        plan = mw.plan_calls(self.index, args, self.products, "all", 0, 0, 0)
        self.assertEqual(plan["body_calls_max"], 0)
        self.assertEqual(plan["estimated_max_cost_micros"], 185_000)


class ArchiveTests(unittest.TestCase):
    def test_archive_contains_no_internal_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            row = {
                "article_key": "a" * 64,
                "title": "示例文章",
                "account_name": "示例公众号",
                "publish_ts": 1_700_000_000,
                "publish_time": "2023-11-15T06:13:20+08:00",
                "article_url": "https://mp.weixin.qq.com/s?x=1",
                "body_text": "正文",
                "body_status": "ok",
            }
            output = mw.write_archive(Path(folder), "示例公众号", [row], {"status": "success"})
            manifest = (output / "manifest.json").read_text(encoding="utf-8")
            self.assertNotIn("collection", manifest.lower())
            self.assertEqual(len(list((output / "articles").glob("*.md"))), 1)


if __name__ == "__main__":
    unittest.main()
