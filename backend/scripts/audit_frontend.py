"""无头审计前端：加载 http://127.0.0.1:8080，抓控制台错误与真实 DOM。"""
import sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
URL = "http://127.0.0.1:8080/?_cb=" + str(int(time.time()))

console_msgs = []
page_errors = []

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=EDGE, headless=True,
                                args=["--no-sandbox", "--disable-gpu"])
    page = browser.new_page()
    page.on("console", lambda m: console_msgs.append((m.type, m.text)))
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    page.on("requestfailed", lambda r: page_errors.append(
        f"REQ_FAILED {r.url} :: {r.failure}"))

    print(f"[加载] {URL}")
    try:
        resp = page.goto(URL, wait_until="networkidle", timeout=30000)
        print(f"[HTTP] status={resp.status} final_url={page.url}")
    except Exception as e:
        print(f"[GOTO_ERROR] {e}")

    # 等 React 挂载
    time.sleep(4)

    title = page.title()
    root_html = ""
    try:
        root_html = page.eval_on_selector("#root", "el => el.innerHTML") or ""
    except Exception as e:
        root_html = f"<eval error: {e}>"
    print(f"[TITLE] {title!r}")
    print(f"[ROOT_LEN] {len(root_html)} chars")
    print(f"[ROOT_HEAD] {root_html[:300]!r}")

    print(f"\n=== CONSOLE ({len(console_msgs)}) ===")
    for t, txt in console_msgs:
        print(f"  [{t}] {txt[:300]}")
    print(f"\n=== PAGE ERRORS ({len(page_errors)}) ===")
    for e in page_errors:
        print(f"  {e[:400]}")

    # 截图
    try:
        page.screenshot(path=str(Path(__file__).resolve().parent / "audit_shot.png"))
        print("\n[截图] audit_shot.png 已保存")
    except Exception as e:
        print(f"[截图失败] {e}")

    browser.close()
