# -*- coding: utf-8 -*-
"""万象Ai 实时盯盘：精准提取三道防线与开平仓事件，正确编码写入盯盘日志。
用法：python 盯盘监控.py  (后台运行)
"""
import os, time, sys

LOG = "supervisor_uvicorn.log"
OUT = "盯盘_实时.log"

# 只抓交易/防线相关，屏蔽 dashboard 轮询(GET /api/)淹没
KW = [
    "开仓", "平仓", "下单", "open_position", "close_position",
    "第⑥道防线", "反向即跑", "SMC订单流锚", "真进化", "置信修正",
    "锁利", "篮子", "盈利锁", "盈利", "浮亏", "亏损",
    "自动循环", "反转即时平", "反转哨兵",
    "MetaAgent] 裁决", "MetaAgent] 判定", "MetaAgent] 翻",
    "MetaAgent] 真进化闭环",
    "持仓", "adverse", "adverse_move",
    "trade_executor",
]

def main():
    import sys as _sys
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), LOG)
    outp = os.path.join(os.path.dirname(os.path.abspath(__file__)), OUT)
    with open(outp, "a", encoding="utf-8") as wf:
        wf.write(f"\n===== 盯盘启动 {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        wf.flush()
        print(f"===== 盯盘启动 {time.strftime('%H:%M:%S')} =====", flush=True)
        # 跳到末尾，只看新日志
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)
            last_beat = time.time()
            while True:
                line = f.readline()
                if not line:
                    # 检查文件是否被轮转(大小变小)
                    try:
                        if os.path.getsize(path) < f.tell():
                            f.seek(0, 0)
                    except Exception:
                        pass
                    time.sleep(0.3)
                    if time.time() - last_beat > 30:
                        wf.write(f"[心跳] {time.strftime('%H:%M:%S')} 监控中...\n")
                        wf.flush()
                        print(f"[心跳] {time.strftime('%H:%M:%S')} 监控中...", flush=True)
                        last_beat = time.time()
                    continue
                s = line.rstrip("\n")
                if any(k in s for k in KW):
                    # 去掉超长 GET 行噪音（双保险）
                    if "GET /api/" in s and "自动循环" not in s and "MetaAgent" not in s:
                        continue
                    wf.write(s[:400] + "\n")
                    wf.flush()
                    print(s[:400], flush=True)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
