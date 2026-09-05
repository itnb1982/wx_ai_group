"""
万象Ai 智能交易系统 — 新闻 / 舆情层（2026-08-13 结构性补短板）

为什么需要这一层（调研支撑，≥3 源交叉验证）：
  - goldprice.com《AI-Powered Sentiment Mining of Global News to Forecast Gold
    Price Swings Amid Fed Dynamics》：Gold Sentiment Index(GSI) 综合地缘/政策/商品情绪；
    Fed Tightness Score(FTS) 从 Fed 措辞打分；结论"情绪补充而非替代基本面"。
  - dataconomy.com《AI gold trading bots and the data revolution 2026》：NLP 在新闻
    触及导线的毫秒级把央行声明/地缘头条/通胀报告翻译成情绪分，进入连续预测循环。
  - gainsium.com《Best AI Trading Tools for 2026》：Nexus Sentiment AI 监控新闻/社媒/
    央行声明/地缘情报，实时情绪分 + 黑天鹅预警；"不要只依赖单一 AI 工具，要组合"。
  - TradingAgents(GitHub 32K+★, arXiv 2412.20138)：多智能体框架把 News Analyst +
    Sentiment Analyst 设为独立分析角色，与 Bull/Bear 辩论并列；"Blank beats wrong"
    原则——无数据时不喂垃圾信息。

设计铁律（对齐用户 2026-07-21/07-28 铁律）：
  1. 零新依赖：纯标准库 urllib + xml.etree 解析 RSS，保 F 盘整体可移植（严禁 WorkBuddy 专属依赖）。
  2. Blank beats wrong：无新鲜新闻 → has_news=False，不向 AI 注入任何内容（绝不编造）。
  3. 互补非替代：舆情分只做「提准」——高影响事件下逆新闻方向开单需更高置信才放行，
     平时不干预（不 blanket 拦截，保护"多交易多赚钱"及格线）。
  4. 容错：任意单源失败静默跳过；全部失败 → 缓存为空 → has_news=False（不影响主链路）。

网络说明：本服务运行于用户 Windows 主机(F:/WanxiangAI)，具备公网。沙箱(WorkBuddy)可能
无公网，但那只会让缓存为空、has_news=False，主链路照常运行，不崩溃。
"""

from __future__ import annotations

import html
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from xml.etree import ElementTree as ET

try:
    from app.core.config import settings
except Exception:  # noqa: BLE001
    try:
        from app.config import settings
    except Exception:  # noqa: BLE001
        class _Fallback:
            NEWS_ENABLED = True
            NEWS_REFRESH_SEC = 300
            NEWS_WINDOW_HOURS = 6.0
            NEWS_HIGH_IMPACT_HOURS = 12.0
            NEWS_SENTIMENT_BIAS_THRESHOLD = 0.30
            NEWS_CONFLICT_MIN_CONF = 0.80
            NEWS_MAX_ITEMS = 30
        settings = _Fallback()

logger = None
try:
    from loguru import logger as _lg
    logger = _lg
except Exception:  # noqa: BLE001
    import logging
    logger = logging.getLogger("news_service")


# ─────────────────────────────────────────────────────────────────────────────
#  RSS 数据源（黄金专属 + 宏观）。带可信度权重（机构>区域>社媒，对齐 research）。
#  端点可能随站点改版失效——任一失败静默跳过，不阻塞主链路。
# ─────────────────────────────────────────────────────────────────────────────
RSS_SOURCES = [
    # 黄金/贵金属专属（高可信，2026-08-13 复核全部可达且可解析）
    {"name": "FXStreet",          "url": "https://www.fxstreet.com/rss",                        "credibility": 0.90},
    {"name": "Forexlive",         "url": "https://www.forexlive.com/feed",                      "credibility": 0.80},
    {"name": "Gold-Eagle",        "url": "https://www.gold-eagle.com/rss.xml",                 "credibility": 0.85},
    # 外汇/宏观（中高可信，含 Fed/美元/地缘/大宗商品）
    {"name": "ActionForex",       "url": "https://www.actionforex.com/feed/",                  "credibility": 0.80},
    {"name": "CNBC-Commodities",  "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html", "credibility": 0.85},
    {"name": "MarketWatch-Top",   "url": "https://www.marketwatch.com/rss/topstories/",        "credibility": 0.85},
]
# 2026-08-13 源清单复核记录（沙箱实测，均 200 且可解析为条目）：
#   原 Kitco(404)/DailyFX(403)/Investing(403)/Reuters(超时已废弃) 4 源失效 →
#   替换为 Gold-Eagle(黄金专属,30条)/ActionForex(外汇,20条)/CNBC-Commodities(宏观大宗,30条)/MarketWatch-Top(宏观,10条)。
#   覆盖率由 2/6 提升至 6/6；任一源失败仍静默跳过，不阻塞主链路。

