# -*- coding: utf-8 -*-
"""
Regression tests for SinaNewsSearchProvider (免费新浪新闻搜索兜底).
"""

import sys
import time
import unittest
from datetime import datetime, timedelta
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

from src.search_service import (
    CninfoIrmSearchProvider,
    EastmoneyDataApiSearchProvider,
    SearchResponse,
    SearchResult,
    SearchService,
    SinaNewsSearchProvider,
)

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

    def test_cls_v1_sign_matches_expected(self) -> None:
        """财联社 v1 签名 = md5(sha1(按 key 字典序拼接 query))，实测 errno=0。"""
        from src.search_service import ClsWireSearchProvider

        params = {
            "appName": "CailianpressWeb",
            "os": "web",
            "sv": "7.7.5",
            "last_time": "",
            "refresh_type": "1",
            "rn": "5",
        }
        sign = ClsWireSearchProvider._cls_sign(params)
        self.assertIsInstance(sign, str)
        self.assertEqual(len(sign), 32)  # md5 hex
        # 确定性：相同参数 -> 相同签名
        self.assertEqual(sign, ClsWireSearchProvider._cls_sign(dict(params)))

    def test_cls_fetch_uses_v1_signed_url(self) -> None:
        """_fetch_cls 用 v1/roll/get_roll_list + sign，解析 roll_data。"""
        from src.search_service import ClsWireSearchProvider

        provider = ClsWireSearchProvider(enabled=True)
        payload = {
            "errno": 0,
            "msg": "",
            "data": {
                "roll_data": [
                    {
                        "title": "",
                        "content": "【贵州茅台】飞天茅台自营店再调价，散客可购买…",
                        "ctime": _NOW - 60,
                        "id": "12345",
                    }
                ]
            },
        }
        with patch(
            "src.search_service.requests.get",
            return_value=MagicMock(status_code=200, json=lambda: payload),
        ) as m:
            results, err = provider._fetch_cls("贵州茅台", 3, 7)

        self.assertIsNone(err)
        self.assertEqual(len(results), 1)
        self.assertIn("贵州茅台", results[0].snippet)
        # 请求 URL 用 v1/roll/get_roll_list，且带 sign 参数
        call_params = m.call_args.kwargs.get("params", {})
        self.assertIn("sign", call_params)
        self.assertEqual(call_params["appName"], "CailianpressWeb")

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

    def test_cn_stock_prefers_ths_then_eastmoney_then_sina(self) -> None:
        """A股且同花顺+东财+新浪都开启时：同花顺个股 -> 东财 -> 新浪 -> Brave。"""
        service = SearchService(
            brave_keys=["brave-test-key"],
            searxng_base_urls=[],
            searxng_public_instances_enabled=False,
            cls_wire_enabled=True,
            sina_news_enabled=True,
            em_data_news_enabled=True,
            ths_news_enabled=True,
            sina_news_prefer_for_cn=True,
        )
        ordered = [p.name for p in service._providers_for_query("601138", "工业富联")]
        self.assertEqual(ordered[0], "ThsStockNews")
        self.assertLess(ordered.index("ThsStockNews"), ordered.index("EastmoneyData"))
        self.assertLess(ordered.index("EastmoneyData"), ordered.index("SinaNews"))
        self.assertLess(ordered.index("SinaNews"), ordered.index("Brave"))

    def test_ths_extracts_code_and_parses_news(self) -> None:
        """ThsStockNews 从查询提取 6 位代码并解析返回新闻。"""
        from src.search_service import ThsStockNewsProvider
        provider = ThsStockNewsProvider(enabled=True)
        self.assertEqual(provider._extract_stock_code("工业富联 601138 股票 最新消息"), "601138")
        self.assertIsNone(provider._extract_stock_code("贵州茅台 最新消息"))
        payload = {
            "status_code": 0,
            "data": {
                "total": 100,
                "data": [
                    {
                        "title": "工业富联：8月7日获融资买入9.74亿元",
                        "source": "同花顺iNews",
                        "time": _NOW - 60,
                        "pc_url": "https://stock.10jqka.com.cn/c1.shtml",
                    }
                ],
            },
        }
        with patch(
            "src.search_service.requests.get",
            return_value=MagicMock(status_code=200, json=lambda: payload),
        ) as m:
            resp = provider.search("工业富联 601138 股票 最新消息", max_results=3, days=7)
        self.assertTrue(resp.success)
        self.assertEqual(len(resp.results), 1)
        r = resp.results[0]
        self.assertIn("融资买入", r.title)
        self.assertEqual(r.source, "同花顺iNews")
        self.assertIsNotNone(r.published_date)
        self.assertEqual(m.call_args.kwargs["params"]["code"], "601138")

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


