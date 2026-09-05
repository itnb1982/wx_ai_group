# -*- coding: utf-8 -*-
"""
视觉模型 K 线图渲染器（万象Ai · 2026-08-14）
=========================================================================
为视觉模型(qwen2.5-vl 等)渲染 XAUUSD 蜡烛图 PNG。

设计要点：
  · 纯 Pillow 渲染，零额外依赖（Pillow 已装入项目 venv）。
  · 与决策链同源：数据来自 mt5_service.get_market_data 的 timeframes 原始 OHLC，
    保证视觉模型看到的价格结构 = AI 大脑看到的行情，避免"两套数据"错位。
  · 浅色背景 + 彩色蜡烛 + MA20/MA50 + 量能 + 当前价线 + 坐标刻度，
    便于视觉模型直接读取趋势/结构/供给需求区。
  · 不注入已算好的 SMC 区/指标——让视觉模型自己从价格结构识别，
    这正是"多模态增强"相对纯数值指标的价值。
  · 全部异常安全：任何失败返回 None，上层降级（不画图就不出视觉票）。
"""
from __future__ import annotations

import io
from typing import Dict, List, Optional

from loguru import logger

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception as e:  # pragma: no cover
    logger.warning(f"[Vision] Pillow 未安装/不可用: {e}")
    Image = None  # 容错：Pillow 未装时上层感知为"渲染不可用"

# 配色（浅色背景，视觉模型读数友好）
_BG = (255, 255, 255)
_GRID = (232, 232, 232)
_UP = (38, 166, 154)       # 涨 · 绿
_DOWN = (239, 83, 80)     # 跌 · 红
_TEXT = (40, 40, 40)
_MA20 = (33, 150, 243)    # 蓝
_MA50 = (255, 152, 0)     # 橙
_PRICE = (120, 120, 120)
_VOL = (180, 180, 180)