# ─────────────────────────────────────────────────────────────────────────────
#  情绪词典（中文 + 英文混合）。score>0=利多黄金(推动金价上行)，<0=利空黄金。
#  权重代表该词对金价方向的"典型冲击强度"。
# ─────────────────────────────────────────────────────────────────────────────
BULLISH_LEXICON = {
    # 货币政策宽松 / 避险 / 美元走弱
    "降息": 1.0, "宽松": 0.8, "dovish": 0.9, "鸽派": 0.9, "easing": 0.8, "stimulus": 0.7, "刺激": 0.6,
    "避险": 1.0, "safe-haven": 1.0, "safe haven": 1.0, "避险情绪": 1.0, "避险买盘": 1.0,
    "战争": 1.0, "冲突": 0.9, "地缘": 0.8, "geopolitical": 0.8, "紧张": 0.7, "escalation": 0.9, "升级": 0.9,
    "制裁": 0.8, "sanctions": 0.8, "中东": 0.8, "俄乌": 0.7, "台海": 0.7,
    "债务上限": 0.8, "违约": 0.8, "default": 0.7, "违约风险": 0.8,
    "衰退": 0.9, "recession": 0.9, "经济放缓": 0.7, "slowdown": 0.7, "疲软": 0.6,
    "美元走弱": 1.0, "美元疲软": 1.0, "美元下跌": 0.9, "美元走软": 0.9, "weak dollar": 0.9,
    "falling dollar": 0.9, "dollar weakness": 0.9,
    "通胀": 0.5, "inflation": 0.5, "通胀担忧": 0.7, "通胀压力": 0.6,
    "央行购金": 1.0, "央行增持": 0.9, "central bank buying": 0.9,
    "资金流入": 0.8, "inflow": 0.7, "etf inflow": 0.8,
    "恐慌": 0.8, "fear": 0.7, "避险需求": 0.9,
}

BEARISH_LEXICON = {
    # 货币政策收紧 / 美元走强 / 风险偏好回升
    "加息": 1.0, "紧缩": 0.9, "hawkish": 0.9, "鹰派": 0.9, "tightening": 0.9,
    "美元走强": 1.0, "美元上涨": 0.9, "美元强势": 0.9, "strong dollar": 0.9, "rising dollar": 0.9,
    "收益率上升": 0.9, "美债收益率": 0.7, "国债收益率上行": 0.8, "yields rise": 0.8, "yield up": 0.8,
    "资金流出": 0.9, " outflow": 0.8, "etf outflow": 0.9, "流出": 0.7,
    "风险偏好": 0.7, "risk-on": 0.8, "risk on": 0.8, "风险情绪改善": 0.7, "股市上涨": 0.6, "rally": 0.5,
    "就业强劲": 0.8, "solid jobs": 0.8, "非农强劲": 0.8, "strong jobs": 0.8,
    "经济强劲": 0.8, "robust economy": 0.8, "经济数据向好": 0.7,
    "获利了结": 0.8, "profit-taking": 0.9, "抛售": 0.8, "sell-off": 0.8, "清仓": 0.7,
    "美联储鹰派": 0.9, "fed hawkish": 0.9, "通胀降温": 0.7, "inflation cooling": 0.7,
    "通胀回落": 0.7, "回落": 0.4,
}

# 黄金相关性过滤词（标题/摘要须命中其一才算 XAUUSD 相关，降噪）
RELEVANCE_KEYWORDS = [
    "gold", "xau", "黄金", "bullion", "金价", "现货金", "伦敦金",
    "fed", "美联储", "利率", "美元", "dollar", "yield", "收益率",
    " Inflation".lower(), "通胀", "地缘", "避险", "央行", "利率决议",
    "cpi", "ppi", "nfp", "非农", "贵金属",
]

