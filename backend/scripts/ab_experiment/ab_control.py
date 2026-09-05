#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A/B 实验·开关控制脚本（周一实盘 walk-forward 用）

功能：
  - 翻转 config.py 里的 DEBATE_RING_ENABLED 总开关（辩论环加法模块）。
  - 把当前实验态写入 ab_state.json（供监控/判定脚本读取，确认后端确实加载了目标模式）。
  - 打印重启指引（沙箱无法自动重启孤儿进程，须用户双击 restart_task_backend.bat）。

用法：
  python ab_control.py --off      # 第1周基线：关闭辩论环
  python ab_control.py --on       # 第2周处理：开启辩论环
  python ab_control.py --status   # 仅查看当前开关态，不改写

注意：本脚本只改 config.py 一行 + 写 ab_state.json，绝不触碰其他逻辑。
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# scripts/ab_experiment/ab_control.py -> backend/app/config.py
CONFIG_PATH = os.path.normpath(os.path.join(HERE, "..", "..", "app", "config.py"))
STATE_PATH = os.path.join(HERE, "ab_state.json")

LINE_RE = re.compile(r"^(.*DEBATE_RING_ENABLED:\s*bool\s*=\s*)(True|False)(.*)$")


def read_current():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            m = LINE_RE.match(line.rstrip("\n"))
            if m:
                return m.group(2) == "True"
    return None


def set_flag(target: bool):
    new = "True" if target else "False"
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()
    changed = False
    for i, line in enumerate(lines):
        m = LINE_RE.match(line.rstrip("\n"))
        if m and m.group(2) != new:
            lines[i] = f"{m.group(1)}{new}{m.group(3)}\n"
            changed = True
            break
    if changed:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            f.writelines(lines)
    return changed


def write_state(target: bool):
    from datetime import datetime
    state = {
        "debate_ring_enabled": target,
        "flipped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": "off=第1周基线 / on=第2周处理；翻转后须双击 restart_task_backend.bat 重载",
    }
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser(description="A/B 实验·辩论环开关控制")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--on", action="store_true", help="开启辩论环（第2周处理）")
    grp.add_argument("--off", action="store_true", help="关闭辩论环（第1周基线）")
    grp.add_argument("--status", action="store_true", help="仅查看当前开关态")
    args = ap.parse_args()

    cur = read_current()
    if cur is None:
        print(f"[错误] 未在 {CONFIG_PATH} 找到 DEBATE_RING_ENABLED 定义，脚本路径可能漂移。")
        sys.exit(1)

    cur_str = "开启(ON)" if cur else "关闭(OFF)"
    print(f"当前状态：辩论环 = {cur_str}")

    if args.status:
        return

    target = bool(args.on)
    if target == cur:
        print(f"目标态与当前一致（{cur_str}），无需改写 config.py；仍刷新 ab_state.json。")
    else:
        set_flag(target)
        print(f"已改写 config.py -> 辩论环 = {'开启(ON)' if target else '关闭(OFF)'}")

    write_state(target)
    print(f"已写入实验态：{STATE_PATH}")
    print("\n⚠️ 生效步骤（沙箱无法自动重启孤儿进程）：")
    print("   请双击 F:\\WanxiangAI\\backend\\restart_task_backend.bat 重载后端。")
    print("   重载后访问 http://127.0.0.1:8080/api/health ，确认 'debate_ring_enabled' 字段 = "
          f"{str(target).lower()} 即加载成功。")


if __name__ == "__main__":
    main()
