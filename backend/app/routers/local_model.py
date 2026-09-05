"""
万象Ai — 本地模型（Qwen3-8B / Chronos-2）运维 API

端点：
  GET  /api/local-model/status     两个本地模型的运行状态与工作量
  POST /api/local-model/warm       预热 Qwen3（主动把权重装进显存）
  POST /api/local-model/selftest   跑一次真实校对，验证端到端可用

╔══════════════════════════════════════════════════════════════════════╗
║ 设计要点：本地模型是【增强项】，不是【依赖项】。                       ║
║                                                                      ║
║   本文件所有端点都不得影响交易主链路。即便 Ollama 完全没装，          ║
║   status 也要正常返回一份「未安装」的结构化说明，而不是 500。         ║
║   前端据此渲染"未启用"状态并给出安装指引——把缺失讲清楚，            ║
║   比让页面报错有用得多。                                              ║
║                                                                      ║
║ warm 为什么要独立成端点：Qwen3-8B 首次加载要把 5GB 权重读进显存，     ║
║   耗时可达数十秒。如果等到 L2 降级那一刻才加载，正好赶上系统最脆弱、   ║
║   最需要它立刻出票的时候。所以给运维一个「提前热身」的按钮。          ║
╚══════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends
from loguru import logger

from app.models.user import User
from app.routers.auth import get_current_user

router = APIRouter(prefix="/api/local-model", tags=["本地模型"])


def _qwen_status() -> dict:
    """Qwen3-8B 状态。任何异常都转成结构化的「不可用」，不向上抛。"""
    try:
        from app.services.local_llm_service import status_dict

        st = status_dict()
    except Exception as e:  # noqa: BLE001
        return {
            "available": False,
            "enabled": False,
            "reason": f"服务加载失败: {str(e)[:150]}",
            "model": "qwen3:8b",
        }

    # 补一段给人看的状态描述。前端不该自己拼这类文案——
    # 同一份判断逻辑散落到多个页面，早晚会出现两个页面说法不一致。
    if not st.get("enabled"):
        st["headline"] = "已手动关闭"
        st["hint"] = "环境变量 WX_LOCAL_LLM_DISABLED 已启用，如需开启请移除该变量"
    elif st.get("available"):
        st["headline"] = "在岗"
        st["hint"] = "正在担任决策校对员；云端双脑失联时自动转为降级副驾"
    else:
        reason = str(st.get("reason") or "")
        if "不可达" in reason:
            st["headline"] = "未启用"
            st["hint"] = "未检测到 Ollama 服务。安装后执行 ollama pull qwen3:8b 即可自动接入"
        elif "未找到模型" in reason:
            st["headline"] = "缺模型"
            st["hint"] = "Ollama 已运行但缺少模型，执行 ollama pull qwen3:8b"
        else:
            st["headline"] = "不可用"
            st["hint"] = reason or "原因未知"
    return st


def _chronos_status() -> dict:
    """Chronos-2 状态。同样全异常安全。"""
    try:
        from app.services.chronos_service import get_chronos

        eng = get_chronos()
        st = eng.status if isinstance(getattr(eng, "status", None), dict) else {}
        if not isinstance(st, dict):
            st = {}
        st.setdefault("available", False)
    except Exception as e:  # noqa: BLE001
        st = {"available": False, "reason": f"服务加载失败: {str(e)[:150]}"}

    st["model"] = "chronos-2-120m"

    # 三态而非两态：「还没被叫醒」和「叫醒了但坏了」是完全不同的处境，
    # 混成一句「已降级」会让运维误以为模型故障，跑去排查一个根本不存在的问题。
    if st.get("available"):
        st["headline"] = "在岗"
        ok = int(st.get("calls_ok") or 0)
        ago = st.get("last_ok_ago_s")
        cov = st.get("last_covariates")
        if ok > 0 and ago is not None:
            mode = f"多变量({'/'.join(cov)})" if cov else "单变量"
            st["hint"] = (
                f"正在提供 P10/P50/P90 时序分位预测并参与质量陪审团加权；"
                f"累计成功 {ok} 次，{int(ago)} 秒前刚出过一次{mode}预测"
            )
        else:
            st["hint"] = "模型权重已载入显存，等待首次决策周期调用"
    elif not st.get("initialized"):
        st["headline"] = "待唤醒"
        st["hint"] = "采用懒加载：首个决策周期触发时才载入显存，属正常状态"
    else:
        st["headline"] = "已降级"
        st["hint"] = (
            (st.get("load_error") or "时序预测不可用")
            + " → 决策回退至 SMC 订单流 + 体制感知（不影响正常交易）"
        )
    return st


@router.get("/status")
def get_status(user: User = Depends(get_current_user)):
    """两个本地模型的合并状态。前端「本地双核」面板的唯一数据源。"""
    qwen = _qwen_status()
    chronos = _chronos_status()

    # 四模型就绪判定收口在后端：前端不得自己数模型个数。
    # 品牌副文有「虚标红线」——只有四个模型真的都在岗，才允许对外
    # 宣称「四模型协同」，否则一律用三模型过渡副文。
    #
    # 云端双脑的「就绪」判定 = 有过成功记录（total_ok > 0）且当前未失联（not down）。
    #
    # 这里刻意【不】用「没报错就算就绪」的宽松口径。一个从未被调用过的组件，
    # 既没有成功记录也没有失败记录，把它算作就绪就是在虚标——恰恰是品牌红线
    # 要防的那种自欺。stale（一段时间没上报）不算掉线，因为 L2 降级时根本
    # 不会去调云 API，云自然不再上报，那是预期行为而非故障。
    ready_count = 0
    cloud_detail: dict = {}
    try:
        from app.services.platform_health_monitor import snapshot_dict

        snap = snapshot_dict()
        comps = snap.get("components") or {}
        for k in ("deepseek", "hunyuan"):
            c = comps.get(k) or {}
            down = bool(c.get("down"))
            total_ok = int(c.get("total_ok", 0) or 0)
            stale = bool(c.get("stale"))
            # 三态区分（品牌红线：只有「真有成功记录」才叫就绪）：
            #   active = 有过成功上报（可宣称协同）
            #   down   = 连续失败达阈值（真故障，需运维介入）
            #   idle   = 既无成功记录也无失败（冷启动 / 当前无交易触发调用），
            #            属正常待命 —— 绝非故障，不应渲染成红色「未就绪」。
            #            最典型场景：后端重启后计数器清零 + 当前休市无决策，
            #            云端双脑从未被调用。此时若画红色，客户会误以为模型掉了，
            #            实则系统完全健康，开盘有交易即自动恢复。
            if down:
                activation = "down"
            elif total_ok > 0:
                activation = "active"
            else:
                activation = "idle"
            ready = activation == "active"
            cloud_detail[k] = {
                "ready": ready,
                "activation": activation,
                "down": down,
                "stale": stale,
                "total_ok": total_ok,
                "last_error": c.get("last_error") or "",
            }
            if ready:
                ready_count += 1
    except Exception as e:  # noqa: BLE001
        # 健康监控本身取不到时，不猜——按 0 就绪计，宁可少报不可虚标。
        cloud_detail = {"error": str(e)[:150]}
    if chronos.get("available"):
        ready_count += 1
    if qwen.get("available"):
        ready_count += 1

    quad = ready_count >= 4
    return {
        "qwen": qwen,
        "chronos": chronos,
        "cloud": cloud_detail,
        "summary": {
            "local_ready": int(bool(qwen.get("available"))) + int(bool(chronos.get("available"))),
            "local_total": 2,
            "model_ready_count": ready_count,
            "model_total": 4,
            "quad_ready": quad,
            # 品牌副文由后端下发，前端不得自己拼——一旦两处逻辑分叉，
            # 迟早出现「首页说四模型、状态页说三模型」的自打脸。
            "tagline": (
                "云端双脑 × 本地双核 · 四模型协同决策"
                if quad else
                "云端双脑 × 本地时序 · 三模型协同决策"
            ),
        },
        "checked_at": time.time(),
    }


def _warm_model_sync():
    """warm_model 的同步实现体，供线程池 offload。"""
    try:
        from app.services.local_llm_service import get_local_llm

        svc = get_local_llm()
        if not svc.available(force=True):
            return {
                "ok": False,
                "message": "Ollama 不可用，无法预热",
                "detail": svc.status().get("reason"),
            }
        t0 = time.time()
        # 用 _generate 而非 proofread：不污染校对员的工作量统计。
        out = svc._generate("回复 OK 两个字符即可。", 60.0)  # noqa: SLF001
        dt = (time.time() - t0) * 1000.0
        ok = out is not None
        logger.info(f"[本地模型] 预热{'成功' if ok else '失败'}，耗时 {dt:.0f}ms")
        return {
            "ok": ok,
            "message": f"预热{'成功' if ok else '失败'}",
            "latency_ms": round(dt, 1),
            "model": svc.status().get("model"),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[本地模型] 预热异常: {e}")
        return {"ok": False, "message": f"预热异常: {e}"}


@router.post("/warm")
async def warm_model(user: User = Depends(get_current_user)):
    """预热 Qwen3：发一个极短的请求把权重拉进显存。

    刻意用真实请求而不是 Ollama 的 preload 接口——真实请求能同时验证
    「模型能加载」和「能正常产出」，preload 只能验证前者。

    2026-08-09：本地 LLM 推理可能阻塞数十秒，改为 async + to_thread offload。
    """
    import asyncio
    return await asyncio.to_thread(_warm_model_sync)


@router.post("/selftest")
def self_test(user: User = Depends(get_current_user)):
    """端到端自检：喂一个**故意写错**的决策，看校对员能否揪出来。

    为什么用错误样本而不是正确样本：正确样本返回「没问题」时，
    你分不清它是真检查过了，还是模型摆烂一律回 OK。
    用一个止损明显挂反的样本，能查出来才算真的在工作。
    """
    bad_decision = {
        "decision": "BUY",
        "confidence": 0.8,
        "entry_price": 2650.0,
        "stop_loss": 2680.0,      # ← BUY 的止损挂在入场价上方，明显错误
        "take_profit": 2600.0,    # ← 止盈也挂反了
        "reason": "市场呈现明显的下行趋势，空头力量占据主导地位。",  # ← 与 BUY 自相矛盾
    }
    snapshot = {"current_price": 2650.0}
    try:
        from app.services.local_llm_service import get_local_llm

        svc = get_local_llm()
        if not svc.available(force=True):
            return {
                "ok": False,
                "passed": False,
                "message": "本地模型不可用，无法自检",
                "detail": svc.status().get("reason"),
            }
        res = svc.proofread(bad_decision, snapshot)
        if res is None:
            return {"ok": False, "passed": False, "message": "校对员无响应"}

        # 判定标准：必须揪出问题。一个明显错误的样本被判 clean，
        # 说明模型没有真正在核对，这比它不可用更危险。
        passed = not res.ok and len(res.issues) > 0
        return {
            "ok": True,
            "passed": passed,
            "message": (
                f"自检通过：校对员正确识别出 {len(res.issues)} 处问题"
                if passed else
                "自检未通过：校对员未能识别出故意植入的错误，其判断结果不可信"
            ),
            "issues": res.issues,
            "severity": res.severity,
            "latency_ms": round(res.latency_ms, 1),
            "sample": "BUY 决策但止损挂在入场价上方 + 理由主张下跌",
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "passed": False, "message": f"自检异常: {str(e)[:200]}"}