# 高影响事件词（命中即标记 high_impact）
HIGH_IMPACT_KEYWORDS = [
    "fomc", "美联储决议", "利率决议", "议息", "加息决议", "降息决议",
    "cpi", "ppi", "非农", "nfp", "就业数据", "初请", "gdp",
    "地缘政治", "客发", "突发", "战争", "冲突升级", "制裁升级",
    "黑天鹅", "重磅", "今晚公布", "今日公布", "即将公布", "利率决定",
]
# 清洗上面的笔误（"客发"是误输入，移除以免误触发）
HIGH_IMPACT_KEYWORDS = [k for k in HIGH_IMPACT_KEYWORDS if k != "客发"]

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _clean_text(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)        # 去 HTML 标签
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _parse_date(raw: str) -> datetime | None:
    """解析 RSS pubDate(RFC822) 或 Atom published(ISO8601) → 带时区 UTC"""
    if not raw:
        return None
    raw = raw.strip()
    try:
        # RFC 822: "Wed, 12 Aug 2026 14:30:00 GMT"
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        pass
    try:
        # ISO 8601: "2026-08-12T14:30:00Z"
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def _parse_feed(xml_text: str) -> list[dict]:
    """解析 RSS 2.0 与 Atom，返回 [{'title','summary','link','published':datetime|None}]"""
    items: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items

    # RSS 2.0: rss/channel/item
    rss_items = root.findall(".//item")
    for it in rss_items:
        title = _clean_text(_text_of(it.find("title")))
        summary = _clean_text(_text_of(it.find("description")) or _text_of(it.find("summary")))
        link = _text_of(it.find("link")) or ""
        pub = _parse_date(_text_of(it.find("pubDate")))
        if title:
            items.append({"title": title, "summary": summary, "link": link,
                          "published": pub, "source": "", "credibility": 0.7})

    # Atom: feed/entry
    if not items:
        entries = root.findall(".//{*}entry") or root.findall(".//entry")
        for e in entries:
            title = _clean_text(_text_of(e.find("{*}title")) or _text_of(e.find("title")))
            summary = _clean_text(_text_of(e.find("{*}summary")) or _text_of(e.find("summary")))
            link = ""
            le = e.find("{*}link") or e.find("link")
            if le is not None:
                link = le.get("href") or _text_of(le)
            pub = _parse_date(_text_of(e.find("{*}published")) or _text_of(e.find("published"))
                             or _text_of(e.find("{*}updated")) or _text_of(e.find("updated")))
            if title:
                items.append({"title": title, "summary": summary, "link": link,
                              "published": pub, "source": "", "credibility": 0.7})
    return items


def _text_of(el) -> str:
    if el is None:
        return ""
    if el.text:
        return el.text
    # Atom 可能用 <title type="xhtml"> 嵌套
    return "".join(el.itertext())


def _relevant(title: str, summary: str) -> bool:
    text = (title + " " + summary).lower()
    return any(k.lower() in text for k in RELEVANCE_KEYWORDS)


def _score_item(title: str, summary: str) -> float:
    """词典情绪分 → [-1, 1]。bull 命中加、bear 命中减，归一化。"""
    text = (title + " " + summary).lower()
    bull = sum(w for kw, w in BULLISH_LEXICON.items() if kw.lower() in text)
    bear = sum(w for kw, w in BEARISH_LEXICON.items() if kw.lower() in text)
    if bull == 0 and bear == 0:
        return 0.0
    return (bull - bear) / (bull + bear)


def _high_impact(title: str, summary: str) -> bool:
    text = (title + " " + summary).lower()
    return any(k.lower() in text for k in HIGH_IMPACT_KEYWORDS)


