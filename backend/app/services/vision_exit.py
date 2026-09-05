# -*- coding: utf-8 -*-
"""
视觉持仓看护模块（万象Ai · 2026-08-14）
=========================================================================
用户核心诉求：用 AI 模型实时管理订单、判断行情要反转就提前锁利润——
「AI 模型驱动」而非「EA 阈值达标才触发」。

★ 定位（与系统纯 AI 架构一致）
  · 这是用户要的「AI 实时看护仓位」真正落地层：模型看图(多周期蜡烛)识别反转结构
    (CHoCH/吞没/扫流动性/背离)，对每笔持仓独立给出 hold / tighten_sl / partial_close / full_close。
  · 与 vision_service(开仓前第5路增强信号) 解耦：那个负责「方向准不准」，这个负责「持仓怎么管」。
  · 复用 CUDA gpu1（Windows GPU2 第二张3060Ti）的 qwen2.5vl:7b 实例(端口11435)，
    与开仓视觉信号物理共享同一视觉算力，零新增硬件。（2026-08-16 更新：模型 4b→7b；
    GPU 编号口径=Windows 任务管理器视角，详见 config.py 视觉段注释）

★ 架构（后台生产者 + 同步读缓存，零延迟拖累主决策链）
  · ★★ 2026-08-16 铁律（用户纠正）：视觉/AI 模型调用次数**不随账号数 N 线性增长**——
    信号跟随主号、跟单复制主号。跟单账号(follow_leader=True)在 _manage_positions 走
    _mirror_leader_exits 镜像主号出场即 return，**不启动本服务**；只有主号/独立号
    (follow_leader=False) 各启动一个本实例（模块级单例）——6 账号(将来 100 账号)永远
    只有 ~1 次看护推理，结果经 publish_leader_exit 广播给跟单。
  · 后台守护线程按 VISION_EXIT_REFRESH_SEC(默认30s) 低频渲染该主号当前持仓的
    H4/M15/M5 图表(叠加每单开仓/SL/TP 水平线)→ 送视觉模型 → 缓存 ExitVote。
  · trade_executor._manage_positions 同步读取缓存票(零延迟)，作为**最高优先级出场信号**
    覆盖 smart_exit 的 hold；但硬 SL/TP 被扫 与 L3 篮子锁利 仍为最终兜底。

★ 安全铁律（与既有「亏损单保护」一致，绝不违背）
  · 模型只在「持仓已盈利」时建议 full_close/partial_close(锁利) 或 tighten_sl(收紧止损)；
    绝不主动在亏损时平仓——亏损单交给硬止损 + L2 反转确认（避免 AI 把顺势浮亏单砍在地板）。
  · 置信度 < VISION_EXIT_MIN_CONF(默认0.6) 一律 hold（宁可少动，不误杀）。
  · 模型只投「建议票」；真正下单动作仍由 trade_executor 在持仓管理主链路里执行，
    且受硬 SL/TP、max_positions、风险层全程兜底——模型无法越权爆仓。
"""
from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from loguru import logger

try:
    from app.config import settings
except Exception:  # pragma: no cover
    settings = None

import httpx

# 复用开仓视觉信号的 GPU1 实例与模型配置（同一视觉算力，零新增依赖）
try:
    from app.services.vision_service import (
        _OLLAMA_URL as _VISION_OLLAMA_URL,
        _model_name as _vision_model_name,
        _vision_num_gpu as _vision_num_gpu,
    )
except Exception:  # pragma: no cover
    _VISION_OLLAMA_URL = "http://127.0.0.1:11435"
    _vision_model_name = lambda: "qwen2.5vl:7b"  # noqa: E731
    _vision_num_gpu = lambda: 999  # noqa: E731


