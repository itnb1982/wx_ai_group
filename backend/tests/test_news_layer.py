"""
新闻 / 舆情层单元测试（2026-08-13）

覆盖：
  1. 词典情绪评分（利多/利空/中性）
  2. 高影响事件识别
  3. XAUUSD 相关性过滤
  4. RSS 2.0 / Atom 解析
  5. get_news_context 聚合（窗口/权重/偏向/高影响）
  6. Blank beats wrong：无新闻 → has_news=False，不注入
  7. format_prompt_block 两种形态
"""

import sys
import os
import math
from datetime import datetime, timezone, timedelta

import pytest

# 确保 backend 根在 sys.path（pytest 根目录约定）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import news_service as ns
from app.services.news_service import NewsService, _score_item, _high_impact, _relevant, _parse_feed


# ─────────────────────────────────────────────────────────────────────────────
#  1. 词典情绪
# ─────────────────────────────────────────────────────────────────────────────
def test_score_bullish_gold():
    s = _score_item("美联储鸽派言论推动避险买盘，美元走弱金价上涨", "")
    assert s > 0, f"应为利多黄金，实际 {s}"


def test_score_bearish_gold():
    s = _score_item("美联储鹰派加息，美元走强收益率上升，金价承压", "")
    assert s < 0, f"应为利空黄金，实际 {s}"


def test_score_neutral():
    s = _score_item("今日天气晴朗，股市平稳", "")
    assert s == 0.0


# ─────────────────────────────────────────────────────────────────────────────
#  2. 高影响事件
# ─────────────────────────────────────────────────────────────────────────────
def test_high_impact_fomc():
    assert _high_impact("今晚公布美联储利率决议，市场密切关注", "") is True


def test_high_impact_cpi():
    assert _high_impact("美国CPI数据即将公布", "") is True


def test_high_impact_none():
    assert _high_impact("黄金技术面出现看张形态", "") is False


# ─────────────────────────────────────────────────────────────────────────────
#  3. 相关性过滤
# ─────────────────────────────────────────────────────────────────────────────
def test_relevant_gold():
    assert _relevant("Gold price surges on safe-haven demand", "") is True
    assert _relevant("黄金突破历史新高", "") is True


def test_relevant_irrelevant():
    assert _relevant("某手机发布会如期举行", "") is False


# ─────────────────────────────────────────────────────────────────────────────
#  4. RSS 解析
# ─────────────────────────────────────────────────────────────────────────────
SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Test Feed</title>
  <item>
    <title>美联储鸽派信号提振黄金避险买盘</title>
    <description>美元走弱，金价上涨</description>
    <link>https://example.com/a</link>
    <pubDate>Wed, 12 Aug 2026 14:30:00 GMT</pubDate>
  </item>
  <item>
    <title>地缘政治紧张推升避险需求</title>
    <description>中东冲突升级</description>
    <link>https://example.com/b</link>
    <pubDate>Wed, 12 Aug 2026 10:00:00 GMT</pubDate>
  </item>
</channel></rss>"""

SAMPLE_ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Test</title>
  <entry>
    <title>美国CPI数据今晚公布，市场严阵以待</title>
    <summary>通胀数据来袭</summary>
    <link href="https://example.com/c"/>
    <published>2026-08-12T09:00:00Z</published>
  </entry>
</feed>"""


def test_parse_rss():
    items = _parse_feed(SAMPLE_RSS)
    assert len(items) == 2
    assert items[0]["title"].startswith("美联储")
    assert items[0]["published"] is not None
    assert items[0]["source"] == ""  # 测试用无 source


def test_parse_atom():
    items = _parse_feed(SAMPLE_ATOM)
    assert len(items) == 1
    assert "CPI" in items[0]["title"]
    assert items[0]["published"] is not None


# ─────────────────────────────────────────────────────────────────────────────
#  5. get_news_context 聚合
# ─────────────────────────────────────────────────────────────────────────────
def _make_service_with(items):
    svc = NewsService()
    svc._ensure_started = lambda: None  # 防后台线程触网
    svc._items = items
    svc._last_updated = datetime.now(timezone.utc)
    return svc


def test_context_bullish_aggregation():
    now = datetime.now(timezone.utc)
    items = [
        {"title": "美联储鸽派言论提振黄金避险买盘", "summary": "美元走弱",
         "source": "Kitco", "credibility": 0.95, "published": now},
        {"title": "地缘政治紧张推升避险需求", "summary": "中东冲突",
         "source": "DailyFX", "credibility": 0.90, "published": now},
    ]
    ctx = _make_service_with(items).get_news_context()
    assert ctx["has_news"] is True
    assert ctx["gold_sentiment_score"] > 0
    assert ctx["bias"] == "BUY"
    assert ctx["item_count"] == 2


def test_context_high_impact_conflict():
    now = datetime.now(timezone.utc)
    items = [
        {"title": "美国CPI数据今晚公布，市场严阵以待", "summary": "投资者密切关注数据结果",
         "source": "FXStreet", "credibility": 0.90, "published": now},
        {"title": "美联储鹰派加息美元走强，金价承压", "summary": "收益率上升",
         "source": "Kitco", "credibility": 0.95, "published": now},
    ]
    ctx = _make_service_with(items).get_news_context()
    assert ctx["high_impact_active"] is True
    assert ctx["bias"] == "SELL"  # 鹰派+利空 → 偏空


def test_context_blank_beats_wrong():
    ctx = _make_service_with([]).get_news_context()
    assert ctx["has_news"] is False
    assert ctx["gold_sentiment_score"] == 0.0
    assert ctx["bias"] == "HOLD"
    assert ctx["high_impact_active"] is False


def test_context_old_news_excluded():
    # 超过窗口的旧闻不计入聚合
    old = datetime.now(timezone.utc) - timedelta(hours=48)
    items = [
        {"title": "美联储鸽派提振黄金", "summary": "美元走弱",
         "source": "Kitco", "credibility": 0.95, "published": old},
    ]
    ctx = _make_service_with(items).get_news_context()
    # 单条且已超窗口 → 不计入加权（item_count=0）→ has_news=False
    assert ctx["item_count"] == 0
    assert ctx["has_news"] is False


# ─────────────────────────────────────────────────────────────────────────────
#  6. format_prompt_block
# ─────────────────────────────────────────────────────────────────────────────
def test_prompt_block_no_news():
    block = NewsService.format_prompt_block({"news": {"has_news": False}})
    assert "无 XAUUSD 相关新鲜新闻" in block


def test_prompt_block_with_news():
    md = {"news": {
        "has_news": True, "gold_sentiment_score": 0.6, "bias": "BUY",
        "high_impact_active": True,
        "high_impact_events": [{"title": "FOMC今晚公布", "source": "FXStreet", "age_h": 2}],
        "headlines": [{"title": "避险买盘推升金价", "score": 0.8, "high_impact": False}],
    }}
    block = NewsService.format_prompt_block(md)
    assert "gold_sentiment_score=+0.60" in block
    assert "高影响" in block
    assert "避险买盘推升金价" in block
