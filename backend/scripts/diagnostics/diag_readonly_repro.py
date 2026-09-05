"""readonly 根因复现实验（一次性诊断脚本，非生产代码）。

事实基础：
  - 独立进程直连同一 URI 写入 0.2ms 成功（已实测 10/10）
  - uvicorn 进程内 _raw_creator 连续 6 次退避全失败 -> init_db 6 轮 = 198s

假设：app 的某个 import 产生副作用（后台线程持续写库 / 打开长事务 /
      模块级连接残留），使同进程后续写连接被判 readonly。

方法：先测干净基线，再 import 完整应用依赖链，再测同一操作。
      若 import 后复现，则按依赖分组二分定位到具体模块。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def probe(tag, n=5):
    """直接调用生产 _raw_creator，测真实成功率与耗时。"""
    from app.database import _raw_creator
    ok = 0
    fails = []
    costs = []
    for _ in range(n):
        t0 = time.time()
        try:
            c = _raw_creator()
            c.close()
            ok += 1
        except Exception as e:  # noqa: BLE001
            fails.append(f"{type(e).__name__}: {str(e)[:110]}")
        costs.append(time.time() - t0)
    avg = sum(costs) / len(costs) * 1000
    print(f"  [{tag}] 成功 {ok}/{n}  均耗时 {avg:.1f}ms")
    for f in fails[:2]:
        print(f"       失败: {f}")
    return ok == n


def list_threads(tag):
    import threading
    ts = [t for t in threading.enumerate() if t is not threading.main_thread()]
    print(f"  [{tag}] 非主线程数 = {len(ts)}")
    for t in ts[:12]:
        print(f"       - {t.name} daemon={t.daemon} alive={t.is_alive()}")


if __name__ == "__main__":
    print("=" * 70)
    print("STEP 1  干净基线（只 import app.database）")
    print("=" * 70)
    base_ok = probe("baseline")
    list_threads("baseline")

    print()
    print("=" * 70)
    print("STEP 2  import 完整应用依赖链（app.main）")
    print("=" * 70)
    t0 = time.time()
    try:
        import app.main  # noqa: F401
        print(f"  import app.main 完成，耗时 {time.time() - t0:.2f}s")
    except Exception as e:  # noqa: BLE001
        print(f"  import app.main 失败: {type(e).__name__}: {e}")
    list_threads("after-import")

    print()
    print("=" * 70)
    print("STEP 3  import 后再测同一操作")
    print("=" * 70)
    after_ok = probe("after-import")

    print()
    print("=" * 70)
    if base_ok and not after_ok:
        print("结论：复现成功 —— readonly 由 app 的 import 副作用引入。")
    elif base_ok and after_ok:
        print("结论：未复现 —— import 本身无副作用，问题在 lifespan 运行期或 uvicorn 环境。")
    else:
        print("结论：基线即失败 —— 与 app import 无关，属环境级（文件锁/杀软/残留 journal）。")
    print("=" * 70)
