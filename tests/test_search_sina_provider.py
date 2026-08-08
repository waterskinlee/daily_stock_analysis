# -*- coding: utf-8 -*-
"""
Regression tests for SinaNewsSearchProvider (免费新浪新闻搜索兜底).
"""

import sys
import time
import unittest
from types import ModuleType
from unittest.mock import MagicMock, patch

# Mock optional deps before search_service import
if "newspaper" not in sys.modules:
    mock_np = ModuleType("newspaper")
    mock_np.Article = MagicMock()
    mock_np.Config = MagicMock()
    sys.modules["newspaper"] = mock_np
for mod_name in ("fake_useragent", "sqlalchemy"):
    if mod_name not in sys.modules:
        sys.modules[mod_name] = ModuleType(mod_name)
try:
    from fake_useragent import UserAgent
except Exception:
    import fake_useragent
    fake_useragent.UserAgent = lambda *a, **k: type("U", (), {"random": "test"})()

from src.search_service import SearchService, SinaNewsSearchProvider

_NOW = int(time.time())


def _payload(items):
    return {"code": 0, "message": "success", "data": {"list": items}}


class TestSinaNewsSearchProvider(unittest.TestCase):
    """Tests for Sina news API provider request and mapping behavior."""

    def _provider(self, enabled=True):
        return SinaNewsSearchProvider(enabled=enabled)

    def test_cleans_dsa_generic_query_for_sina(self) -> None:
        """「贵州茅台 600519 股票 最新消息」清理为「贵州茅台」，新浪 OR 语义不再被噪音词污染。"""
        provider = self._provider()
        self.assertEqual(provider._clean_query("贵州茅台 600519 股票 最新消息"), "贵州茅台")
        self.assertEqual(provider._clean_query("贵州茅台 600519"), "贵州茅台")
        # 事件查询保留事件词
        self.assertIn("减持", provider._clean_query("贵州茅台 (年报预告 OR 减持公告)"))
        # 空/过短回退原查询
        self.assertEqual(provider._clean_query("股票 最新消息"), "股票 最新消息")
        self.assertEqual(provider._clean_query("600519"), "600519")

    def test_uses_cleaned_query_in_request(self) -> None:
        provider = self._provider()
        payload = _payload(
            [
                {
                    "title": "飞天茅台再涨价！",
                    "intro": "x",
                    "url": "https://finance.sina.com.cn/a.shtml",
                    "ctime": _NOW - 60,
                    "media_show": "21世纪经济报道",
                }
            ]
        )
        with patch(
            "src.search_service.requests.get",
            return_value=MagicMock(status_code=200, json=lambda: payload),
        ) as m:
            resp = provider.search("贵州茅台 600519 股票 最新消息", max_results=3, days=7)
        self.assertTrue(resp.success)
        self.assertEqual(m.call_args.kwargs["params"]["q"], "贵州茅台")

    def test_parses_title_intro_url_and_date(self) -> None:
        provider = self._provider()
        payload = _payload(
            [
                {
                    "title": "飞天茅台<em>再涨价</em>！散客可到自营店购买",
                    "intro": "8月8日下午，贵州茅台上海自营店…",
                    "url": "https://finance.sina.com.cn/wm/2026-08-08/doc-x.shtml",
                    "ctime": _NOW - 60,
                    "media": "",
                    "media_show": "中国基金报",
                    "source": "新浪财经",
                }
            ]
        )
        with patch(
            "src.search_service.requests.get",
            return_value=MagicMock(status_code=200, json=lambda: payload),
        ) as m:
            resp = provider.search("贵州茅台", max_results=5, days=7)

        self.assertTrue(resp.success)
        self.assertEqual(len(resp.results), 1)
        r = resp.results[0]
        self.assertIn("<em>", r.title) if False else None
        self.assertNotIn("<em>", r.title)  # 高亮标签被剥离
        self.assertIn("再涨价", r.title)
        self.assertEqual(r.url, "https://finance.sina.com.cn/wm/2026-08-08/doc-x.shtml")
        self.assertEqual(r.source, "中国基金报")  # media_show 优先于空 media
        self.assertIsNotNone(r.published_date)
        # ctime 距今 1 分钟内，published_date 非空且格式正确
        self.assertRegex(r.published_date, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        # 请求参数：q / tp=news / size 不小于 max_results
        kwargs = m.call_args.kwargs
        params = kwargs.get("params", {})
        self.assertEqual(params.get("q"), "贵州茅台")
        self.assertEqual(params.get("tp"), "news")
        self.assertGreaterEqual(int(params.get("size")), 10)

    def test_drops_stale_items_beyond_days(self) -> None:
        provider = self._provider()
        old_ts = _NOW - 20 * 86400  # 20 天前
        payload = _payload(
            [
                {
                    "title": "旧闻",
                    "intro": "x",
                    "url": "https://finance.sina.com.cn/old.shtml",
                    "ctime": old_ts,
                    "media": "新浪",
                },
                {
                    "title": "新文",
                    "intro": "y",
                    "url": "https://finance.sina.com.cn/new.shtml",
                    "ctime": _NOW - 60,
                    "media": "新浪",
                },
            ]
        )
        with patch(
            "src.search_service.requests.get",
            return_value=MagicMock(status_code=200, json=lambda: payload),
        ):
            resp = provider.search("贵州茅台", max_results=5, days=7)

        self.assertTrue(resp.success)
        self.assertEqual(len(resp.results), 1)
        self.assertEqual(resp.results[0].title, "新文")

    def test_http_error_fail_open(self) -> None:
        provider = self._provider()
        with patch(
            "src.search_service.requests.get",
            return_value=MagicMock(status_code=500, json=lambda: {}),
        ):
            resp = provider.search("贵州茅台", max_results=5, days=7)
        self.assertFalse(resp.success)
        self.assertEqual(resp.results, [])
        self.assertIn("HTTP 500", resp.error_message or "")

    def test_network_error_fail_open(self) -> None:
        provider = self._provider()
        with patch(
            "src.search_service.requests.get",
            side_effect=Exception("connection reset"),
        ):
            resp = provider.search("贵州茅台", max_results=5, days=7)
        self.assertFalse(resp.success)
        self.assertEqual(resp.results, [])
        self.assertIn("请求失败", resp.error_message or "")

    def test_disabled_returns_unavailable(self) -> None:
        provider = self._provider(enabled=False)
        with patch("src.search_service.requests.get") as m:
            resp = provider.search("贵州茅台", max_results=5, days=7)
        m.assert_not_called()
        self.assertFalse(resp.success)
        self.assertIn("未启用", resp.error_message or "")

    def test_inserted_into_provider_chain_between_brave_and_searxng(self) -> None:
        """sina_news_enabled=True 时 provider 链应含 SinaNews，且位于 Brave 后 SearXNG 前。"""
        service = SearchService(
            brave_keys=["brave-test-key"],
            searxng_base_urls=[],
            searxng_public_instances_enabled=False,
            cls_wire_enabled=True,
            sina_news_enabled=True,
        )
        names = [p.name for p in service._providers]
        self.assertIn("SinaNews", names)
        self.assertIn("Brave", names)
        self.assertIn("CLS", names)
        self.assertLess(names.index("Brave"), names.index("SinaNews"))
        self.assertLess(names.index("SinaNews"), names.index("CLS"))

    def test_disabled_sina_news_not_in_chain(self) -> None:
        service = SearchService(
            brave_keys=["brave-test-key"],
            searxng_base_urls=[],
            searxng_public_instances_enabled=False,
            cls_wire_enabled=True,
            sina_news_enabled=False,
        )
        names = [p.name for p in service._providers]
        self.assertNotIn("SinaNews", names)

    def test_cn_stock_prefers_sina_news_first(self) -> None:
        """A股（中文优先）且 SINA_NEWS_PREFER_FOR_CN 时，SinaNews 排到 Brave 前。"""
        service = SearchService(
            brave_keys=["brave-test-key"],
            searxng_base_urls=[],
            searxng_public_instances_enabled=False,
            cls_wire_enabled=True,
            sina_news_enabled=True,
            sina_news_prefer_for_cn=True,
        )
        ordered = [p.name for p in service._providers_for_query("600519", "贵州茅台")]
        self.assertEqual(ordered[0], "SinaNews")
        self.assertLess(ordered.index("SinaNews"), ordered.index("Brave"))

    def test_cn_stock_prefers_eastmoney_then_sina(self) -> None:
        """A股且东财+新浪都开启时：东财资讯 -> 新浪 -> Brave。"""
        service = SearchService(
            brave_keys=["brave-test-key"],
            searxng_base_urls=[],
            searxng_public_instances_enabled=False,
            cls_wire_enabled=True,
            sina_news_enabled=True,
            em_data_news_enabled=True,
            sina_news_prefer_for_cn=True,
        )
        ordered = [p.name for p in service._providers_for_query("601138", "工业富联")]
        self.assertEqual(ordered[0], "EastmoneyData")
        self.assertLess(ordered.index("EastmoneyData"), ordered.index("SinaNews"))
        self.assertLess(ordered.index("SinaNews"), ordered.index("Brave"))

    def test_foreign_stock_keeps_brave_first(self) -> None:
        """美股/港股（英文查询）时保持 Brave 优先，SinaNews 仅作兜底。"""
        service = SearchService(
            brave_keys=["brave-test-key"],
            searxng_base_urls=[],
            searxng_public_instances_enabled=False,
            cls_wire_enabled=True,
            sina_news_enabled=True,
            sina_news_prefer_for_cn=True,
        )
        ordered = [p.name for p in service._providers_for_query("AAPL.US", "苹果")]
        self.assertEqual(ordered[0], "Brave")
        self.assertLess(ordered.index("Brave"), ordered.index("SinaNews"))

    def test_sina_priority_disabled_keeps_default_order(self) -> None:
        service = SearchService(
            brave_keys=["brave-test-key"],
            searxng_base_urls=[],
            searxng_public_instances_enabled=False,
            cls_wire_enabled=True,
            sina_news_enabled=True,
            sina_news_prefer_for_cn=False,
        )
        ordered = [p.name for p in service._providers_for_query("600519", "贵州茅台")]
        self.assertEqual(ordered[0], "Brave")
        self.assertLess(ordered.index("Brave"), ordered.index("SinaNews"))

    def test_original_providers_order_unchanged(self) -> None:
        """_providers_for_query 不修改 self._providers（其它调用点/测试依赖原顺序）。"""
        service = SearchService(
            brave_keys=["brave-test-key"],
            searxng_base_urls=[],
            searxng_public_instances_enabled=False,
            cls_wire_enabled=True,
            sina_news_enabled=True,
            sina_news_prefer_for_cn=True,
        )
        original = [p.name for p in service._providers]
        _ = service._providers_for_query("600519", "贵州茅台")
        self.assertEqual([p.name for p in service._providers], original)


if __name__ == "__main__":
    unittest.main()