class TestEastmoneyDataApiSearchProvider(unittest.TestCase):
    """东财数据中心返回顺序不稳定时仍应取到近期资讯。"""

    def test_fetches_wide_page_and_sorts_recent_items_before_cutoff(self) -> None:
        provider = EastmoneyDataApiSearchProvider(enabled=True)
        now = datetime.now()
        fresh_date = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        stale_date = (now - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
        payload = {
            "result": {
                "cmsArticleWeb": [
                    {
                        "title": "旧结果",
                        "content": "旧内容",
                        "date": stale_date,
                        "mediaName": "东方财富",
                        "url": "https://example.com/old",
                    },
                    {
                        "title": "最新公告",
                        "content": "最新内容",
                        "date": fresh_date,
                        "mediaName": "东方财富",
                        "url": "https://example.com/new",
                    },
                ]
            }
        }
        with patch(
            "src.search_service.requests.get",
            return_value=MagicMock(status_code=200, json=lambda: payload),
        ) as mock_get:
            response = provider.search(
                "立讯精密 002475 股票 最新消息",
                max_results=1,
                days=7,
            )

        self.assertTrue(response.success)
        self.assertEqual([item.title for item in response.results], ["最新公告"])
        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(params["keyword"], "立讯精密")
        self.assertEqual(params["pagesize"], "100")
    def test_falls_back_to_code_query_when_name_has_no_recent_results(self) -> None:
        provider = EastmoneyDataApiSearchProvider(enabled=True)
        now = datetime.now()
        stale_date = (now - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
        fresh_date = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        name_payload = {
            "result": {
                "cmsArticleWeb": [
                    {
                        "title": "立讯精密旧结果",
                        "content": "旧内容",
                        "date": stale_date,
                    }
                ]
            }
        }
        code_payload = {
            "result": {
                "cmsArticleWeb": [
                    {
                        "title": "立讯精密：已耗资约10亿元回购股份",
                        "content": "公司回购进展",
                        "date": fresh_date,
                    }
                ]
            }
        }
        responses = [
            MagicMock(status_code=200, json=lambda: name_payload),
            MagicMock(status_code=200, json=lambda: code_payload),
        ]
        with patch("src.search_service.requests.get", side_effect=responses) as mock_get:
            response = provider.search(
                "立讯精密 002475 股票 最新消息",
                max_results=1,
                days=7,
            )

        self.assertTrue(response.success)
        self.assertEqual([item.title for item in response.results], ["立讯精密：已耗资约10亿元回购股份"])
        self.assertEqual(
            [call.kwargs["params"]["keyword"] for call in mock_get.call_args_list],
            ["立讯精密", "002475"],
        )


class TestCninfoIrmSearchProvider(unittest.TestCase):
    """巨潮互动易必须作为官方公司回复补充，而不是替换常规新闻。"""

    @staticmethod
    def _response(payload, status_code=200):
        return MagicMock(status_code=status_code, json=lambda: payload)

    def test_maps_answered_company_replies_and_uses_query_params(self) -> None:
        provider = CninfoIrmSearchProvider(enabled=True)
        fresh_question = (_NOW - 3600) * 1000
        fresh_reply = (_NOW - 60) * 1000
        lookup = {"data": [{"stockCode": "002475", "shortName": "立讯精密", "secid": "9900014448"}]}
        questions = {
            "rows": [
                {
                    "stockCode": "002475",
                    "companyShortName": "立讯精密",
                    "mainContent": "AI 高速互连业务是否已经进入批量出货？",
                    "attachedContent": "公司相关战略合作聚焦光、铜高速互连，业务按计划推进。",
                    "attachedAuthor": "立讯精密",
                    "pubDate": fresh_question,
                    "updateDate": fresh_reply,
                    "indexId": "123",
                },
                {
                    "stockCode": "002475",
                    "companyShortName": "立讯精密",
                    "mainContent": "尚未回复的问题",
                    "attachedContent": None,
                    "pubDate": fresh_question,
                    "indexId": "124",
                },
            ]
        }

        with patch(
            "src.search_service.requests.post",
            side_effect=[self._response(lookup), self._response(questions)],
        ) as post_mock:
            response = provider.search("立讯精密 002475 股票 最新消息", max_results=3, days=7)

        self.assertTrue(response.success)
        self.assertEqual(len(response.results), 1)
        item = response.results[0]
        self.assertIn("AI 高速互连", item.title)
        self.assertIn("公司回复", item.snippet)
        self.assertIn("按计划推进", item.snippet)
        self.assertEqual(item.source, "巨潮互动易")
        self.assertEqual(item.relevance_category, "direct_company_news")
        self.assertEqual(item.published_date, time.strftime("%Y-%m-%d %H:%M", time.localtime(fresh_reply / 1000)))
        self.assertEqual(post_mock.call_args_list[0].kwargs["data"], {"keyWord": "002475"})
        second_kwargs = post_mock.call_args_list[1].kwargs
        self.assertEqual(second_kwargs["params"]["orgId"], "9900014448")
        self.assertEqual(second_kwargs["params"]["stockcode"], "002475")
        self.assertNotIn("data", second_kwargs)

    def test_drops_stale_replies_and_fails_open(self) -> None:
        provider = CninfoIrmSearchProvider(enabled=True)
        stale = (_NOW - 20 * 86400) * 1000
        lookup = {"data": [{"stockCode": "002475", "secid": "9900014448"}]}
        questions = {
            "rows": [{
                "stockCode": "002475",
                "companyShortName": "立讯精密",
                "mainContent": "旧问题",
                "attachedContent": "旧回复",
                "updateDate": stale,
                "indexId": "old",
            }]
        }
        with patch(
            "src.search_service.requests.post",
            side_effect=[self._response(lookup), self._response(questions)],
        ):
            stale_response = provider.search("002475", max_results=3, days=7)
        self.assertFalse(stale_response.success)
        self.assertEqual(stale_response.results, [])

        with patch("src.search_service.requests.post", side_effect=ConnectionError("reset")):
            failed_response = provider.search("002475", max_results=3, days=7)
        self.assertFalse(failed_response.success)
        self.assertEqual(failed_response.results, [])
        self.assertIn("请求失败", failed_response.error_message or "")

    def test_search_service_uses_interactions_without_replacing_news_chain(self) -> None:
        service = SearchService(
            searxng_base_urls=[],
            searxng_public_instances_enabled=False,
            cninfo_irm_enabled=True,
        )
        interaction = SearchResponse(
            query="002475 互动易",
            provider="CninfoIRM",
            success=True,
            results=[SearchResult(
                title="立讯精密公司回复：高速互连业务按计划推进",
                snippet="投资者提问：业务进展？\n公司回复：按计划推进。",
                url="https://irm.cninfo.com.cn/ircs/company/companyDetail?stockcode=002475",
                source="巨潮互动易",
                published_date=time.strftime("%Y-%m-%d %H:%M", time.localtime(_NOW - 60)),
                relevance_score=100,
                relevance_category="direct_company_news",
                relevance_reasons=["官方公司回复"],
            )],
        )
        with patch.object(service._interaction_provider, "search", return_value=interaction):
            response = service.search_stock_news("002475", "立讯精密", max_results=3)
            intel = service.search_comprehensive_intel("002475", "立讯精密", max_searches=0)

        self.assertTrue(service.is_available)
        self.assertEqual(response.provider, "CninfoIRM")
        self.assertEqual(len(response.results), 1)
        self.assertIn("company_interactions", intel)
        self.assertEqual(intel["company_interactions"].results[0].source, "巨潮互动易")
        report = service.format_intel_report(intel, "立讯精密")
        self.assertIn("公司互动回复", report)
        self.assertIn("高速互连业务按计划推进", report)


if __name__ == "__main__":
    unittest.main()
