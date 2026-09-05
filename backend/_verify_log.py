# -*- coding: utf-8 -*-
"""只读日志分析器 — 持续验证器专用。不修改任何交易代码/配置/订单。

用法: python _verify_log.py [尾部读取字节数]
注意: 必须写成脚本文件（UTF-8），不要用 python -c 传中文正则，
      Git Bash 下中文会被 shell 编码破坏导致永远不匹配（已踩坑）。
"""
import re
import sys
import collections

LOG = "supervisor_uvicorn.log"
TAIL = int(sys.argv[1]) if len(sys.argv) > 1 else 8_000_000

# 全部用 ASCII 模块名/函数名做锚点，避开中文匹配问题。
# 【第3轮修正】此前 "行情为模拟数据→强制HOLD" 错误地匹配整个 debate_engine:decide，
# 把 6291 条 INFO 一起算成故障（虚报 3951 次）。凡是按模块名匹配的行，
# 必须叠加日志级别过滤，否则必然误报。级别过滤写在 LEVEL_FILTER 里。
PATTERNS = {
    "持仓扫描失败(本轮止损/锁利/熔断跳过)": r"get_all_positions_rescanned",
    "辩论引擎异常(仅WARN/ERROR)":          r"debate_engine:decide",
    "单轮决策超时180s":                   r"_run_cycle_with_timeout",
    "DeepSeek空响应(finish_reason=length)": r"deepseek_client:(analyze|evaluate_exits):\d+ - .*(ERROR|空内容)",
    "DeepSeek截断自愈重试(4096→8192)":     r"evaluate_exits:689",
    "NoneType has no len(预处理异常)":      r"has no len",
    "smart_exit 持仓评估":                r"smart_exit:evaluate_position:193",
    "smart_exit 锁利/保本动作":            r"smart_exit:evaluate_position:217",
    "MetaAgent 裁决":                     r"meta_agent:adjudicate:951",
}

# 仅统计这些级别；未列出的事件不做级别过滤（全计）
LEVEL_FILTER = {
    "辩论引擎异常(仅WARN/ERROR)": ("WARNING", "ERROR", "CRITICAL"),
    "持仓扫描失败(本轮止损/锁利/熔断跳过)": ("WARNING", "ERROR", "CRITICAL"),
    "单轮决策超时180s": ("WARNING", "ERROR", "CRITICAL"),
}


def level_ok(line, name):
    allowed = LEVEL_FILTER.get(name)
    if not allowed:
        return True
    head = line[:40]
    return any(("| %s" % lv) in head for lv in allowed)


def hour_of(line):
    m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}):", line)
    return m.group(1) if m else None


def main():
    with open(LOG, "rb") as f:
        size = f.seek(0, 2)
        f.seek(max(0, size - TAIL))
        data = f.read().decode("utf-8", "ignore")
    lines = data.split("\n")

    print(f"日志总大小: {size/1024/1024:.1f} MB，本次分析尾部 {TAIL/1024/1024:.1f} MB，共 {len(lines)} 行\n")
    print(f"{'事件':40s} {'近4小时分布':<40s} 合计")
    print("-" * 100)
    for name, pat in PATTERNS.items():
        rx = re.compile(pat)
        cnt = collections.Counter()
        for ln in lines:
            if rx.search(ln) and level_ok(ln, name):
                h = hour_of(ln)
                if h:
                    cnt[h] += 1
        if not cnt:
            print(f"{name:40s} {'(无)':<40s} 0")
            continue
        last = sorted(cnt.items())[-4:]
        dist = " | ".join(f"{k[-2:]}时:{v}" for k, v in last)
        print(f"{name:40s} {dist:<40s} {sum(cnt.values())}")

    # 后端重启时间点（重启风暴是隐性头号风险：计数器清零、持仓监控出现盲窗）
    print("\n-- 后端重启时间点 --")
    boots = []
    for i, ln in enumerate(lines):
        if "Started server process" in ln:
            pid = re.search(r"\[(\d+)\]", ln)
            ts = None
            for j in range(i, max(0, i - 60), -1):
                m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", lines[j])
                if m:
                    ts = m.group(1)
                    break
            boots.append((ts, pid.group(1) if pid else "?"))
    for idx, (ts, pid) in enumerate(boots):
        delta = ""
        if idx and ts and boots[idx - 1][0]:
            try:
                from datetime import datetime as _dt
                a = _dt.strptime(boots[idx - 1][0], "%Y-%m-%d %H:%M:%S")
                b = _dt.strptime(ts, "%Y-%m-%d %H:%M:%S")
                delta = "  (距上次 %.0f 分钟)" % ((b - a).total_seconds() / 60.0)
            except Exception:
                pass
        print(f"   {ts}  pid={pid}{delta}")
    print(f"   合计重启 {len(boots)} 次")

    # 持仓扫描失败按账号拆分
    print("\n-- 持仓扫描失败 按账号(近4小时) --")
    per = collections.Counter()
    for ln in lines:
        m = re.search(r"get_all_positions_rescanned:\d+ - \[(.*?)\] ([0-9a-f]{8})", ln)
        if m:
            per[m.group(2)] += 1
    for k, v in per.most_common():
        print(f"   {k}: {v} 次")

    # smart_exit 锁利动作明细
    print("\n-- smart_exit 锁利/保本动作 最近12条 --")
    acts = [ln for ln in lines if "smart_exit:evaluate_position:217" in ln]
    for ln in acts[-12:]:
        print("   " + ln.strip()[:190])

    # 最新价格线索
    print("\n-- 最近 smart_exit 评估(含现价推算) --")
    for ln in [x for x in lines if "smart_exit:evaluate_position:193" in x][-6:]:
        m = re.search(r"pos=(\w+) move=([\-\d.]+) trigger=([\d.]+) sl=([\d.]+) open=([\d.]+)", ln)
        if m:
            side, move, trig, sl, op = m.group(1), float(m.group(2)), m.group(3), m.group(4), float(m.group(5))
            cur = op - move if side == "sell" else op + move
            print(f"   {ln[:23]} {side} open={op} move={move} → 推算现价≈{cur:.2f} sl={sl} trigger={trig}")

    # MetaAgent 最近裁决
    print("\n-- MetaAgent 最近8次裁决 --")
    for ln in [x for x in lines if "meta_agent:adjudicate:951" in x][-8:]:
        print("   " + ln.strip()[:170])


if __name__ == "__main__":
    main()
