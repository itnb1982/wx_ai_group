"""
2026-08-17 · 重启后全模型在岗验证（一键审计）
=============================================
覆盖：8 模型岗位矩阵 + 本轮 5 项修复（投票席/持仓管家显示/校对文案/人话解读/交叉验证）
运行：backend 目录下用 .venv python 执行
"""
import json
import subprocess
import sys
import time

BASE = "http://127.0.0.1:8080"


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()


def get_token():
    r = sh(f'curl -s --max-time 10 -X POST {BASE}/api/auth/login '
           f'-H "Content-Type: application/json" '
           f'-d \'{{"email":"1558895@qq.com","password":"Tzhl@708090"}}\'')
    try:
        return json.loads(r).get("access_token", "")
    except Exception:
        return ""


def api(path, token):
    r = sh(f'curl -s --max-time 12 {BASE}{path} -H "Authorization: Bearer {token}"')
    try:
        return json.loads(r)
    except Exception:
        return {}


def main():
    print("=" * 60)
    print("重启后全模型在岗验证")
    print("=" * 60)

    # 1. 健康
    h = api("/api/health", "")
    print(f"\n[1] 健康: pid={h.get('pid')} uptime={round(h.get('uptime_sec',0)/60,1)}min "
          f"mt5={h.get('mt5_connected')}/{h.get('accounts_expected')} "
          f"degraded={h.get('accounts_degraded')}")

    tok = get_token()

    # 2. 云+本地模型状态
    s = api("/api/local-model/status", tok)
    sm = s.get("summary", {})
    print(f"[2] 模型总态: local={sm.get('local_ready')}/{sm.get('local_total')} "
          f"model={sm.get('model_ready_count')}/{sm.get('model_total')} quad={sm.get('quad_ready')}")
    q = s.get("qwen") or {}
    roles = q.get("roles") or {}
    for rk, rv in roles.items():
        print(f"    Qwen.{rk}: active={rv.get('active')} runs={rv.get('runs')}")
    c = s.get("cloud", {})
    for ck, cv in c.items():
        print(f"    cloud.{ck}: ready={cv.get('ready')} down={cv.get('down')}")

    # 3. TimesFM 交叉验证 + 投票席 + basket
    af = api("/api/dashboard/ai-flow", tok)
    mq = af.get("meta_quality") or {}
    ct = mq.get("cross_ts") or {}
    print(f"[3] TimesFM交叉验证: available={ct.get('available')} "
          f"divergence={ct.get('divergence')} agreement={ct.get('agreement')}")
    v = af.get("voting") or {}
    print(f"    投票席: available={v.get('available')} 有效票={v.get('counted_seats')}/{v.get('total_seats')}")
    for seat in v.get("seats", []):
        print(f"      {seat.get('name')}: {seat.get('vote')} state={seat.get('state')} w={seat.get('weight')}")
    b = af.get("basket") or {}
    print(f"    basket: available={b.get('available')} action={b.get('action')}")

    # 4. 视觉
    vs = api("/api/dashboard/vision-status", tok)
    print(f"[4] 视觉: runs={vs.get('runs')} ok={vs.get('ok_runs')} err={repr(vs.get('last_err'))}")
    vv = vs.get("vote") or {}
    print(f"    vote: available={vv.get('available')} {vv.get('direction')} {vv.get('confidence')}")

    # 5. 参考面板 4 时序
    ts = api("/api/ts-reference/snapshot", tok)
    snap = ts.get("snapshot", ts)
    print(f"[5] 参考面板: status={snap.get('status')} live={snap.get('live')}")
    for m in snap.get("models", []):
        if isinstance(m, dict):
            print(f"    {m.get('name')}: dir={m.get('direction')} conf={m.get('confidence')}")

    # 6. 业务错误
    print("[6] 业务错误检查: 见日志 grep（应无 Traceback/NameError）")


if __name__ == "__main__":
    main()
