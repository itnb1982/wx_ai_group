# -*- coding: utf-8 -*-
"""
重启前预平仓锁利脚本（★ 2026-08-10 新增）

背景：restart_service_admin.ps1 用 Stop-Process -Force 杀 terminal64.exe（MT5 终端），
MT5 客户端默认 auto-reverse 所有持仓 → 重启瞬间损失全部未实现浮盈
（实测 20:32:48 一次损失 $1030：#377637348 +348 + #377596715 +682 被 0 盈亏 reverse）。

本脚本在【杀 MT5 终端之前】调用后端 /api/emergency/flatten 主动按市价平掉全部持仓锁利，
避免被 MT5 强杀时 auto-reverse 掉浮盈。halt_after=false 不封盘，重启后 AI 正常继续交易。

调用方：restart_service_admin.ps1（步骤 0.5，sc stop 之前）
失败策略：后端不可达/登录失败 → 打印警告并退出 0（不阻塞重启，回退到 MT5 auto-reverse）。
"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8080"
# 运维凭据：单机私有部署的可信脚本通道（与重启脚本同权限级别）。
# 如需更换，修改下面两行即可；也可从环境变量覆盖。
EMAIL = "1558895@qq.com"
PASSWORD = "Tzhl@708090"


def _post(path, body, token=None, timeout=90):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers=headers, method="POST",
    )
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def main():
    # 0) 后端必须活着才能平仓；先探活
    try:
        urllib.request.urlopen(BASE + "/api/health", timeout=5)
    except Exception as e:
        print(f"[预平仓] 后端未就绪({e})，跳过预平仓（MT5 被强杀时仍会 auto-reverse，可能损失浮盈）")
        return 0
    # 1) 登录
    try:
        tok = _post("/api/auth/login", {"email": EMAIL, "password": PASSWORD}, timeout=15).get("access_token", "")
        if not tok:
            print("[预平仓] 登录失败（token 为空），跳过")
            return 0
    except Exception as e:
        print(f"[预平仓] 登录异常({e})，跳过")
        return 0
    # 2) 全平（不封盘，重启后 AI 继续交易）
    try:
        res = _post(
            "/api/emergency/flatten",
            {"scope": "global", "reason": "重启前主动平仓锁利", "halt_after": False},
            token=tok,
        )
        ok = res.get("ok")
        flat = res.get("fully_flat")
        closed = res.get("results") or []
        n_closed = sum(r.get("closed_count", 0) for r in closed)
        n_remain = sum(r.get("remaining_count", 0) for r in closed)
        if ok:
            print(f"[预平仓] 成功：平掉 {n_closed} 笔，残留 {n_remain} 笔，fully_flat={flat}")
            if not flat:
                print(f"[预平仓] ⚠ 未完全清空！明细: {json.dumps(closed, ensure_ascii=False)[:400]}")
                print("[预平仓]   将改由 MT5 终端 auto-reverse 兜底（浮盈可能损失）")
        else:
            print(f"[预平仓] 接口返回异常: {json.dumps(res, ensure_ascii=False)[:300]}")
        return 0
    except Exception as e:
        print(f"[预平仓] 平仓调用异常({e})，跳过（回退 auto-reverse）")
        return 0


if __name__ == "__main__":
    sys.exit(main())