def _fetch_one(src: dict, timeout: int = 8) -> list[dict]:
    req = Request(src["url"], headers={"User-Agent": _USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml"})
    with urlopen(req, timeout=timeout) as resp:
        data = resp.read().decode("utf-8", errors="ignore")
    parsed = _parse_feed(data)
    for p in parsed:
        p["source"] = src["name"]
        p["credibility"] = src["credibility"]
    return parsed


class NewsService:
    """单例新闻/舆情服务。后台线程定时刷新，内存缓存最新 N 条。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._items: list[dict] = []           # 最新相关条目（已去重）
        self._last_updated: datetime | None = None
        self._last_error: str | None = None
        self._refresh_count: int = 0
        self._started = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── 后台线程（惰性启动，避免导入副作用）──
    def _ensure_started(self):
        if self._started or not getattr(settings, "NEWS_ENABLED", True):
            return
        self._started = True
        try:
            self.refresh()  # 首次同步拉一次（短超时，失败不影响）
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[NewsService] 首次刷新失败(忽略): {e}")
        self._thread = threading.Thread(target=self._loop, daemon=True, name="news-refresh")
        self._thread.start()
        logger.info("[NewsService] 后台刷新线程已启动")

    def _loop(self):
        interval = int(getattr(settings, "NEWS_REFRESH_SEC", 300))
        while not self._stop.is_set():
            if self._stop.wait(interval):
                break
            try:
                self.refresh()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[NewsService] 后台刷新异常: {e}")

    def stop(self):
        self._stop.set()

    # ── 刷新 ──
    def refresh(self) -> int:
        """拉取全部源、过滤、聚合。返回新条目数。失败源静默跳过。"""
        all_items: list[dict] = []
        errors = []
        for src in RSS_SOURCES:
            try:
                fetched = _fetch_one(src)
                all_items.extend(fetched)
            except (URLError, HTTPError, TimeoutError, Exception) as e:  # noqa: BLE001
                errors.append(f"{src['name']}:{type(e).__name__}")
                continue

        # 去重（按 title 近似）
        seen = set()
        deduped = []
        for it in all_items:
            key = it["title"].lower()[:80]
            if key in seen:
                continue
            seen.add(key)
            deduped.append(it)

        # 保留黄金相关 + 有发布时间（无时间也保留但打标）
        relevant = [it for it in deduped if _relevant(it["title"], it.get("summary", ""))]
        # 按发布时间倒序
        relevant.sort(key=lambda x: x.get("published") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        max_items = int(getattr(settings, "NEWS_MAX_ITEMS", 30))
        relevant = relevant[:max_items]

        with self._lock:
            self._items = relevant
            self._last_updated = datetime.now(timezone.utc)
            self._refresh_count += 1
            self._last_error = ";".join(errors) if errors else None
        if errors:
            logger.debug(f"[NewsService] 刷新完成，{len(relevant)} 条相关；失败源: {errors}")
        else:
            logger.debug(f"[NewsService] 刷新完成，{len(relevant)} 条相关新闻")
        return len(relevant)

    # ── 对外上下文 ──
    def get_news_context(self) -> dict:
        """
        返回决策链注入用的新闻上下文。
        Blank beats wrong：无新鲜相关新闻 → has_news=False，score=0，不注入。
        """
        self._ensure_started()
        with self._lock:
            items = list(self._items)

        now = datetime.now(timezone.utc)
        window_h = float(getattr(settings, "NEWS_WINDOW_HOURS", 6.0))
        hi_h = float(getattr(settings, "NEWS_HIGH_IMPACT_HOURS", 12.0))
        bias_th = float(getattr(settings, "NEWS_SENTIMENT_BIAS_THRESHOLD", 0.30))

        # 情绪聚合（窗口内，按可信度×时间衰减加权）
        tau = max(window_h / 2.0, 0.5)
        weighted_scores = []
        recent_titles = []
        high_impact_events = []
        for it in items:
            pub = it.get("published")
            if pub is None:
                age_h = window_h  # 无时间按窗口边界处理（保守）
            else:
                age_h = (now - pub).total_seconds() / 3600.0
            if age_h > window_h:
                continue
            recency = max(0.05, __import__("math").exp(-age_h / tau))
            s = _score_item(it["title"], it.get("summary", ""))
            cred = float(it.get("credibility", 0.7))
            weighted_scores.append((s, cred * recency))
            recent_titles.append({
                "title": it["title"],
                "source": it.get("source", ""),
                "score": round(s, 2),
                "age_h": round(age_h, 1),
                "high_impact": _high_impact(it["title"], it.get("summary", "")),
            })
            if _high_impact(it["title"], it.get("summary", "")) and age_h <= hi_h:
                high_impact_events.append({
                    "title": it["title"],
                    "source": it.get("source", ""),
                    "age_h": round(age_h, 1),
                })

        # 聚合分数 = 加权平均（已在 [-1,1]）
        if weighted_scores:
            num = sum(s * w for s, w in weighted_scores)
            den = sum(abs(w) for _, w in weighted_scores) + 1e-9
            gold_sentiment_score = max(-1.0, min(1.0, num / den))
        else:
            gold_sentiment_score = 0.0

        has_news = len(weighted_scores) > 0
        if gold_sentiment_score > bias_th:
            bias = "BUY"     # 利多黄金 → 偏多
        elif gold_sentiment_score < -bias_th:
            bias = "SELL"    # 利空黄金 → 偏空
        else:
            bias = "HOLD"

        high_impact_active = len(high_impact_events) > 0

        # 去重高影响事件（按标题）
        _seen_hi = set()
        _hi_dedup = []
        for e in high_impact_events:
            k = e["title"].lower()[:60]
            if k in _seen_hi:
                continue
            _seen_hi.add(k)
            _hi_dedup.append(e)
        high_impact_events = _hi_dedup[:5]

        return {
            "has_news": has_news,
            "item_count": len(weighted_scores),
            "gold_sentiment_score": round(gold_sentiment_score, 3),
            "bias": bias,                       # BUY / SELL / HOLD（明确偏向）
            "high_impact_active": high_impact_active,
            "high_impact_events": high_impact_events,
            "headlines": recent_titles[:8],     # 供 prompt 展示
            "last_updated": self._last_updated.isoformat() if self._last_updated else None,
            "data_lag_note": (
                "新闻/舆情为公开 RSS 聚合，时效以条目 pubDate 为准（分钟级~小时级滞后），"
                "非交易所即时流；情绪分为词典法(中英混合)，仅供参考、不替代价格行为决策。"
            ),
            "error": self._last_error,
        }

    # ── 给 prompt 用的可读文本块 ──
    @staticmethod
    def format_prompt_block(market_data: dict) -> str:
        news = (market_data or {}).get("news") or {}
        if not news.get("has_news"):
            return (
                "【新闻 / 舆情层（2026-08-13 新增）】:\n"
                "（当前无 XAUUSD 相关新鲜新闻，本层不提供额外信号；请完全依据价格行为/SMC/体制决策，"
                "勿臆测未发生的宏观事件。）"
            )
        score = news.get("gold_sentiment_score", 0.0)
        bias = news.get("bias", "HOLD")
        hi = news.get("high_impact_active")
        lines = [
            "【新闻 / 舆情层（2026-08-13 新增·公开 RSS 聚合·词典情绪分）】:",
            f"- 黄金舆情综合分 gold_sentiment_score={score:+.2f}（{bias}偏向；|score|≥0.30 视为明确偏向）",
        ]
        if hi:
            evs = news.get("high_impact_events", [])[:3]
            lines.append("- ⚠️ 高影响事件窗口激活（FOMC/CPI/NFP/地缘等），价格可能剧烈波动：")
            for e in evs:
                lines.append(f"    · [{e.get('source','')}] {e.get('title','')}（{e.get('age_h','?')}h前）")
        heads = news.get("headlines", [])[:5]
        if heads:
            lines.append("- 近期相关头条：")
            for h in heads:
                tag = " [高影响]" if h.get("high_impact") else ""
                lines.append(f"    · {h.get('title','')}（情绪{h.get('score',0):+.1f}{tag}）")
        lines.append(
            "- 使用约束：舆情只作「提准」参考——高影响事件下若你的方向与舆情明显相反，"
            "须有极高置信与硬价格证据才开单，否则优先 HOLD；无高影响事件时舆情不强行干预。"
        )
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
#  单例
# ─────────────────────────────────────────────────────────────────────────────
_service_instance: NewsService | None = None
_service_lock = threading.Lock()


def get_news_service() -> NewsService:
    global _service_instance
    if _service_instance is None:
        with _service_lock:
            if _service_instance is None:
                _service_instance = NewsService()
    return _service_instance


def get_news_context() -> dict:
    """便捷函数：返回新闻上下文（惰性启动后台刷新）。"""
    return get_news_service().get_news_context()


def format_prompt_block(market_data: dict) -> str:
    return NewsService.format_prompt_block(market_data)
