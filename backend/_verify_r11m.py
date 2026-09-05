# -*- coding: utf-8 -*-
"""第11轮 m：regime 判定复核 —— ADX 说震荡，EMA20 序列是否其实是单边趋势"""
import json, sys, io, urllib.request, urllib.error
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
BASE = "http://127.0.0.1:8080"; EMAIL = "1558895@qq.com"; PASSWORD = "Tzhl@708090"


def req(method, path, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token: r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r, timeout=25) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


st, raw = req("POST", "/api/auth/login", body={"email": EMAIL, "password": PASSWORD})
tok = json.loads(raw).get("access_token")
st, raw = req("GET", "/api/dashboard/market-chart?tf=M15", tok)
d = json.loads(raw)
ind = d.get("indicators", {})
s = ind.get("ema20_series") or []
print(f"EMA20 序列长度 = {len(s)}")
if s:
    print(f"首值={s[0]}  末值={s[-1]}  当前ema20={ind.get('ema20')}")
    for w in (20, 40, 60, len(s)):
        seg = s[-w:]
        up = sum(1 for a, b in zip(seg, seg[1:]) if b > a)
        n = len(seg) - 1
        print(f"  近{w:>3}根: 上升占比 {up}/{n} = {up/n*100:5.1f}%  "
              f"净变动 {seg[-1]-seg[0]:+8.2f}  最大回撤 {max(max(seg[:i+1])-seg[i] for i in range(len(seg))):.2f}")

price = (d.get("current") or {}).get("bid")
atr, ema20 = ind.get("atr"), ind.get("ema20")
if price and atr and ema20:
    ext = (price - ema20) / atr
    print(f"\n延伸度: (bid {price} − ema20 {ema20}) / ATR {atr} = {ext:+.2f} ×ATR")
print(f"ADX={ind.get('adx')}  RSI={ind.get('rsi')}  regime判定='{ind.get('trend')}'")
print(f"布林: 上{ind.get('boll_upper')} 中{ind.get('boll_mid')} 下{ind.get('boll_lower')} "
      f"→ 现价{'已破上轨' if price and price > (ind.get('boll_upper') or 9e9) else '在带内'}")

# 今日各批次入场时的延伸度回溯（用开仓价近似）
print("\n=== 今日各批 SELL 入场时的延伸度估算（以当时 ema20 近似）===")
print("  批次        开仓价    参考ema20   延伸(×ATR)  结局")
for name, op, e20, res in (("00:00批", 4349.16, 4341.0, "人工割 -2,587"),
                           ("00:33批", 4361.50, 4348.0, "人工平 +1,022"),
                           ("01:07批", 4353.57, 4350.5, "人工割   -757"),
                           ("02:15批", 4363.50, 4354.0, "真实止损 -3,322")):
    print(f"  {name:<10}{op:>9.2f}{e20:>11.1f}{(op-e20)/7.16:>12.2f}   {res}")
