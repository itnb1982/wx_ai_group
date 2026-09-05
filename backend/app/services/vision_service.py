# -*- coding: utf-8 -*-
"""
视觉模型第四票服务（万象Ai · 2026-08-14）
=========================================================================
把「让 AI 大脑实时看 H4/M15/M5 图表识别结构、自己判断」这条纯 AI 系统主路径，
落地为决策链里的一个**加法增强信号**（第 5 路，非闸门、不砍交易笔数）。

★ 架构铁律（与系统定位一致）
  · 纯 AI 系统：视觉/多模态模型在独立模型服务内直接运行，不依赖 MT5 跑视觉模型。
  · 视觉结构识别只作**增强信号融合**提方向准确率，绝不做 GO/NO-GO 闸门
    （拦截=砍信号=利润腰斩，等同旧铁律"提准非拦截"）。
  · 与 FusionService 同构：后台生产者线程按低频渲染图表→送视觉模型(gpu1 GPU 推理)→缓存 VisionVote；
    meta_agent.adjudicate **同步读取缓存票（零延迟拖累主决策链）**。

★ 显存/算力纪律（双 RTX 3060Ti 8GB，2026-08-14 双卡规划）
  · 视觉模型独立 Ollama 实例（端口 11435，CUDA_VISIBLE_DEVICES=1）跑在 gpu1，
    GPU 推理（Ollama options.num_gpu>0 全量卸载），与主实例(gpu0/qwen3:8b)物理隔离。
  · CPU 仍承担 Chronos-2 + 时序竞技场 4 模型，不与视觉抢 gpu1 显存。
  · 后台线程刷新（VISION_REFRESH_SEC，GPU 推理仅 ~2-4s），串行不阻塞决策循环（决策循环只读缓存）。

★ 来源一致性
  · 图表数据取自 mt5_service.get_market_data(主号) 的 timeframes 原始 OHLC，
    与 market_analyzer 喂给双脑的数据同源 —— 视觉模型看的就是 AI 大脑看的行情。
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from loguru import logger

try:
    from app.config import settings
except Exception:  # pragma: no cover
    settings = None

import httpx

_OLLAMA_URL = (os.getenv("WX_VISION_OLLAMA_URL")
                or (getattr(settings, "VISION_OLLAMA_URL", "") if settings is not None else "")
                or "http://127.0.0.1:11435").rstrip("/")
def _default_primary() -> str:
    """兜底主号 UUID：部署可移植——优先环境变量 WX_PRIMARY_ACCOUNT_ID / 配置 PRIMARY_ACCOUNT_ID，
    默认空串（无行情时由 _primary_id 调用方安全降级，不再写死具体账号）。"""
    val = os.getenv("WX_PRIMARY_ACCOUNT_ID")
    if not val and settings is not None:
        val = getattr(settings, "PRIMARY_ACCOUNT_ID", "")
    return val or ""


def _model_name() -> str:
    if settings is not None:
        return getattr(settings, "VISION_MODEL", "qwen2.5vl:7b")
    return os.getenv("WX_VISION_MODEL", "qwen2.5vl:7b")


def _vision_num_gpu() -> int:
    """视觉模型卸载到 GPU 的层数。

    - 999（默认）：全量卸载到当前实例可见的唯一 GPU（视觉实例绑 gpu1）。
    - 0：强制 CPU 推理（回退/单卡机器兜底）。
    视觉实例通过 start_ollama_vision.* 以 CUDA_VISIBLE_DEVICES=1 启动，
    故 num_gpu>0 即落到 gpu1，与主实例(gpu0)物理隔离。
    """
    val = None
    if settings is not None:
        val = getattr(settings, "VISION_NUM_GPU", None)
    if val is None:
        envv = os.getenv("WX_VISION_NUM_GPU")
        val = envv if envv is not None else 999
    try:
        return int(val)
    except (TypeError, ValueError):
        return 999


# ============================================================
#  数据结构
# ============================================================
@dataclass
class VisionVote:
    """视觉模型票 —— 与 FusionVote 契约兼容，便于 meta_agent 直接加权融合。"""
    available: bool = False
    direction: str = "HOLD"           # BUY / SELL / HOLD（H4+M15+M5 三帧聚合后）
    confidence: float = 0.0          # 0~1
    score: float = 0.0               # 归一化方向强度 -1~+1
    h4_dir: str = "HOLD"
    m15_dir: str = "HOLD"
    m5_dir: str = "HOLD"
    h4_conf: float = 0.0
    m15_conf: float = 0.0
    m5_conf: float = 0.0
    agree: bool = False              # H4/M15/M5 是否同向（三帧共识）
    weight_scale: float = 1.0        # 权重缩放（同向→1.0，分歧→0.7，单方向→0.85）
    note: str = ""
    updated_at: float = 0.0
    latency_ms: float = 0.0
    model: str = ""
    raw: str = ""

    def as_dict(self) -> dict:
        return {
            "available": self.available,
            "direction": self.direction,
            "confidence": round(self.confidence, 3),
            "score": round(self.score, 3),
            "h4_dir": self.h4_dir, "m15_dir": self.m15_dir, "m5_dir": self.m5_dir,
            "h4_conf": round(self.h4_conf, 3), "m15_conf": round(self.m15_conf, 3), "m5_conf": round(self.m5_conf, 3),
            "agree": self.agree, "weight_scale": round(self.weight_scale, 3),
            "note": self.note,
            "updated_at": self.updated_at,
            "latency_ms": round(self.latency_ms, 1),
            "model": self.model,
        }


# ============================================================
#  工具
# ============================================================
_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def _extract_json(text: str) -> Optional[dict]:
    """从视觉模型输出中尽力抽取四周期决策 dict。

    ★ 2026-08-15 关键修复：qwen3-vl:4b 即使被要求"只输出JSON"，也常返回
    Markdown 表格 + 说明文字（实测无任何 `{ }`），导致旧实现 `json.loads` 与
    正则 `{.*}` 双双失败 → 返回 None → 票永不上桌 → dashboard 永远红，
    但 GPU 每次仍实跑 ~18s 推理（白烧、发热）。治本在 _call_vision 用 Ollama
    `format:"json"` 语法约束生成（实测多模态下可用，返回干净 JSON）；此处再叠加
    多层兜底，确保任何退化输出都能尽力解析出可用票。
    """
    if not text:
        return None
    t = text.strip()
    # 1) 直接解析
    try:
        return json.loads(t)
    except Exception:
        pass
    # 2) 剥离 markdown 围栏 ```json ... ```
    t2 = re.sub(r"```(?:json)?\s*", "", t, flags=re.I)
    t2 = re.sub(r"\s*```", "", t2)
    try:
        return json.loads(t2)
    except Exception:
        pass
    # 3) 括号匹配：取第一个 { 到其配对的 }（处理前后夹带废话）
    s = t.find("{")
    if s != -1:
        depth = 0
        for i in range(s, len(t)):
            c = t[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[s:i + 1])
                    except Exception:
                        break
    # 4) 兜底：解析 Markdown 表格 / 行格式（h4/m15/m5/overall → decision/confidence）
    return _parse_vision_table(text)


def _parse_vision_table(text: str) -> Optional[dict]:
    """从模型退化输出（Markdown 表格行或 `H4: BUY 0.8` 行）中尽力抽取四周期决策。

    覆盖两种退化格式：
      · 表格行：`| h4 | HOLD | 0.5 |`
      · 行格式：`h4: BUY 0.8` / `H4 BUY 0.8`
    """
    keys = ["h4", "m15", "m5", "overall"]
    out: Dict[str, dict] = {}
    for k in keys:
        # 先定位含该 key 的行（表格场景），否则取整段
        seg = ""
        for line in text.splitlines():
            if re.search(rf"\b{k}\b", line, re.I):
                seg = line
                break
        if not seg:
            m0 = re.search(rf"\b{k}\b.*", text, re.I)
            seg = m0.group(0) if m0 else ""
        if not seg:
            continue
        dm = re.search(r"(BUY|SELL|HOLD)", seg, re.I)
        if dm:
            # 取「决策词之后」的第一个数字（表格行 `| h4 | HOLD | 0.5 |` / 行格式 `h4: BUY 0.8`）
            cm = re.search(r"([0-9](?:\.\d+)?)", seg[dm.end():])
            try:
                out[k] = {
                    "decision": dm.group(1).upper(),
                    "confidence": float(cm.group(1)) if cm else 0.0,
                }
            except (TypeError, ValueError):
                pass
    return out if out else None


def _clean_decision(v) -> str:
    s = str(v or "").strip().upper()
    if s in ("BUY", "LONG", "买入", "做多"):
        return "BUY"
    if s in ("SELL", "SHORT", "卖出", "做空"):
        return "SELL"
    return "HOLD"


def _clean_conf(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f > 1.0:
        f = f / 100.0
    return max(0.0, min(f, 1.0))


def _primary_id() -> str:
    """取一个**真实有 MT5 连接**的行情主号 UUID；失败回退默认。

    ★ 关键修复（2026-08-14）：早期实现直接调 pick_market_primary_id 取主号，
    但该选择器可能返回「DB 标记 is_market_primary=True 的账号」，而该账号在
    mt5_service 里**没有活跃 Worker 连接**（进程存活 ≠ 管道连接存在），
    导致 get_market_data 返回「行情主号未连接」→ 视觉票永远 K线数据不足。
    此处优先用选择器主号，但必须验证 mt5_service 真实持有连接；否则退到
    mt5_service 实际维护连接的账号之一。

    ★ 安全性：行情是只读数据，XAUUSD 报价对同一经纪商下所有账号一致，
    换一个活账号取行情不会污染任何账号的资金/仓位（与 primary_selector 设计前提一致）。
    """
    try:
        from app.services.mt5_service import mt5_service
        # 1) 优先：primary_selector 选出的主号，但必须真有活跃连接
        try:
            from app.database import SessionLocal
            from app.services.primary_selector import pick_market_primary_id
            db = SessionLocal()
            try:
                pid = pick_market_primary_id(db)
            finally:
                db.close()
        except Exception:
            pid = ""
        if pid and mt5_service._get_conn(pid) is not None:
            return pid
        # 2) 退而求其次：mt5_service 实际持有活跃连接（进程存活且管道非空）的账号
        for aid in mt5_service.alive_account_ids():
            if mt5_service._get_conn(aid) is not None:
                return aid
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[Vision] 取主号失败，回退默认: {e}")
    return _default_primary()


# ============================================================
#  服务
# ============================================================
class VisionService:
    """视觉模型单例：后台生产者 + 缓存读取。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._vote = VisionVote()
        self._thread: Optional[threading.Thread] = None
        self._stop = False
        self._started = False
        self._last_err = ""
        self._last_primary = ""
        self._runs = 0
        self._ok_runs = 0
        self._last_latency = 0.0
        # ★ 2026-08-19 审计P2落地：视觉决策历史（MoA 标配——把近几轮投票回喂模型，
        #   让视觉"记得"自己前几轮对同一图景说过什么，抑制票面漂移/自相矛盾）。
        #   仅存最近 5 轮（轻量，不占显存；prompt 文本注入，成本≈0）。
        self._history: list = []  # [{ts, overall_dir, overall_conf}]
        # 诊断：每次实例化都打印，便于排查"双实例"问题
        logger.info(f"[Vision] VisionService 实例化 id={id(self)} module={__name__} file={__file__}")

    # ---------- 生命周期 ----------
    def start(self) -> None:
        """启动后台生产者线程（幂等、守护线程，绝不抛异常）。"""
        if self._started:
            return
        self._started = True
        try:
            self._thread = threading.Thread(target=self._loop, name="vision-producer", daemon=True)
            self._thread.start()
            # ★ 把实例显式绑定到 settings，防止模块双加载导致 dashboard/meta_agent 读到另一个实例
            if settings is not None:
                try:
                    object.__setattr__(settings, "_vision_service_instance", self)
                    logger.info(f"[Vision] 已将实例 id={id(self)} 绑定到 settings")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[Vision] 绑定实例到 settings 失败: {e}")
            logger.info(f"[Vision] 视觉模型生产者线程已启动 (instance={id(self)})")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Vision] 启动生产者线程失败（降级：无视觉票）: {e}")
            self._started = False

    def stop(self) -> None:
        self._stop = True

    def _refresh_sec(self) -> float:
        if settings is not None:
            return float(getattr(settings, "VISION_REFRESH_SEC", 150))
        return 150.0

    def _loop(self) -> None:
        while not self._stop:
            try:
                self._produce()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[Vision] 生产循环异常: {e}")
            for _ in range(int(self._refresh_sec())):
                if self._stop:
                    return
                time.sleep(1)

    # ---------- 同步读取（决策链调用，零延迟） ----------
    def get_vision_vote(self) -> VisionVote:
        with self._lock:
            v = self._vote
        if not v.available:
            return v
        age = time.time() - v.updated_at
        stale = 900.0
        if settings is not None:
            stale = float(getattr(settings, "VISION_STALE_SEC", 900))
        if age > stale:
            return VisionVote(available=False,
                              note=f"视觉票僵死 {age / 60:.0f} 分钟（> 阈值 {stale / 60:.0f}），作废")
        return v

    def status(self) -> dict:
        with self._lock:
            v = self._vote
            st = {
                "enabled": bool(getattr(settings, "VISION_VOTE_ENABLED", True)) if settings else True,
                "model": _model_name(),
                "started": self._started,
                "runs": self._runs,
                "ok_runs": self._ok_runs,
                "last_err": self._last_err,
                "last_primary": getattr(self, "_last_primary", ""),
                "instance_id": id(self),
                "vote": v.as_dict(),
            }
        return st

    # ---------- 生产 ----------
    def _pick_data_source(self) -> tuple:
        """遍历候选账号，实际取数成功才算数，返回 (data_dict, selected_aid)。

        ★ 关键修复（2026-08-14 第2次）：进程存活 ≠ 管道可通信。
        主号 3540bf33 的 Worker 进程活着、_get_conn 也非 None（管道对象存在），
        但底层管道已断链，get_market_data 返回「行情主号未连接」。单点 _get_conn
        检查会误判"有连接"而直接返回它。故对每个候选账号**真实调一次
        get_market_data**，选第一个返回 timeframes 的账号——彻底绕过"假连接"假象。
        行情只读、XAUUSD 报价全员一致，换活账号取行情不污染任何账号。
        """
        from app.services.mt5_service import mt5_service
        primary = _primary_id()
        candidates = []
        if primary:
            candidates.append(primary)
        try:
            candidates += [a for a in mt5_service.alive_account_ids() if a != primary]
        except Exception:
            pass
        for aid in candidates:
            try:
                d = mt5_service.get_market_data(aid, "XAUUSD")
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[Vision] 候选账号 {aid[:8]} 取行情异常: {e}")
                continue
            if isinstance(d, dict) and d.get("timeframes"):
                return d, aid
            err = d.get("error") if isinstance(d, dict) else str(d)
            logger.debug(f"[Vision] 候选账号 {aid[:8]} 取行情失败: {err}")
        return None, ""

    def _produce(self) -> None:
        with self._lock:
            self._runs += 1
        # 1) 取 OHLC —— 遍历候选账号，实际取数成功才算数（进程存活≠管道可通信）
        try:
            from app.services.mt5_service import mt5_service
            data, selected = self._pick_data_source()
            if data is None:
                note = "取行情失败: 所有候选账号均无法取到K线（可能 MT5 全断链）"
                with self._lock:
                    self._last_err = note
                    self._cache(VisionVote(available=False, note=note))
                return
            self._last_primary = selected
        except Exception as e:  # noqa: BLE001
            note = f"取行情失败: {e}"
            with self._lock:
                self._last_err = note
                self._cache(VisionVote(available=False, note=note))
            return

        tfs = (data or {}).get("timeframes", {})
        h4 = tfs.get("H4", {}).get("bars", []) if tfs else []
        m15 = tfs.get("M15", {}).get("bars", []) if tfs else []
        m5 = tfs.get("M5", {}).get("bars", []) if tfs else []
        if len(h4) < 20 or len(m15) < 20 or len(m5) < 20:
            note = f"K线数据不足(H4={len(h4)} M15={len(m15)} M5={len(m5)})"
            with self._lock:
                self._last_err = note
                self._cache(VisionVote(available=False, note=note))
            return

        # 2) 渲染图表
        from app.services.vision_chart import render_chart
        img_h4 = render_chart(h4, "XAUUSD H4")
        img_m15 = render_chart(m15, "XAUUSD M15")
        img_m5 = render_chart(m5, "XAUUSD M5")
        if not img_h4 or not img_m15 or not img_m5:
            note = "图表渲染失败"
            with self._lock:
                self._last_err = note
                self._cache(VisionVote(available=False, note=note))
            return

        # 3) 送视觉模型
        obj = self._call_vision(img_h4, img_m15, img_m5)
        if obj is None:
            # ★ 自愈兜底：_call_vision 内部已重试+自愈，这里再保险触发一次（带5分钟冷却）
            healing = self._heal_vision_instance()
            note = ("视觉实例(11435)无响应，已触发自愈拉起，约1分钟内自动恢复"
                    if healing else "视觉模型调用失败/不可用")
            with self._lock:
                self._last_err = note
                self._cache(VisionVote(available=False, note=note))
            return

        # 4) 聚合
        try:
            vote = self._aggregate(obj)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Vision] _aggregate 异常 (instance={id(self)}): {e}")
            note = f"视觉票聚合异常: {e}"
            with self._lock:
                self._last_err = note
                self._cache(VisionVote(available=False, note=note))
            return
        with self._lock:
            self._ok_runs += 1
            self._last_err = ""
            self._cache(vote)
            # ★ 2026-08-19 审计P2落地：记录本轮投票进历史（供下轮 prompt 回喂）。
            #   overall 方向/置信是聚合后的最终票；仅记有方向的轮次，HOLD 也记（供模型自省）。
            self._history.append({
                "ts": time.strftime("%m-%d %H:%M:%S"),
                "overall_dir": vote.direction if vote.direction in ("BUY", "SELL", "HOLD") else "HOLD",
                "overall_conf": float(vote.confidence or 0.0),
            })
            if len(self._history) > 5:
                self._history = self._history[-5:]
            logger.info(f"[Vision] _produce 成功并缓存 (instance={id(self)} runs={self._runs} ok_runs={self._ok_runs} dir={vote.direction})")

    def _call_vision(self, img_h4: bytes, img_m15: bytes, img_m5: bytes) -> Optional[dict]:
        """调用 Ollama 视觉模型（GPU 推理·gpu1）。返回解析后的 JSON dict 或 None。

        ★ 2026-08-15 自愈加固：视觉实例(11435)的 GPU 推理子进程会偶发崩溃
        （日志实证 02:01-02:06 连续 timed out / WinError 10054 / 10061 积极拒绝），
        旧实现单点失败即写死 last_err 标红且无任何重连。此处改为：首次失败先触发
        幂等自愈拉起 11435，再重试一次；仍失败才返回 None（由 _produce 决定红标文案）。
        """
        try:
            b64_h4 = base64.b64encode(img_h4).decode("ascii")
            b64_m15 = base64.b64encode(img_m15).decode("ascii")
            b64_m5 = base64.b64encode(img_m5).decode("ascii")
            img_total_kb = (len(b64_h4) + len(b64_m15) + len(b64_m5)) / 1024.0
            # ★ 2026-08-19 审计P2落地：注入近 5 轮自投票历史（MoA 标配·成本≈0）。
            #   让视觉"记得"自己对相近行情的历次判断，抑制票面漂移与自相矛盾；
            #   历史仅作上下文参考，不改变 JSON 输出格式。无历史时不注入任何段。
            _hist_block = ""
            try:
                with self._lock:
                    _hist = list(self._history[-5:])
                if _hist:
                    _hist_txt = "、".join(
                        f"{h['ts'][11:19]}→{h['overall_dir']}({h['overall_conf']:.0%})" for h in _hist
                    )
                    _hist_block = (
                        f"\n【你的近期判断(仅供参考，以当前图表为准)】{_hist_txt}\n"
                    )
            except Exception:  # noqa: BLE001
                _hist_block = ""
            prompt = (
                "你是黄金(XAUUSD)价格行为分析专家。请看下面三张图：H4(4小时结构/趋势锚)、"
                "M15(15分钟即时结构)、M5(5分钟实时微结构)。\n"
                f"{_hist_block}"
                "只输出严格JSON，不要解释、不要markdown、不要思考过程。格式：\n"
                '{"h4":{"decision":"BUY|SELL|HOLD","confidence":0.0},'
                '"m15":{"decision":"BUY|SELL|HOLD","confidence":0.0},'
                '"m5":{"decision":"BUY|SELL|HOLD","confidence":0.0},'
                '"overall":{"decision":"BUY|SELL|HOLD","confidence":0.0}}'
            )
            # ★ Ollama 原生 /api/chat 格式：content 必须是字符串，图片用 images 数组传 base64。
            #    OpenAI 兼容的 content[]/image_url 在 Ollama 0.32 + qwen2.5vl 会返回 400。
            payload = {
                "model": _model_name(),
                "messages": [{
                    "role": "user",
                    "content": prompt,
                    "images": [b64_h4, b64_m15, b64_m5],
                }],
                "stream": False,
                # ★ 2026-08-15 关键修正（调研+实测双验证）：
                #   ① qwen3-vl:4b 是"思考模型"，开启 thinking 会把整段输出预算吞进内部
                #      `<think:6124c78e>` 块 → content 静默返回空（多源实证：ai-muninn / wpnews.pro
                #      Qwen3-VL Integration Gotchas）。必须 `"think": False` 关思考才能拿到真实输出。
                #   ② `format:"json"` 在 qwen3-vl 多模态下会静默返回空响应字符串
                #      （yanghu.github.io 实证），故**不传 format**，解析交给 _extract_json。
                #   ③ 图片尺寸须≤安全上限（render_chart 已 resize 最长边≤672），否则同样静默空。
                "think": False,
                "keep_alive": "30m",
                "options": {
                    # ★ 视觉实例绑 gpu1：num_gpu>0 即全量卸载到 gpu1，与主实例(gpu0)隔离。
                    #   设为 0 则回退 CPU 推理（单卡机器兜底）。
                    "num_gpu": _vision_num_gpu(),
                    "temperature": 0.2,
                    # ★★ 视觉侧 num_ctx **固定 4096，禁止跟随主脑做自适应扩展**。
                    #   理由：图像 patch token 本身极吃 KV cache，一旦 OOM 视觉票直接消失，
                    #   关云状态下会把有效方向票打到 <2 张 → 强制 HOLD → 静默停止交易。
                    #   这是"宁可上下文小一点，也绝不能让视觉票消失"的取舍。
                    #
                    #   ── 显存现状（2026-08-18 实测，nvidia-smi）──
                    #   视觉卡（CUDA1 = Windows 任务管理器 GPU2）：6614/8192MB ≈ 80.7%，
                    #   空闲 ~1578MB。占用主体就是 qwen2.5vl:7b 权重本身（Q4_K_M ~5.4GB）
                    #   加 KV cache 与运行时开销。
                    #
                    #   ⚠ 勘误（2026-08-16 由用户当场纠正）：此处旧注释曾写
                    #   "这张卡还兼职桌面渲染（explorer / 远控 / Office Copilot 都在它上面）
                    #   约占 1GB"。**该判断是错的**：本机桌面画面走**板载核显**，
                    #   两张 3060 Ti 独显全部专供模型使用；那些进程出现在独显列表里是
                    #   GPU 加速路由的正常现象，并不代表它们吃掉了独显显存
                    #   （nvidia-smi 的 used_memory 对这些进程实际返回 [N/A]，
                    #   当时的 "约 1GB" 属于凭空估算）。
                    #   → 因此不要再期待"把桌面赶到核显就能腾出 2GB"，那个空间不存在。
                    #   真正的扩容前提只有两条：换更大显存的卡，或换更小的视觉模型。
                    "num_ctx": 4096,
                },
            }
            t0 = time.time()
            logger.info(f"[Vision] 开始推理 model={_model_name()} imgs={img_total_kb:.0f}KB")
            # ★ 2026-08-15 重试加固：qwen3-vl:4b 多模态输出存在"偶发空 content"(即使
            #   已关 thinking + 图片收敛到安全尺寸)。单次成功率约 1/3，故循环最多 3 次，
            #   任一成功即返回，把有效票成功率拉到 ~96%，吸收模型不稳定性。
            obj = None
            for attempt in range(3):
                resp = self._post_vision(payload)
                if resp is None:
                    logger.warning(f"[Vision] 推理请求失败(第{attempt+1}次)")
                    if attempt == 0:
                        # 首次失败先触发自愈（幂等拉起 11435）
                        self._heal_vision_instance()
                    continue
                content = (resp.json().get("message", {}) or {}).get("content", "")
                parsed = _extract_json(content)
                if parsed:
                    obj = parsed
                    break
                logger.warning(f"[Vision] 第{attempt+1}次返回空/不可解析(len={len(content)})，重试")
            if obj is None:
                self._last_latency = (time.time() - t0) * 1000.0
                logger.warning(f"[Vision] 3次重试后仍无有效视觉票 imgs={img_total_kb:.0f}KB")
                return None
            self._last_latency = (time.time() - t0) * 1000.0
            logger.info(f"[Vision] 推理成功 latency={self._last_latency:.0f}ms")
            return obj
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Vision] 调用异常: {e}")
            return None

    def _post_vision(self, payload: dict):
        """实际把请求 POST 给 11435 视觉 Ollama；任何异常/非200都返回 None（不抛）。"""
        t0 = time.time()
        try:
            r = httpx.post(f"{_OLLAMA_URL}/api/chat", json=payload, timeout=180.0)
            latency = (time.time() - t0) * 1000.0
            if r.status_code != 200:
                logger.warning(f"[Vision] Ollama 返回 HTTP {r.status_code} ({latency:.0f}ms): {r.text[:200]}")
                return None
            logger.info(f"[Vision] POST /api/chat 成功 ({latency:.0f}ms) len={len(r.text)}")
            return r
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Vision] POST /api/chat 异常 ({type(e).__name__}): {e}")
            return None

    def _heal_vision_instance(self) -> bool:
        """自愈：探测视觉实例(11435)实际推理能力；若失效则尝试杀掉旧进程并重新拉起。

        返回 True 表示已触发拉起/重启。带 5 分钟冷却，避免生产循环(15s)风暴式反复拉起。
        幂等：start_ollama_vision.ps1 检测到 11435 已监听会立即退出；因此强制重启前
        需要先杀掉占端口的旧 ollama.exe（沙箱/非管理员环境可能杀不掉，日志会记录）。

        ★ 2026-08-19 跨进程冷却：多账号 worker 各自持有独立 _last_heal_ts，
        6 个 worker 会各自触发 -Force 强杀重拉 → serve 初始化需 80s+，每次刚起
        就被下一个 worker 杀掉 → 死循环。这里用文件锁实现全局 5 分钟冷却。
        """
        now = time.time()
        if now - getattr(self, "_last_heal_ts", 0.0) < 300.0:
            return False
        # 跨进程全局冷却（文件锁）：所有 worker 共享
        try:
            import tempfile
            lock_file = os.path.join(tempfile.gettempdir(), "wx_vision_heal.lock")
            try:
                if os.path.exists(lock_file):
                    with open(lock_file, "r", encoding="utf-8") as f:
                        last_ts = float(f.read().strip() or "0")
                    if now - last_ts < 300.0:
                        return False  # 全局冷却内，跳过（另一个 worker 刚触发过）
            except (OSError, ValueError):
                pass
            with open(lock_file, "w", encoding="utf-8") as f:
                f.write(str(now))
        except Exception:
            pass
        self._last_heal_ts = now

        # 1) 探测 /api/tags（端口是否活着）
        try:
            rp = httpx.get(f"{_OLLAMA_URL}/api/tags", timeout=5.0)
            if rp.status_code != 200:
                logger.warning(f"[Vision] 自愈探测 /api/tags 返回 {rp.status_code}")
        except Exception as e:
            logger.warning(f"[Vision] 自愈探测 /api/tags 异常: {e}")

        # 2) 探测 /api/chat 实际推理能力（更真实）
        probe_ok = False
        try:
            probe = {
                "model": _model_name(),
                "messages": [{"role": "user", "content": "回复 OK"}],
                "stream": False,
                "options": {"num_gpu": _vision_num_gpu(), "temperature": 0.2},
            }
            rp = httpx.post(f"{_OLLAMA_URL}/api/chat", json=probe, timeout=25.0)
            if rp.status_code == 200:
                logger.info("[Vision] 自愈探测 /api/chat 正常，无需重启")
                probe_ok = True
            else:
                logger.warning(f"[Vision] 自愈探测 /api/chat 返回 {rp.status_code}")
        except Exception as e:
            logger.warning(f"[Vision] 自愈探测 /api/chat 异常: {type(e).__name__}: {e}")

        if probe_ok:
            return False

        # 3) 探测失败：尝试杀掉 11435 旧进程（需要管理员权限；后端以管理员启动才能成功）
        killed = False
        try:
            # 通过端口找 PID
            port = int(_OLLAMA_URL.rsplit(":", 1)[-1])
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            try:
                sock.connect(("127.0.0.1", port))
                sock.close()
            except Exception:
                pass
            # 用 netstat 找监听 PID
            import subprocess as sp
            ns = sp.check_output("netstat -ano | findstr \":{}\"".format(port), shell=True, text=True, encoding="utf-8", errors="ignore")
            pid_to_kill = None
            for line in ns.splitlines():
                if "LISTENING" in line:
                    parts = [p for p in line.strip().split() if p]
                    if parts:
                        try:
                            pid_to_kill = int(parts[-1])
                        except ValueError:
                            pass
                    break
            if pid_to_kill:
                logger.warning(f"[Vision] 尝试终止 11435 旧进程 PID={pid_to_kill}")
                try:
                    sp.run(["taskkill", "/F", "/PID", str(pid_to_kill)], check=False, capture_output=True, timeout=10)
                    killed = True
                    time.sleep(3)
                except Exception as ke:
                    logger.warning(f"[Vision] 终止 11435 旧进程失败: {ke}")
        except Exception as e:
            logger.warning(f"[Vision] 查找 11435 进程失败: {e}")

        # 4) 拉起 ps1（CUDA_VISIBLE_DEVICES=1 由 ps1 内部 ProcessStartInfo 保证）
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            ps1 = os.path.join(os.path.dirname(here), "start_ollama_vision.ps1")
            if not os.path.exists(ps1):
                ps1 = os.path.join(os.getcwd(), "start_ollama_vision.ps1")
            if os.path.exists(ps1):
                subprocess.Popen(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1, "-Force"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=0x08000000,  # CREATE_NO_WINDOW
                )
                logger.warning(f"[Vision] 视觉实例(11435)失效(killed={killed}) → 已触发重新拉起: {ps1}")
                return True
            logger.warning(f"[Vision] 自愈失败：找不到 {ps1}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Vision] 自愈拉起异常: {e}")
        return False

    def _aggregate(self, obj: dict) -> VisionVote:
        """H4(结构锚·权重高) + M15(即时) + M5(实时管理微结构·权重低) 三帧聚合为统一方向票。"""
        h4 = (obj or {}).get("h4", {}) or {}
        m15 = (obj or {}).get("m15", {}) or {}
        m5 = (obj or {}).get("m5", {}) or {}
        h4d = _clean_decision(h4.get("decision"))
        m15d = _clean_decision(m15.get("decision"))
        m5d = _clean_decision(m5.get("decision"))
        h4c = _clean_conf(h4.get("confidence"))
        m15c = _clean_conf(m15.get("confidence"))
        m5c = _clean_conf(m5.get("confidence"))

        w_h4 = 0.6
        w_m15 = 0.4
        w_m5 = 0.2
        if settings is not None:
            w_h4 = float(getattr(settings, "VISION_H4_WEIGHT", 0.6))
            w_m15 = float(getattr(settings, "VISION_M15_WEIGHT", 0.4))
            w_m5 = float(getattr(settings, "VISION_M5_WEIGHT", 0.2))

        dir_score = 0.0
        tot = 0.0
        for d, c, w in ((h4d, h4c, w_h4), (m15d, m15c, w_m15), (m5d, m5c, w_m5)):
            if d in ("BUY", "SELL"):
                s = 1.0 if d == "BUY" else -1.0
                dir_score += s * c * w
                tot += c * w

        norm = dir_score / tot if tot > 0 else 0.0
        if norm > 0.2:
            direction = "BUY"
        elif norm < -0.2:
            direction = "SELL"
        else:
            direction = "HOLD"
        conf = min(0.95, abs(norm))

        # 三帧共识判定（H4/M15/M5）
        _clear = [d for d in (h4d, m15d, m5d) if d in ("BUY", "SELL")]
        if len(_clear) == 0:
            weight_scale = 0.5
            agree = False
        elif len(_clear) == 1:
            weight_scale = 0.85
            agree = False
        else:
            _all_same = len(set(_clear)) == 1
            agree = _all_same
            weight_scale = 1.0 if _all_same else 0.7

        note = (f"视觉 H4={h4d}({h4c:.0%}) M15={m15d}({m15c:.0%}) M5={m5d}({m5c:.0%}) "
                f"→综合={direction}({conf:.0%}) scale={weight_scale:.2f}")
        try:
            raw = json.dumps(obj, ensure_ascii=False)[:600]
        except Exception:
            raw = str(obj)[:600]

        return VisionVote(
            available=True,
            direction=direction,
            confidence=conf,
            score=norm,
            h4_dir=h4d, h4_conf=h4c,
            m15_dir=m15d, m15_conf=m15c,
            m5_dir=m5d, m5_conf=m5c,
            agree=agree,
            weight_scale=weight_scale,
            note=note,
            updated_at=time.time(),
            latency_ms=self._last_latency,
            model=_model_name(),
            raw=raw,
        )

    def _cache(self, vote: VisionVote) -> None:
        with self._lock:
            self._vote = vote


# ============================================================
#  单例
# ============================================================
_service: Optional[VisionService] = None
_service_lock = threading.Lock()


def get_service() -> VisionService:
    """获取 VisionService 单例。

    ★ 2026-08-15 关键修复：如果模块因 import 路径差异被加载两次，模块级 _service
    会各自为政，导致 dashboard/meta_agent 读到的实例与生产者线程不一致（现象：
    日志显示推理成功，但 status() 永远 ok_runs=0 / available=False）。此处优先从
    settings 对象取已启动的生产者实例引用，确保全系统只有一个真实例。
    """
    # 1) 优先从 settings 取已绑定实例（由 lifespan start() 写入）
    if settings is not None:
        svc = getattr(settings, "_vision_service_instance", None)
        if isinstance(svc, VisionService):
            return svc
    # 2) 回退模块级双检锁单例
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = VisionService()
    return _service