# ============================================================
#  数据结构
# ============================================================
# 单笔持仓的看护决策
@dataclass
class PositionExitDecision:
    ticket: int = 0
    action: str = "hold"                 # hold / tighten_sl / partial_close / full_close
    confidence: float = 0.0
    new_sl: float = 0.0                  # tighten_sl 时的新止损价
    close_pct: float = 0.0               # partial_close 时的平仓比例 0~1
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "ticket": self.ticket,
            "action": self.action,
            "confidence": round(self.confidence, 3),
            "new_sl": round(self.new_sl, 2),
            "close_pct": round(self.close_pct, 3),
            "reason": self.reason,
        }


@dataclass
class ExitVote:
    available: bool = False
    decisions: List[PositionExitDecision] = field(default_factory=list)
    updated_at: float = 0.0
    latency_ms: float = 0.0
    model: str = ""
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "available": self.available,
            "decisions": [d.as_dict() for d in self.decisions],
            "updated_at": self.updated_at,
            "latency_ms": round(self.latency_ms, 1),
            "model": self.model,
            "note": self.note,
        }


# ============================================================
#  工具
# ============================================================
_JSON_BLOCK = __import__("re").compile(r"\{.*\}", __import__("re").S)


def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    m = _JSON_BLOCK.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _clean_action(v) -> str:
    s = str(v or "").strip().lower()
    for a in ("full_close", "partial_close", "tighten_sl", "hold"):
        if a in s:
            return a
    if "close" in s or "exit" in s:
        return "full_close"
    if "tighten" in s or "sl" in s or "trail" in s:
        return "tighten_sl"
    if "partial" in s:
        return "partial_close"
    return "hold"