def _font(size: int):
    """字体加载（★ 2026-08-16 加中文字体链）：先中文字体（msyh 微软雅黑 / simhei 黑体），
    再英文字体（arial），最后 Pillow 默认（仅 ASCII，中文会豆腐）。视觉实例在 Windows 跑。"""
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc", "arial.ttf", "arialuni.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _sma(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    n = len(values)
    for i in range(n):
        if i + 1 < period:
            out.append(None)
        else:
            out.append(sum(values[i + 1 - period:i + 1]) / period)
    return out


def _find_swings(bars: List[Dict], window: int = 3) -> List[tuple]:
    """找摆动高低点（fractal 式：中心 i 是 [i-window, i+window] 窗口内的唯一最高/最低）。

    返回 [(index, price, 'H'|'L'), ...]——只描述客观价格结构（任何图表软件都画得出来），
    不注入任何主观 SMC 结论（OB/FVG/流动性区留给视觉模型自己判断）。
    """
    n = len(bars)
    if n < window * 2 + 1:
        return []
    highs = [float(b.get("high") or b.get("close") or 0) for b in bars]
    lows = [float(b.get("low") or b.get("close") or 0) for b in bars]
    out: List[tuple] = []
    for i in range(window, n - window):
        seg_h = highs[i - window:i + window + 1]
        seg_l = lows[i - window:i + window + 1]
        if highs[i] == max(seg_h) and seg_h.count(highs[i]) == 1:
            out.append((i, highs[i], "H"))
        if lows[i] == min(seg_l) and seg_l.count(lows[i]) == 1:
            out.append((i, lows[i], "L"))
    return out


def _swing_seq(swings: List[tuple]) -> str:
    """由最近摆动高低点推算结构状态文字（HH/HL/LH/LL 序列）。

    仅描述客观结构演进（供视觉模型快速锚定趋势/震荡），不替模型下方向结论。
    例：最近高点>前高且最近低点>前低 → "HH+HL 上行结构"。
    """
    hs = [s for s in swings if s[2] == "H"]
    ls = [s for s in swings if s[2] == "L"]
    if len(hs) >= 2 and len(ls) >= 2:
        hh = "HH" if hs[-1][1] > hs[-2][1] else "LH"
        ll = "HL" if ls[-1][1] > ls[-2][1] else "LL"
        if hh == "HH" and ll == "HL":
            return f"{hh}+{ll} 上行结构"
        if hh == "LH" and ll == "LL":
            return f"{hh}+{ll} 下行结构"
        return f"{hh}+{ll} 震荡分歧"
    if len(hs) >= 2:
        return "HH" if hs[-1][1] > hs[-2][1] else "LH"
    if len(ls) >= 2:
        return "HL" if ls[-1][1] > ls[-2][1] else "LL"
    return ""


def render_chart(
    bars: List[Dict],
    title: str,
    width: int = 920,
    height: int = 540,
    ma_periods: tuple = (20, 50),
    markers: Optional[List[Dict]] = None,
    show_swings: bool = True,
) -> Optional[bytes]:
    """渲染蜡烛图 PNG。

    Args:
        bars: 升序 OHLCV 列表，元素 {open,high,low,close,volume,time}
        title: 图标题（如 "XAUUSD H4"）
        markers: 可选价位标记列表，元素 {price:float, label:str, color:(r,g,b)}
                 用于在图上叠加持仓的开仓/SL/TP 水平线（看护模块用）。
        show_swings: 是否叠加客观结构标注（摆动高低点 + HH/HL/LH/LL 状态文字 +
                     最近关键位虚线）。只呈现客观价格结构，不注入主观 SMC 结论。
    Returns:
        PNG bytes；数据不足或渲染失败时返回 None。
    """
    if Image is None or not bars or len(bars) < 5:
        return None
    try:
        closes = [float(b["close"]) for b in bars]
        highs = [float(b["high"]) for b in bars]
        lows = [float(b["low"]) for b in bars]
        vols = [float(b.get("volume", 0) or 0) for b in bars]
        n = len(bars)

        pmin = min(lows)
        pmax = max(highs)
        pad = (pmax - pmin) * 0.06 or 1.0
        pmin -= pad
        pmax += pad

        left, right, top = 10, width - 64, 34
        price_h = height - 96
        bottom = top + price_h
        vol_h = 46
        vol_top = bottom + 8

        def x(i: int) -> int:
            return left + int((right - left) * (i / (n - 1)) if n > 1 else 0)

        def y(p: float) -> int:
            return top + int(price_h * (1 - (p - pmin) / (pmax - pmin)))

        def yv(v: float) -> int:
            mv = max(vols) or 1
            return vol_top + int(vol_h * (1 - v / mv))

        img = Image.new("RGB", (width, height), _BG)
        d = ImageDraw.Draw(img)
        f = _font(11)
        ft = _font(13)

        # 价格网格 + 右轴刻度
        for k in range(5):
            yy = top + int(price_h * k / 4)
            p = pmax - (pmax - pmin) * k / 4
            d.line([(left, yy), (right, yy)], fill=_GRID)
            d.text((right + 6, yy - 7), f"{p:.1f}", fill=_TEXT, font=f)

        # 移动平均线
        for per, col in zip(ma_periods, (_MA20, _MA50)):
            series = _sma(closes, per)
            pts = [(x(i), y(v)) for i, v in enumerate(series) if v is not None]
            if len(pts) > 1:
                d.line(pts, fill=col, width=1)

        # 蜡烛
        cw = max(1, int((right - left) / n * 0.66))
        for i, b in enumerate(bars):
            cx = x(i)
            up = float(b["close"]) >= float(b["open"])
            col = _UP if up else _DOWN
            d.line([(cx, y(b["high"])), (cx, y(b["low"]))], fill=col, width=1)
            yo, yc = y(b["open"]), y(b["close"])
            d.rectangle([cx - cw // 2, min(yo, yc), cx + cw // 2, max(yo, yc)], fill=col)
            d.rectangle([cx - cw // 2, yv(b.get("volume", 0)), cx + cw // 2, vol_top + vol_h],
                        fill=_VOL if up else (210, 150, 150))

        # 当前价线 + 标签
        cur = closes[-1]
        d.line([(left, y(cur)), (right, y(cur))], fill=_PRICE, width=1)
        d.text((right + 6, y(cur) - 7), f"now {cur:.1f}", fill=_PRICE, font=f)

        # ★ 2026-08-16 客观结构标注（swing 高低点 + HH/HL/LH/LL 状态 + 最近关键位虚线）
        #   只画"任何图表软件都画得出的客观价格结构"，绝不注入 OB/FVG/流动性等主观 SMC 结论，
        #   结构与方向仍由视觉模型自己判断——守住"不替模型下结论"的既有设计原则。
        swing_info = ""
        if show_swings:
            try:
                swings = _find_swings(bars, window=3)
                swing_info = _swing_seq(swings)
                # 最近 12 个 swing 点标三角（H=橙 / L=紫），仅取在可见价格范围内的
                sw_col_H = (230, 126, 34)   # 橙
                sw_col_L = (142, 68, 173)   # 紫
                for (si, sp, st) in swings[-12:]:
                    if pmin <= sp <= pmax:
                        sx, sy = x(si), y(sp)
                        if st == "H":
                            d.polygon([(sx, sy - 7), (sx - 5, sy), (sx + 5, sy)], fill=sw_col_H)
                        else:
                            d.polygon([(sx, sy + 7), (sx - 5, sy), (sx + 5, sy)], fill=sw_col_L)
                # 最近一个 swing 高/低位虚线（关键位锚点）
                if swings:
                    last_h = max((s for s in swings if s[2] == "H"), key=lambda s: s[0], default=None)
                    last_l = max((s for s in swings if s[2] == "L"), key=lambda s: s[0], default=None)
                    for (sp, st, col) in ((last_h[1], "H", sw_col_H) if last_h else (None, "", None),
                                          (last_l[1], "L", sw_col_L) if last_l else (None, "", None)):
                        if sp is not None and pmin <= sp <= pmax:
                            yy = y(sp)
                            seg2 = 6
                            for sx in range(left, right, seg2 * 2):
                                d.line([(sx, yy), (min(sx + seg2, right), yy)], fill=col, width=1)
                # 结构状态文字画在标题右侧（模型一眼锚定趋势/震荡）
                if swing_info:
                    ft2 = _font(12)
                    tw = ft2.getlength(swing_info) if hasattr(ft2, "getlength") else len(swing_info) * 8
                    d.text((right - tw - 8, 8), swing_info, fill=(90, 90, 90), font=ft2)
            except Exception:
                swing_info = ""  # 结构标注失败不影响主图（异常安全）

        # 持仓价位标记（看护模块：开仓/SL/TP 水平线 + 标签）
        if markers:
            for mk in markers:
                try:
                    mp = float(mk.get("price"))
                    mcol = mk.get("color") or (90, 90, 90)
                    mlab = str(mk.get("label", ""))
                    if pmin <= mp <= pmax:
                        my = y(mp)
                        # 虚线：短段拼接
                        seg = 8
                        for sx in range(left, right, seg * 2):
                            d.line([(sx, my), (min(sx + seg, right), my)], fill=mcol, width=1)
                        d.text((left + 4, my - 9), mlab, fill=mcol, font=f)
                except Exception:
                    continue

        # 标题
        d.text((left, 8), title, fill=_TEXT, font=ft)

        # 时间轴（首/中/尾）
        tf = _font(10)
        t0 = str(bars[0].get("time", ""))[:16]
        tm = str(bars[n // 2].get("time", ""))[:16]
        te = str(bars[-1].get("time", ""))[:16]
        d.text((left, bottom + 2), t0, fill=_TEXT, font=tf)
        d.text((int((left + right) / 2) - 70, bottom + 2), tm, fill=_TEXT, font=tf)
        d.text((right - 116, bottom + 2), te, fill=_TEXT, font=tf)

        # ★ 2026-08-15 关键修复：Qwen3-VL(qwen3-vl:4b) 在 Ollama 0.32.6 下对超过
        #   视觉 token 上限的图片会"静默返回空 content"（HTTP 200 但正文为空、无任何
        #   报错），导致视觉票永不上桌而 GPU 仍白跑 ~20s 推理发热。实测：920×540 原图
        #   稳定空，resize 到最长边≤672 后稳定返回可解析 JSON。故在此把图收敛到安全尺寸。
        # ★ 2026-08-16：qwen2.5vl:7b 为动态分辨率模型（原生支持更长边），且非 thinking
        #   系无空 content 坑；但为保守兼容 8GB 显存/视觉 token 预算，仍设上限（放宽到 784）。
        _MAX_EDGE = 784
        if max(width, height) > _MAX_EDGE:
            _scale = _MAX_EDGE / max(width, height)
            img = img.resize((max(1, int(width * _scale)), max(1, int(height * _scale))))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()
    except Exception as e:
        # 任何异常 → 渲染不可用（上层降级，绝不上抛）
        logger.warning(f"[Vision] 图表渲染失败: {e}")
        return None
