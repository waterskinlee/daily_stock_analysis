# -*- coding: utf-8 -*-
"""Feishu App Bot chunking must respect stock boundaries (split raw, then format)."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from unittest.mock import MagicMock, patch

from src.notification_sender.feishu_sender import FeishuSender


def _make_sender(max_bytes: int = 20000) -> FeishuSender:
    config = SimpleNamespace(feishu_max_bytes=max_bytes)
    sender = FeishuSender.__new__(FeishuSender)
    sender._feishu_max_bytes = max_bytes
    sender._feishu_chat_id = "oc_test"
    return sender


def _stock_block(name: str, filler_words: int = 200) -> str:
    """One full per-stock markdown section with --- separator and subsections."""
    filler = "。".join(f"第{i}句关于{name}的观察" for i in range(filler_words))
    return (
        f"\n---\n\n## ⚪ {name}\n\n"
        f"### 📊 数据透视\n\n"
        f"**趋势**: 空头 | 强度 40/100\n"
        f"| 指标 | 值 |\n|---|---|\n| MA5 | 3.09 |\n| MA20 | 3.21 |\n\n"
        f"**成交量**: 量比 1.11 (缩量回调)\n💡 *{name} 的量能解读一句话。*\n\n"
        f"**筹码结构**: 获利盘 42%\n\n"
        f"{filler}\n"
    )


def test_chunking_keeps_each_stock_whole_in_one_message() -> None:
    """With --- separators intact in the RAW text, a stock's 数据透视 and
    筹码结构 must land in the SAME chunk instead of straddling a boundary.
    """
    content = "头部摘要行\n" + "".join(
        _stock_block(f"股票{i}号") for i in range(1, 6)
    )
    # 头部 + 5 只股票，总量远超 20000 字节 → 必然分批
    assert len(content.encode("utf-8")) > 20000

    sender = _make_sender()
    sent: list[str] = []
    with patch.object(sender, "_app_send_once", side_effect=lambda client, c: sent.append(c) or True), \
            patch.object(sender, "_ensure_app_client", return_value=MagicMock()):
        ok = sender._send_via_app_bot(content)

    assert ok is True
    assert len(sent) > 1
    for i, msg in enumerate(sent):
        # 每条消息都带页标
        assert f"📄 {i + 1}/{len(sent)}" in msg
    # 任一股票的数据透视与筹码结构必须在同一条消息里（不允许跨消息拆段）
    for name in (f"股票{i}号" for i in range(1, 6)):
        holders = [msg for msg in sent if f"数据透视" in msg and name in msg]
        assert holders, f"{name} 未出现在任何批次"
        assert all("筹码结构" in msg for msg in holders), f"{name} 的筹码结构被拆到了别的消息"