def _clean_conf(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f > 1.0:
        f = f / 100.0
    return max(0.0, min(f, 1.0))


def _refresh_sec() -> float:
    if settings is not None:
        return float(getattr(settings, "VISION_EXIT_REFRESH_SEC", 75))
    return 75.0


def _min_conf() -> float:
    if settings is not None:
        return float(getattr(settings, "VISION_EXIT_MIN_CONF", 0.6))
    return 0.6


def _stale_sec() -> float:
    if settings is not None:
        return float(getattr(settings, "VISION_EXIT_STALE_SEC", 300))
    return 300.0


# ============================================================
#  服务
# ============================================================
class VisionExitService:
    """视觉持仓看护单例（每 worker 进程 = 一个账号）。

    provider 约定：callable() -> (positions, market)
      · positions: 该账号当前持仓列表，元素 dict 含
        {ticket, side(BUY/SELL), volume, open_price, sl, tp,
         current_price, profit($), open_time_epoch}
      · market: dict 含 timeframes -> {H4:{bars:[...]}, M15:{...}, M5:{...}}
        与 vision_service 同源(mt5_service.get_market_data)。
    任一为 None/空 → 不评估（降级：无看护票，出场退化为 smart_exit 规则）。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._vote = ExitVote()
        self._thread: Optional[threading.Thread] = None
        self._stop = False
        self._started = False
        self._provider: Optional[Callable[[], tuple]] = None
        self._last_err = ""
        self._last_err_logged = ""  # ★ 2026-08-17 可观测性：已打日志的失败原因，防重复刷屏
        self._runs = 0
        self._ok_runs = 0
        self._last_latency = 0.0

    # ---------- 生命周期 ----------
    def set_provider(self, fn: Callable[[], tuple]) -> None:
        self._provider = fn

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            self._thread = threading.Thread(
                target=self._loop, name="vision-exit-producer", daemon=True
            )
            self._thread.start()
            logger.info("[VisionExit] 视觉持仓看护生产者线程已启动")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[VisionExit] 启动生产者线程失败（降级：无看护票）: {e}")
            self._started = False

    def stop(self) -> None:
        self._stop = True

    def _loop(self) -> None:
        while not self._stop:
            try:
                self._produce()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[VisionExit] 生产循环异常: {e}")
            for _ in range(int(_refresh_sec())):
                if self._stop:
                    return
                time.sleep(1)

    # ---------- 同步读取（持仓管理主链路调用，零延迟） ----------
    def get_vote(self) -> ExitVote:
        with self._lock:
            v = self._vote
        if not v.available:
            return v
        age = time.time() - v.updated_at
        if age > _stale_sec():
            return ExitVote(
                available=False,
                note=f"视觉看护票僵死 {age / 60:.0f} 分钟（> 阈值 {_stale_sec() / 60:.0f}），作废",
            )
        return v

    def status(self) -> dict:
        with self._lock:
            v = self._vote
        return {
            "started": self._started,
            "runs": self._runs,
            "ok_runs": self._ok_runs,
            "last_err": self._last_err,
            "model": _vision_model_name(),
            "vote": v.as_dict(),
        }

    # ---------- 生产 ----------
    def _produce(self) -> None:
        self._runs += 1
        if self._provider is None:
            if self._runs == 1 or self._runs % 10 == 0:
                logger.warning(f"[VisionExit] provider 未注册（runs={self._runs}），看护票不可用")
            return
        try:
            provided = self._provider()
            if not provided or len(provided) != 2:
                if self._runs == 1 or self._runs % 10 == 0:
                    logger.warning(f"[VisionExit] provider 返回异常结构（runs={self._runs}），看护票不可用")
                return
            positions, market = provided
        except Exception as e:  # noqa: BLE001
            self._last_err = f"取持仓/行情失败: {e}"
            # ★ 2026-08-17 可观测性修复：失败路径原为静默（仅 _last_err），
            #   看护不可用无任何日志 → 无法诊断（实测 0 取票日志才发现）。
            #   低频 WARNING（首失败 + 每 10 轮一次），不刷屏。
            if self._last_err_logged != self._last_err or self._runs % 10 == 0:
                self._last_err_logged = self._last_err
                logger.warning(f"[VisionExit] 看护取数失败: {self._last_err}")
            return

        if not positions:
            # 无持仓 → 清空看护票（无需看护）
            self._cache(ExitVote(available=True, decisions=[], updated_at=time.time(),
                                 note="无持仓，无需看护"))
            return

        tfs = (market or {}).get("timeframes", {}) if market else {}
        h4 = tfs.get("H4", {}).get("bars", []) if tfs else []
        m15 = tfs.get("M15", {}).get("bars", []) if tfs else []
        m5 = tfs.get("M5", {}).get("bars", []) if tfs else []
        if len(h4) < 20 or len(m15) < 20 or len(m5) < 20:
            self._last_err = f"K线数据不足(H4={len(h4)} M15={len(m15)} M5={len(m5)})"
            if self._last_err_logged != self._last_err or self._runs % 10 == 0:
                self._last_err_logged = self._last_err
                logger.warning(f"[VisionExit] {self._last_err}，看护票作废")
            self._cache(ExitVote(available=False, note=self._last_err))
            return

        # 渲染图表（叠加每单开仓/SL/TP 水平线）
        from app.services.vision_chart import render_chart
        markers: List[Dict] = []
        for i, p in enumerate(positions):
            try:
                ent = float(p.get("open_price") or 0)
                sl = float(p.get("sl") or 0)
                tp = float(p.get("tp") or 0)
                if ent:
                    markers.append({"price": ent, "label": f"P{i}入", "color": (33, 150, 243)})
                if sl:
                    markers.append({"price": sl, "label": f"P{i}SL", "color": (239, 83, 80)})
                if tp:
                    markers.append({"price": tp, "label": f"P{i}TP", "color": (38, 166, 154)})
            except Exception:
                continue
        img_h4 = render_chart(h4, "XAUUSD H4 (持仓看护)", markers=markers)
        img_m15 = render_chart(m15, "XAUUSD M15 (持仓看护)", markers=markers)
        img_m5 = render_chart(m5, "XAUUSD M5 (持仓看护)", markers=markers)
        if not img_h4 or not img_m15 or not img_m5:
            self._last_err = "图表渲染失败"
            if self._last_err_logged != self._last_err or self._runs % 10 == 0:
                self._last_err_logged = self._last_err
                logger.warning(f"[VisionExit] {self._last_err}，看护票作废")
            self._cache(ExitVote(available=False, note=self._last_err))
            return

        obj = self._call_vision(img_h4, img_m15, img_m5, positions)
        if obj is None:
            self._last_err = "视觉模型调用失败/不可用"
            if self._last_err_logged != self._last_err or self._runs % 10 == 0:
                self._last_err_logged = self._last_err
                logger.warning(f"[VisionExit] {self._last_err}，看护票作废")
            self._cache(ExitVote(available=False, note=self._last_err))
            return

        vote = self._parse(obj, positions)
        self._ok_runs += 1
        self._last_err = ""
        self._last_err_logged = ""
        self._cache(vote)

    def _call_vision(self, img_h4: bytes, img_m15: bytes, img_m5: bytes,
                     positions: List[dict]) -> Optional[dict]:
        """调用 GPU1 视觉模型，返回解析后的 JSON dict 或 None。"""
        try:
            b64_h4 = base64.b64encode(img_h4).decode("ascii")
            b64_m15 = base64.b64encode(img_m15).decode("ascii")
            b64_m5 = base64.b64encode(img_m5).decode("ascii")

            # 持仓上下文（编号从 0 开始，避免模型编造 ticket 整数）
            lines = []
            for i, p in enumerate(positions):
                side = str(p.get("side", "") or "").upper()
                # ★ 2026-08-16 审计P1修复：side 缺失时从 type 兜底（worker 序列化用 type）
                if side not in ("BUY", "SELL"):
                    _pt = str(p.get("type") or "").lower()
                    if _pt in ("buy", "sell"):
                        side = "BUY" if _pt == "buy" else "SELL"
                    else:
                        side = "未知"
                vol = float(p.get("volume") or 0)
                ent = float(p.get("open_price") or 0)
                cur = float(p.get("current_price") or 0)
                sl = float(p.get("sl") or 0)
                tp = float(p.get("tp") or 0)
                prof = float(p.get("profit") or 0)
                mins = 0
                try:
                    ot = float(p.get("open_time_epoch") or 0)
                    if ot > 0:
                        mins = max(0, int((time.time() - ot) / 60))
                except Exception:
                    pass
                lines.append(
                    f"[{i}] {side} {vol:.2f}手 @ {ent:.2f}，当前 {cur:.2f}，"
                    f"SL {sl:.2f}，TP {tp:.2f}，浮动盈亏 {prof:+.2f}美元，已持仓 {mins}分钟"
                )
            pos_text = "\n".join(lines)

            prompt = (
                "你是黄金(XAUUSD)持仓实时风控专家，擅长从蜡烛图识别市场结构来管理持仓。\n"
                "下面三张图：第一张 H4（4小时·趋势/结构背景），第二张 M15（15分钟·即时结构），"
                "第三张 M5（5分钟·实时微结构——**平仓决策的第一优先级视角**），"
                "均含 MA20/MA50 与量能，并叠加了各持仓的开仓/SL/TP 水平线。\n"
                "当前该账号持仓如下（编号从 0 开始）：\n"
                f"{pos_text}\n\n"
                "请基于图表上的价格行为（结构突破 CHoCH、吞没、扫流动性、动量背离、供需区反应）"
                "对**每一笔持仓独立**判断：\n"
                "  · 若图上有清晰的反转结构(与持仓方向相反)且持仓已盈利 → 建议 full_close(立即清掉) "
                "或 partial_close(平一部分锁利，给 close_pct 0~1)，并说明反转依据。\n"
                "  · 若持仓已盈利但出现走弱/反转雏形 → tighten_sl(把止损移到更优位置，给 new_sl，"
                "通常移到保本价或结构位)，降低回吐风险。\n"
                "  · 若趋势结构完好、价格贴着均线运行 → hold(让利润奔跑，不要提前下车)。\n\n"
                "周期优先级铁律（开仓看长周期、**平仓看短周期**）：\n"
                "  · M5/M15 的当下反转/走弱信号**优先于** H4 趋势背景——只要 M5 或 M15 出现清晰的反转结构"
                "（与持仓相反），即使 H4 仍顺原趋势，也应按反转给出 tighten_sl/partial_close/full_close 建议，"
                "不要因为 H4 看涨就无视 M5 的见顶信号。\n"
                "  · 用户实盘视角：价格在短周期已经掉头（跌了/涨不动）就是该动作的信号，不做长周期等待。\n"
                "铁律：\n"
                "1. 绝不主动在亏损时平仓（亏损单交给硬止损与反转确认机制），只可在盈利时锁利或收紧止损。\n"
                "2. 只在结构清晰时给非 hold 动作；结构模糊一律 hold。\n"
                "3. 置信度低于 0.6 一律 hold。\n\n"
                "只输出严格 JSON（不要解释、不要 markdown 围栏），格式：\n"
                '{"decisions":[{"idx":0,"action":"hold|tighten_sl|partial_close|full_close",'
                '"confidence":0.0~1.0,"new_sl":0.0,"close_pct":0.0,"reason":"一句话"}]}'
            )
            payload = {
                "model": _vision_model_name(),
                "messages": [{
                    "role": "user",
                    "content": prompt,
                    "images": [b64_h4, b64_m15, b64_m5],
                }],
                "stream": False,
                "keep_alive": "30m",
                "options": {
                    "num_gpu": _vision_num_gpu(),
                    "temperature": 0.2,
                    "num_ctx": 4096,
                },
            }
            t0 = time.time()
            r = httpx.post(f"{_VISION_OLLAMA_URL}/api/chat", json=payload, timeout=180.0)
            if r.status_code != 200:
                logger.warning(f"[VisionExit] Ollama 返回 HTTP {r.status_code}: {r.text[:200]}")
                return None
            content = (r.json().get("message", {}) or {}).get("content", "")
            self._last_latency = (time.time() - t0) * 1000.0
            return _extract_json(content)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[VisionExit] 调用异常: {e}")
            return None

    def _parse(self, obj: dict, positions: List[dict]) -> ExitVote:
        """把模型 JSON 映射为按 ticket 的看护决策列表（idx→ticket）。"""
        decisions: List[PositionExitDecision] = []
        raw_list = (obj or {}).get("decisions", []) or []
        if not isinstance(raw_list, list):
            raw_list = []
        min_conf = _min_conf()
        ticket_by_idx = {}
        for i, p in enumerate(positions):
            try:
                ticket_by_idx[i] = int(p.get("ticket") or i)
            except Exception:
                ticket_by_idx[i] = i

        for item in raw_list:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("idx", -1))
            except (TypeError, ValueError):
                continue
            if idx not in ticket_by_idx:
                continue
            action = _clean_action(item.get("action"))
            conf = _clean_conf(item.get("confidence"))
            # 低于置信阈值 → 强制 hold（宁可少动）
            if conf < min_conf:
                action = "hold"
            try:
                new_sl = float(item.get("new_sl") or 0)
            except (TypeError, ValueError):
                new_sl = 0.0
            try:
                close_pct = max(0.0, min(1.0, float(item.get("close_pct") or 0)))
            except (TypeError, ValueError):
                close_pct = 0.0
            decisions.append(PositionExitDecision(
                ticket=ticket_by_idx[idx],
                action=action,
                confidence=conf,
                new_sl=new_sl,
                close_pct=close_pct,
                reason=str(item.get("reason", ""))[:200],
            ))

        note = "视觉看护：" + "; ".join(
            f"#{d.ticket}→{d.action}({d.confidence:.0%})" for d in decisions if d.action != "hold"
        ) or "视觉看护：全部 hold"
        return ExitVote(
            available=True,
            decisions=decisions,
            updated_at=time.time(),
            latency_ms=self._last_latency,
            model=_vision_model_name(),
            note=note,
        )

    def _cache(self, vote: ExitVote) -> None:
        with self._lock:
            self._vote = vote


# ============================================================
#  单例
# ============================================================
_service: Optional[VisionExitService] = None
_service_lock = threading.Lock()


def get_service() -> VisionExitService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = VisionExitService()
    return _service
