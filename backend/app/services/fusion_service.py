# -*- coding: utf-8 -*-
"""
时序融合服务（Time-Series Fusion Service） —— fusion_v2 决策架构的「第四票」。

══════════════════════════════════════════════════════════════════════
★ 设计定位（用户 2026-08-10 决策：4 个时序模型结果实时喂进方向门）：
   把信号参考面板已经在跑的 4 个本地时序模型
   （Chronos-2 / TimesFM-2.5 / Time-MoE / Moirai）
   的实时推理结果【聚合】成一个统一的「时序融合票」，
   作为 Meta-Agent 加权投票中与 DeepSeek / 混元 / 单 Chronos 并列的第四票。

★ 显存友好：本服务【只读】 TSReferenceService 的 snapshot（后台每 5 分钟刷新、
   已占用推理资源），【不】自己加载任何模型、不重复跑 GPU 推理 → 8GB 显卡零额外负担。

★ 不违反参考面板红线：红线是「参考面板不得 import 决策链」，
   本模块属决策链、反向读取参考面板的产出，单向依赖，不影响解耦测试。

★ 多客户隔离：融合票与账号无关（行情是全局的），所有账号统一享用同一融合票，
   每个账号仍按自己的策略/手数/风控独立执行 —— 符合「多客户并行、账号数不写死」。
══════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from loguru import logger

from app.config import settings  # ★ 2026-08-17：TS_FUSION_REVERSE_MIN_SHARE 锚定分级阈值

# 融合方向强度阈值：归一化方向得分绝对值 > 此值才视为有效方向（类比 Chronos diff_pct>0.3）
TS_FUSION_MIN_CONSENSUS = 0.30
# 单模型方向数值化
_DIR_MAP = {"BUY": 1.0, "SELL": -1.0, "HOLD": 0.0, "LONG": 1.0, "SHORT": -1.0}

# ★★ 安全门（2026-08-10 补，2026-08-15 审计P2修正）：参考面板刷新周期 REFRESH_SEC=300s，
#    单模型最长 75s，正常一轮 ≤ 375s。
#    原 180s 阈值 < 300s 刷新周期 → 每次刷新间隙有 ~120s 融合票被误判「僵死作废」、
#    静默回退单 Chronos（约 40% 时间第四票缺席，关云置信口径随之切换）。
#    修正为 360s（= 300s 一轮 + 60s 余量）：真僵死（>6 分钟不刷新）才作废；
#    陈旧方向风险由「Chronos 锚 + 单模型时效」兜底，正常周期内融合票全程可用。
TS_FUSION_MAX_STALE_SEC = 360.0

# ★ 宏观镜像偏置权重（2026-08-12 审计修复）：
# 关云模式下宏观镜像(DXY/VIX)此前只接云端大脑→整条本地决策链断链。
# 此处把宏观偏置温和叠加进融合票方向强度，使关云方向权威也能参考宏观镜像。
# 仅作修正，4 模型价格共识仍主导；权重 0.30 保证宏观不喧宾夺主、不变成新的过滤/反转机制。
MACRO_BIAS_WEIGHT = 0.30


@dataclass
class FusionVote:
    """时序融合票 —— 与 NumpyDirectionGuard / Chronos 输出契约一致，便于 MetaAgent 直接用。"""
    available: bool = False          # 是否有 ≥1 个可用模型参与融合
    direction: str = "HOLD"          # BUY / SELL / HOLD（融合后）
    confidence: float = 0.0          # 融合置信 0~1
    score: float = 0.0               # 归一化方向强度 -1~+1
    lo: Optional[float] = None       # 4 模型预测下界交集（最保守）
    hi: Optional[float] = None       # 4 模型预测上界交集（最保守）
    agree: bool = False              # 所有 directional 模型是否同向
    hit_rate_avg: float = 0.0        # 参与模型近期命中率均值（质量信号）
    per_model: List[Dict] = field(default_factory=list)   # 各模型原始票（透明化/审计）
    model_count: int = 0             # 参与融合的可用模型数
    weight_scale: float = 1.0        # 权重缩放系数（参与模型越多、越同向 → 越接近 1.0）
    note: str = ""


class FusionService:
    """时序融合服务单例。只读 TSReferenceService snapshot，聚合出第四票。"""

    def __init__(self):
        self._last_vote: Optional[FusionVote] = None
        self._last_at: float = 0.0
        self._cache_ttl = 5.0  # 融合票缓存 5s·2026-08-14 提速：30→5（snapshot 仍 5 分钟推理，缓存只让决策更快读到最新聚合）

    def _get_reference_snapshot(self) -> Optional[dict]:
        """反向读取信号参考面板单例的 snapshot（只读，不触发任何推理）。"""
        try:
            from app.services.ts_reference_service import get_service
            return get_service().get_snapshot()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Fusion] 读取参考面板 snapshot 失败: {e}")
            return None

    def get_fusion_vote(self) -> FusionVote:
        """聚合 4 时序模型 → 统一融合票。带 30s 缓存。"""
        now = time.time()
        if self._last_vote is not None and (now - self._last_at) < self._cache_ttl:
            return self._last_vote

        snap = self._get_reference_snapshot()
        vote = self._aggregate(snap)
        self._last_vote = vote
        self._last_at = now
        return vote

    def _macro_bias(self) -> Optional[float]:
        """从宏观镜像(DXY/VIX)推导黄金方向偏置 ∈ [-1,1]；>0偏多 <0偏空；None=无数据降级。

        DXY 与黄金强负相关(≈-0.65)：DXY 走强→黄金偏空。把「外部基本面语境」浓缩为一个
        方向偏置，温和叠加进融合票，使关云模式的方向权威也能参考宏观镜像。
        仅作修正(MACRO_BIAS_WEIGHT)，绝不喧宾夺主或变成新的硬翻向/过滤机制。
        """
        try:
            from app.services.market_data import market_data_provider
            ext = market_data_provider.get_external_snapshot()
            dxy = ext.get("dxy") or {}
            corr = ext.get("correlation") or {}
            vix = ext.get("vix") or {}
            # 当日 DXY 变化（相关性采样里的 1d 变化优先，否则用 DXY 自身涨跌幅）
            dxy_chg = float(corr.get("dxy_change_1d") or dxy.get("change_pct") or 0.0)
            # 黄金-DXY 长期相关系数；异常正相关(≥0)不采信，回落到常识 -0.65
            base_corr = float(corr.get("correlation_20d") or -0.65)
            if base_corr >= 0:
                base_corr = -0.65
            # DXY 走强(dxy_chg>0)→黄金偏空(bias 负)；tanh 压缩到合理幅度
            bias = -math.tanh(dxy_chg / 0.5) * min(1.0, abs(base_corr))
            # VIX 高(风险厌恶)→黄金避险偏多，部分抵消
            vix_p = float(vix.get("price") or 0.0)
            if vix_p > 25:
                bias += 0.10
            bias = max(-1.0, min(1.0, bias))
            if abs(bias) < 0.01:
                return None
            return bias
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Fusion] 宏观镜像偏置获取失败(降级不加): {e}")
            return None

    def _aggregate(self, snap: Optional[dict]) -> FusionVote:
        if not snap:
            return FusionVote(available=False, note="参考面板 snapshot 不可达")

        # ── 安全门 1：合成行情绝不进决策链 ────────────────────────
        # 参考面板在 MT5 掉线/休市时会用合成行情兜底（load_live_rates 返回 live=False），
        # 那种情况下模型方向是「对假数据的预测」，进决策链等于让 AI 照着噪声下单。
        # 参考面板作为「观测」可以用合成数据展示，决策链绝不可以。
        if snap.get("live") is False:
            return FusionVote(
                available=False,
                note="参考面板当前为合成行情(live=False)，融合票不参与决策",
            )

        # ── 安全门 2：快照僵死作废 ────────────────────────────────
        updated_at = float(snap.get("updated_at") or 0.0)
        if updated_at <= 0:
            return FusionVote(available=False, note="参考面板快照无更新时间戳")
        age = time.time() - updated_at
        if age > TS_FUSION_MAX_STALE_SEC:
            return FusionVote(
                available=False,
                note=f"参考面板快照已僵死 {age/60:.1f} 分钟（阈值 {TS_FUSION_MAX_STALE_SEC/60:.0f} 分钟），融合票作废",
            )

        models = snap.get("models") or []
        if not models:
            return FusionVote(available=False, note="参考面板暂无模型数据")

        usable = []
        for m in models:
            d = str(m.get("direction", "HOLD")).upper()
            if d not in _DIR_MAP:
                continue
            if not m.get("available"):
                continue
            usable.append(m)

        if not usable:
            return FusionVote(
                available=False,
                note="4 个时序模型本轮均不可用（TIMEOUT/ERROR/待安装）",
                per_model=[{k: m.get(k) for k in ("name", "direction", "available")} for m in models],
                model_count=0,
            )

        # ── 安全门 3：单模型不叫「融合」 ──────────────────────────
        # 只有 1 个模型可用时，融合票退化为单模型票，其可靠性并不优于
        # 已被验证过的单 Chronos 路径，反而因换了权重口径引入未知风险。
        # → 直接判不可用，让 MetaAgent 回退 legacy 单 Chronos（久经验证的路径）。
        if len(usable) < 2:
            _only = usable[0].get("name") if usable else "?"
            return FusionVote(
                available=False,
                note=f"仅 {_only} 一个模型可用，不构成融合（回退单Chronos）",
                per_model=[{k: m.get(k) for k in ("name", "direction", "available")} for m in models],
                model_count=len(usable),
            )

        # ── Chronos 方向锚（2026-08-14 升级·科学锚定加权）──
        # 调研依据（≥3 源交叉验证，非拍脑袋）：
        #  ① ScienceDirect 约束优化集成——最优权重压低弱模型、放大强模型（弱模型权重可=0.00）
        #  ② TimeSeriesScientist Performance-Aware Averaging——权重按逆验证损失，弱模型被 clip 缩小
        #  ③ GUARD(KDD2026) Uncertainty-Gated Routing——teacher 置信与领域背离时自动衰减(circuit-breaker)
        #  ④ PapersWithCode TIME-2026：Chronos-2 CRPS 0.55 居 #1，优于 TimesFM-2.5(0.56)/Moirai-2.0(0.58)
        #  ⑤ 本系统 2026-08-13 实证：四时序模型净点数全负且劣于最佳单模型
        # 结论：Chronos 是四模型里唯一经实战验证的方向主脑，必须作「锚」——
        #   · 锚有方向 → 弱模型仅「同向确认」（反向权重置0，不拉偏方向权威）
        #   · 锚观望   → 融合票直接 HOLD，绝不让三弱票单独拍板（meta_agent 回退单 Chronos 路径，不拦截其他路开单）
        _chr = next((m for m in usable if "chronos" in str(m.get("name", "")).lower()), None)
        _chr_dir = str(_chr.get("direction", "HOLD")).upper() if _chr is not None else "HOLD"

        # ── 加权聚合（质量权重 = 近期命中率，缺省 0.5；锚定反向置零）──
        # ★ 2026-08-17：先预计算各方向原始质量权重占比（锚定分级用），
        #   再进入聚合循环——若在循环内逐票累加，处理反向票时占比还没算完
        #   （如 2v2 里第一张 SELL 票出现时 SELL 总权仍=0）→ 误判为少数派误沉默。
        _pre_qw = {"BUY": 0.0, "SELL": 0.0}
        _single_anchor = bool(getattr(settings, "TS_FUSION_SINGLE_ANCHOR", True))
        for m in usable:
            d = str(m.get("direction", "HOLD")).upper()
            if d not in ("BUY", "SELL"):
                continue
            # ★ 定稿P0-1 单锚化：非锚模型不参与任何加权预计算（仅展示）
            if _single_anchor and "chronos" not in str(m.get("name", "")).lower():
                continue
            hr = m.get("hit_rate")
            qw0 = max(0.1, min(1.0, float(hr))) if hr is not None else 0.5
            _pre_qw[d] += qw0
        _pre_total = _pre_qw["BUY"] + _pre_qw["SELL"] + 1e-9
        _pre_share = {k: v / _pre_total for k, v in _pre_qw.items()}

        dir_score = 0.0
        total_qw = 0.0
        los, his = [], []
        per_model = []
        dir_values = []
        raw_dirs = []   # ★ 2026-08-17：全部可用模型的原始方向（含锚定沉默票），供 agree 判断
        for m in usable:
            d = str(m.get("direction", "HOLD")).upper()
            dn = _DIR_MAP.get(d, 0.0)
            conf = float(m.get("confidence") or 0.0)
            _is_chronos = "chronos" in str(m.get("name", "")).lower()
            # ★ 定稿P0-1 单锚化：非锚模型完全观测化（qw=0 不参与加权/agree），
            #   仅保留 per_model 展示供参考面板；锚观望分支逻辑保持不变。
            if _single_anchor and not _is_chronos:
                per_model.append({
                    "name": m.get("name"),
                    "direction": d,
                    "confidence": round(conf, 4),
                    "hit_rate": round(float(m.get("hit_rate")), 3) if m.get("hit_rate") is not None else None,
                    "qw": 0.0,
                    "lo": m.get("lo"), "hi": m.get("hi"),
                    "mode": "observe",
                })
                continue
            # 质量权重：命中率优先；缺失时中性 0.5（沿用 2026-08-12 修复，避免幅度劫持）
            hr = m.get("hit_rate")
            if hr is not None:
                qw = max(0.1, min(1.0, float(hr)))
            else:
                qw = 0.5
            if d in ("BUY", "SELL"):
                raw_dirs.append(d)
            # ★ 锚定规则（★ 2026-08-17 分级修复）：非 Chronos 模型，若锚已有方向且与锚反向
            #   → 反向票「彻底沉默」会让两件事失真：
            #     ① 2BUY vs 2SELL 势均力敌也单边跟锚（测试契约：必须 HOLD 不瞎选）；
            #     ② 分歧票从 dir_values 消失 → agree 误判全同向 → 分歧不打折。
            #   修复：反向票质量权重占比 ≥ 40% 时（势均力敌/强反对），不沉默——让它们
            #   参与聚合，norm≈0 自然 HOLD；仅当反向是少数派(<40%)才沉默（锚主导不拉偏）。
            if (not _is_chronos) and _chr_dir in ("BUY", "SELL") and d in ("BUY", "SELL") and d != _chr_dir:
                _opp = "SELL" if _chr_dir == "BUY" else "BUY"
                if _pre_share[_opp] < float(getattr(settings, "TS_FUSION_REVERSE_MIN_SHARE", 0.40)):
                    qw = 0.0
                    dn = 0.0
            # ★ 2026-08-19 审计P1落地：同源冗余票降权（相关性去冗余）。
            #   实证：4 个时序模型喂同一根 M15 收盘价序列（ts_reference_models 共用 load_live_rates），
            #   方向高度同源 → "4 票聚合"实为"1 票的 4 个近似副本"，强相关票重复暴露同一因子。
            #   业界（18-agent 框架/HedgeAgents）：高相关智能体应降权去冗余，避免过度暴露同因子。
            #   落地：非锚模型与锚 Chronos 同向，但其命中率显著低于锚（净点更差）时，
            #   视为"弱副本票"，质量权重再 × REDUNDANCY_DISCOUNT（默认 0.5），防止弱副本与锚同权。
            #   锚本身不动；反向票已在上面置零；锚观望分支（HOLD）不受影响。
            if (not _is_chronos) and _chr is not None and d == _chr_dir and d in ("BUY", "SELL"):
                try:
                    _chr_hr = float(_chr.get("hit_rate") or 0.5)
                    _self_hr = float(hr) if hr is not None else 0.5
                    if _chr_hr > 0 and _self_hr < _chr_hr:
                        _rd = float(getattr(settings, "TS_FUSION_REDUNDANCY_DISCOUNT", 0.50))
                        qw *= _rd
                except Exception:  # noqa: BLE001
                    pass
            dir_score += dn * qw * conf
            total_qw += qw * (conf if conf > 0 else 0.3)  # 置信过低时给最小权重防除零
            lo = m.get("lo")
            hi = m.get("hi")
            if lo is not None:
                los.append(float(lo))
            if hi is not None:
                his.append(float(hi))
            if dn != 0.0:
                dir_values.append(dn)
            per_model.append({
                "name": m.get("name"),
                "direction": d,
                "confidence": round(conf, 4),
                "hit_rate": round(float(hr), 3) if hr is not None else None,
                "qw": round(qw, 3),
                "lo": lo, "hi": hi,
            })
        # hit_avg 提前算（供下方锚观望分支透出质量信号）
        # ★ 2026-08-19 定稿P0-1 单锚化修正：只取**参与投票**模型的命中率——
        #   单锚化时非锚模型已观测化(qw=0)，其命中率（竞技场全负/可能为 0）拉低均值
        #   会让融合票命中率跌破 TS_FUSION_HIT_FLOOR(0.45) 被地板误杀。
        if _single_anchor and _chr is not None:
            _chr_hr = _chr.get("hit_rate")
            hit_rates = [float(_chr_hr)] if _chr_hr is not None else []
        else:
            hit_rates = [float(m.get("hit_rate")) for m in usable if m.get("hit_rate") is not None]
        hit_avg = (sum(hit_rates) / len(hit_rates)) if hit_rates else 0.0

        # ── 锚观望 → 融合票直接 HOLD（2026-08-14·不替锚拍板）──
        # 解释：锚（Chronos）自己都没方向，说明本地时序无可靠信号；此时让 TimesFM/Time-MoE/Moirai
        # 三弱票单独决定方向，比不用更危险（实证净点数全负）。HOLD 后 meta_agent 走 _fv.direction
        # 非 BUY/SELL 分支，chronos_dir 回退单 Chronos（来自 meta_quality），不拦截 DeepSeek/混元/视觉开单。
        if _chr_dir not in ("BUY", "SELL"):
            # ★ 2026-08-15 P2-6 修复：Chronos 锚观望→融合票静默 HOLD，第三票可能无声消失。
            #   补降级告警，便于审计「仅 DeepSeek/混元/视觉双票在跑、Chronos 未贡献」的时段。
            logger.warning(
                f"[fusion] Chronos 锚方向=_chr_dir({_chr_dir})，融合票回退 HOLD；"
                f"当前可用模型={len(usable)}，竞技场方向未获锚确认（meta 回退单 Chronos）。"
            )
            return FusionVote(
                available=True,
                direction="HOLD",
                confidence=0.0,
                score=0.0,
                lo=max(los) if los else None,
                hi=min(his) if his else None,
                agree=False,
                hit_rate_avg=round(hit_avg, 3),
                per_model=per_model,
                model_count=len(usable),
                weight_scale=0.0,
                note=(f"Chronos观望({_chr_dir})→融合票不替锚拍板(HOLD);"
                      f"{len(usable)}模型竞技场方向未获锚确认(回退单Chronos)"),
            )

        norm = (dir_score / total_qw) if total_qw > 0 else 0.0
        if norm > TS_FUSION_MIN_CONSENSUS:
            direction = "BUY"
        elif norm < -TS_FUSION_MIN_CONSENSUS:
            direction = "SELL"
        else:
            direction = "HOLD"

        confidence = min(0.98, abs(norm))

        # ── 宏观镜像偏置（2026-08-12 审计修复）──
        # 关云模式下宏观镜像(DXY/VIX)此前只接云端大脑→整条本地决策链断链。
        # 此处把宏观偏置温和叠加进融合票方向强度，使关云方向权威也能参考宏观镜像。
        # 仅作修正(权重0.30)，4模型价格共识仍主导；不触发任何安全门、不变成硬翻向。
        _macro = self._macro_bias()
        if _macro is not None:
            norm = norm + _macro * MACRO_BIAS_WEIGHT
            if norm > TS_FUSION_MIN_CONSENSUS:
                direction = "BUY"
            elif norm < -TS_FUSION_MIN_CONSENSUS:
                direction = "SELL"
            else:
                direction = "HOLD"
            confidence = min(0.98, abs(norm))

        # ── 锚定兜底（2026-08-14·circuit-breaker）──
        # 宏观偏置等修正项最多把方向压成 HOLD，绝不允许把方向「翻」向锚的反面——
        # 否则弱信号/宏观会劫持经实战验证的 Chronos 锚（GUARD KDD2026 同款断路逻辑）。
        if _chr_dir in ("BUY", "SELL") and direction in ("BUY", "SELL") and direction != _chr_dir:
            logger.warning(
                f"[Fusion] 修正项欲翻向{direction}，但锚Chronos={_chr_dir}→回退锚方向"
                f"(防开错向);置信降至{min(confidence, 0.6):.0%}")
            direction = _chr_dir
            confidence = min(confidence, 0.6)

        # ── 锚定已在聚合前处理（弱模型反向置零 + 锚观望 HOLD）──
        #   此处仅保留宏观镜像偏置作为小幅修正项（2026-08-12 审计修复），不新增方向门。

        if _macro is not None:
            per_model.append({
                "name": "宏观镜像(DXY/VIX)",
                "direction": "BUY" if _macro > 0 else ("SELL" if _macro < 0 else "HOLD"),
                "confidence": round(abs(_macro), 4),
                "hit_rate": None, "qw": round(MACRO_BIAS_WEIGHT, 3),
                "lo": None, "hi": None,
            })
        # ★ 2026-08-17：agree 用**原始方向**（含锚定沉默票）判断——
        #   锚定沉默让 dir_values 只剩单边 → 分歧误判同向 → 不打折（权重虚高）。
        # ★ 2026-08-19 定稿P0-1 单锚化：非锚模型已观测化不参与 raw_dirs，此时
        #   "锚有方向即视为同向/无分歧"（弱票不参与意见，方向锚唯一）；权重满额 1.00
        #   （单锚是设计选择而非能力不足，0.22 基础权重语义不变，不因模型数降权）。
        if _single_anchor:
            agree = len(raw_dirs) >= 1
            w_scale = 1.0
        else:
            agree = len(raw_dirs) >= 2 and (all(v == "BUY" for v in raw_dirs) or all(v == "SELL" for v in raw_dirs))
            hit_rates = [float(m.get("hit_rate")) for m in usable if m.get("hit_rate") is not None]
            hit_avg = (sum(hit_rates) / len(hit_rates)) if hit_rates else 0.0

            # ── 权重缩放：参与模型越多、越同向，这一票越有分量 ──────────
            # 2模型=0.70 / 3模型=0.85 / 4模型=1.00；内部打架再打 0.85 折。
            # 避免「2 个模型的融合票」拿到和「4 个模型一致同向」相同的话语权。
            w_scale = min(1.0, 0.40 + 0.15 * len(usable))
            if not agree:
                w_scale *= 0.85

        # ★ 2026-08-19 定稿P0-1：model_count 反映**参与投票**的模型数——
        #   单锚化时=锚(Chronos)计数(1)，观测模型不计入（避免 summary 显示"4模型"误导）。
        _vote_count = 1 if (_single_anchor and _chr is not None) else len(usable)
        return FusionVote(
            available=True,
            direction=direction,
            confidence=round(confidence, 4),
            score=round(norm, 4),
            lo=max(los) if los else None,
            hi=min(his) if his else None,
            agree=agree,
            hit_rate_avg=round(hit_avg, 3),
            per_model=per_model,
            model_count=_vote_count,
            weight_scale=round(w_scale, 3),
            note=(f"{'Chronos锚' if _single_anchor and _chr is not None else '4模型融合'}"
                  f"({_vote_count}票{'·同向' if agree else '·分歧'}): "
                  f"方向强度={norm:+.2f} 命中率均={hit_avg:.0%} 权重系数={w_scale:.2f}"),
        )


# 模块级单例（与 TSReferenceService 同生命周期，避免重复建实例）
_service = None


def get_service() -> FusionService:
    global _service
    if _service is None:
        _service = FusionService()
    return _service
